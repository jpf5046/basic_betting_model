#!/usr/bin/env python3
"""Season backfill — PLAN.md §2 backfill job / §10 item 7.

Replays the season to date through the feature registry to synthesize the
labeled canonical frame (PLAN.md §5) before the daily pipeline has run for
long: for every date that had games, features are computed point-in-time
(as of that morning, via FeatureContext — the same code path as the daily
run), then the known outcomes are joined on as labels.

    python3 -m pipeline.backfill [--sports WNBA,MLB] [--season KEY]
                                 [--through YYYY-MM-DD]

Output: one wide CSV per sport+season, data/frames/<sport>_<season>.csv —
identity + as-of meta, one column per feature per side, and the outcome
group (final scores, total, margin, winner; empty until a game is final).
Idempotent: each run rebuilds the file for the whole range. Run the
sport's ingest `fetch` first — the games CSV is the input.

Optionally mirror the results into the database afterwards:
    python3 -m pipeline.db load --sports WNBA
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pipeline.features  # noqa: F401  (auto-registers feature defs)
from pipeline.features.context import ADAPTERS, FeatureContext, default_season
from pipeline.features.frame import build_rows
from pipeline.features.registry import features_for
from pipeline.ingest import core
from pipeline.ingest.core import EASTERN, REPO_ROOT, Game

FRAMES_DIR = REPO_ROOT / "data" / "frames"

# §5 column groups added around the feature frame's identity columns.
META_COLS = ["season", "season_type", "as_of_date", "feature_versions"]
OUTCOME_COLS = ["final_away", "final_home", "actual_total", "actual_margin", "winner"]


def outcome(game: Game) -> dict[str, object]:
    """The §5 outcome group — empty strings until the game is final."""
    if game.status != "final" or not (game.away_score and game.home_score):
        return {c: "" for c in OUTCOME_COLS}
    away, home = int(game.away_score), int(game.home_score)
    return {
        "final_away": away,
        "final_home": home,
        "actual_total": away + home,
        "actual_margin": home - away,  # positive = home won by this many
        "winner": game.home_team_id if home > away else game.away_team_id,
    }


def backfill_rows(sport: str, games: list[Game],
                  through: date) -> tuple[list[str], list[dict]]:
    """Frame rows for every game on every date up to `through`, features
    computed as of each date's morning. Returns (columns, rows)."""
    versions = json.dumps(
        {s.name: s.version for s in features_for(sport)}, sort_keys=True
    )
    dates = sorted({g.date for g in games if g.date and g.date <= through.isoformat()})

    columns: list[str] = []
    seen = set()
    all_rows: list[dict] = []
    for day in dates:
        slate = [g for g in games if g.date == day]
        ctx = FeatureContext(sport, date.fromisoformat(day), games)
        day_cols, rows = build_rows(ctx, slate)
        by_id = {g.game_id: g for g in slate}
        for row in rows:
            game = by_id[row["game_id"]]
            row.update(season=game.season, season_type=game.season_type,
                       as_of_date=day, feature_versions=versions)
            row.update(outcome(game))
        for col in day_cols:
            if col not in seen:
                seen.add(col)
                columns.append(col)
        all_rows.extend(rows)

    # Identity first, then meta, then features, then outcomes last.
    feature_cols = [c for c in columns if c not in ("game_id", "sport", "date",
                                                    "away_team_id", "home_team_id", "status")]
    ordered = (["game_id", "sport", "season", "season_type", "date", "as_of_date",
                "away_team_id", "home_team_id", "status"]
               + feature_cols + OUTCOME_COLS + ["feature_versions"])
    all_rows.sort(key=lambda r: (r["date"], r["game_id"]))
    return ordered, all_rows


def frame_csv_path(sport: str, season: str) -> Path:
    return FRAMES_DIR / f"{sport.lower()}_{season}.csv"


def write_frame_csv(sport: str, season: str, columns: list[str],
                    rows: list[dict]) -> Path:
    import csv
    path = frame_csv_path(sport, season)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, restval="")
        w.writeheader()
        w.writerows(rows)
    return path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.backfill", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sports", default=",".join(sorted(ADAPTERS)),
                   help=f"comma-separated subset (default: {','.join(sorted(ADAPTERS))})")
    p.add_argument("--season", help="season key (default: the current one per sport)")
    p.add_argument("--through", help="last date to include, YYYY-MM-DD (default: today)")
    args = p.parse_args(argv)

    through = (date.fromisoformat(args.through) if args.through
               else datetime.now(EASTERN).date())
    sports = [s.strip().upper() for s in args.sports.split(",") if s.strip()]
    unknown = sorted(set(sports) - set(ADAPTERS))
    if unknown:
        sys.exit(f"unknown sport(s) {', '.join(unknown)} — choose from {', '.join(sorted(ADAPTERS))}")

    wrote_any = False
    for sport in sports:
        season = args.season or default_season(sport, through)
        games_csv = ADAPTERS[sport].games_csv_path(season)
        if not games_csv.exists():
            print(f"{sport}: {games_csv} not found — run "
                  f"`python3 -m pipeline.ingest.{sport.lower()} fetch` first; skipping")
            continue
        games = core.read_games_csv(
            games_csv, f"python3 -m pipeline.ingest.{sport.lower()} fetch")
        columns, rows = backfill_rows(sport, games, through)
        if not rows:
            print(f"{sport}: no games on or before {through.isoformat()} — nothing to backfill")
            continue
        path = write_frame_csv(sport, season, columns, rows)
        labeled = sum(1 for r in rows if r["winner"])
        days = len({r["date"] for r in rows})
        print(f"{sport}: {len(rows)} games over {days} dates "
              f"({labeled} labeled finals) -> {path}")
        wrote_any = True

    if not wrote_any:
        sys.exit("nothing backfilled — no games CSVs found (run the ingest fetches first)")


if __name__ == "__main__":
    main()
