#!/usr/bin/env python3
"""Backfill games + odds from an external games_scores/games_odds export.

For dates old enough that the leagues' live feeds no longer serve them and
The Odds API's historical-snapshot endpoint can no longer reach them (its
window is a few days), this is the fallback path: import an existing
scores/odds export shaped like the source tables described alongside this
module (`games_scores`, `games_odds` — one row per game each).

Both input CSVs share `game_key`, which IS The Odds API's event id (the
32-character hex id, not a synthetic hash) — we use it directly as our
canonical `game_id` for backfilled rows, since a hex string can never
collide with a league-native numeric id (NBA/WNBA/MLB/NHL feed ids are all
plain integers). That also means games and odds need no fuzzy matching:
the join is exact and already present in the source data.

Only sport_keys we have a team registry for are imported — WNBA, MLB, NBA,
NHL, plus "basketball_nba_preseason" (seen in the sample data) mapped to
NBA/preseason. Everything else (soccer leagues, NCAAB, NFL, rugby league,
euroleague, ...) is reported and skipped, not silently dropped — there is
no data/teams.csv entry to resolve those team names against yet, and
PLAN.md §1's "an unmapped team is a loud error" principle applies here too.

Known gaps, since the source rows carry no signal for them:
  * `season_type` defaults to "regular" (except the preseason override
    above) — the export has no playoffs/allstar flag.
  * `venue`, `ot`, `doubleheader`, `game_number` are left at their Game
    defaults — not present in the export.
  * `status` is "final" when `completed` is true, else "scheduled" — the
    export has no postponed/suspended/cancelled distinction.

Usage:
    python3 -m pipeline.backfill_external --scores scores.csv --odds odds.csv
        [--sports WNBA,MLB,NBA,NHL] [--dry-run]

`--odds` is optional (scores-only backfill still works). Re-running is
safe: games are upserted by game_id into the season's games CSV, odds
snapshot rows are written as a dedicated file that store.py already knows
how to read alongside every other snapshot, and the event map is a
plain identity (game_id == event_id) upserted by event_id.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pipeline.features.context import ADAPTERS, default_season
from pipeline.ingest import core
from pipeline.ingest.core import Game, utc_to_eastern_date
from pipeline.odds import api as odds_api
from pipeline.odds.match import MAP_FIELDS, upsert_event_map
from pipeline.odds.normalize import load_name_map
from pipeline.odds.store import write_snapshot

# odds-API sport_key -> our SPORT, for the sports we have a team registry for.
SPORT_KEY_TO_SPORT = {v: k for k, v in odds_api.SPORT_KEYS.items()}

# sport_keys the live API doesn't define but this kind of export contains,
# mapped to (SPORT, season_type override).
SPORT_KEY_OVERRIDES = {
    "basketball_nba_preseason": ("NBA", "preseason"),
}


def _resolve_sport(sport_key: str) -> tuple[str, str | None]:
    """(SPORT, season_type override) for an in-scope sport_key, else
    (None, None)."""
    if sport_key in SPORT_KEY_OVERRIDES:
        return SPORT_KEY_OVERRIDES[sport_key]
    sport = SPORT_KEY_TO_SPORT.get(sport_key)
    return (sport, None) if sport else (None, None)


def _as_utc_iso(commence_time: str) -> str:
    """'2024-01-31 00:40:00' (naive, the export's convention: UTC, same as
    The Odds API) -> '2024-01-31T00:40:00Z'."""
    if not commence_time:
        return ""
    return commence_time.replace(" ", "T", 1).rstrip("Z") + "Z"


class NameMaps:
    """Per-sport name_map (+ reverse to abbrev) built once, lazily."""

    def __init__(self):
        self._name_map: dict[str, dict[str, str]] = {}
        self._abbrev: dict[str, dict[str, str]] = {}

    def team_id(self, sport: str, name: str) -> str:
        if sport not in self._name_map:
            self._name_map[sport] = load_name_map(sport)
        return self._name_map[sport].get(name, "")

    def abbrev(self, sport: str, team_id: str) -> str:
        if sport not in self._abbrev:
            with open(core.TEAMS_CSV, newline="") as f:
                self._abbrev[sport] = {
                    r["team_id"]: r["abbrev"] for r in csv.DictReader(f) if r["sport"] == sport
                }
        return self._abbrev[sport].get(team_id, "")


def build_games(rows: list[dict], sports: set[str], names: NameMaps
               ) -> tuple[dict[tuple[str, str], list[Game]], Counter, list[str]]:
    """Scores rows -> {(sport, season): [Game, ...]}, plus a skip-reason
    counter (out-of-scope sport_keys) and a list of unmapped team names
    from otherwise in-scope rows."""
    by_sport_season: dict[tuple[str, str], list[Game]] = {}
    skipped = Counter()
    unmapped: list[str] = []

    for r in rows:
        sport, season_type = _resolve_sport(r["sport_key"])
        if sport is None:
            skipped[r["sport_key"]] += 1
            continue
        if sport not in sports:
            skipped[f"{r['sport_key']} (excluded by --sports)"] += 1
            continue

        home_id = names.team_id(sport, r["home_team"])
        away_id = names.team_id(sport, r["away_team"])
        if not home_id or not away_id:
            for who, team_id in ((r["home_team"], home_id), (r["away_team"], away_id)):
                if who and not team_id:
                    unmapped.append(f"{sport}: {who!r} (game_key {r['game_key']})")
            continue

        iso = _as_utc_iso(r["commence_time"])
        on = utc_to_eastern_date(iso)
        completed = r["completed"].strip().lower() == "true"
        away_score = r["away_score"] if completed and r["away_score"] not in ("", "NULL") else ""
        home_score = r["home_score"] if completed and r["home_score"] not in ("", "NULL") else ""

        game = Game(
            game_id=r["game_key"],
            season=default_season(sport, datetime.fromisoformat(on).date()) if on else "",
            season_type=season_type or "regular",
            date=on,
            start_time_utc=iso,
            status="final" if completed else "scheduled",
            away_team_id=away_id,
            home_team_id=home_id,
            away_abbrev=names.abbrev(sport, away_id),
            home_abbrev=names.abbrev(sport, home_id),
            away_score=away_score,
            home_score=home_score,
            venue="",
        )
        by_sport_season.setdefault((sport, game.season), []).append(game)

    return by_sport_season, skipped, unmapped


def build_odds_rows(rows: list[dict], sports: set[str], names: NameMaps
                    ) -> tuple[dict[str, list[dict]], Counter]:
    """Odds rows (nested bookmaker_data JSON, The Odds API's own shape) ->
    {sport: [flat odds row, ...]} matching pipeline.odds.normalize.ROW_FIELDS,
    one row per (bookmaker, market, outcome) — every entry in the JSON
    array is kept, since a game's bookmaker_data can hold more than one
    timestamped quote per bookmaker (price movement, not just the close)."""
    by_sport: dict[str, list[dict]] = {}
    skipped = Counter()

    for r in rows:
        sport, _ = _resolve_sport(r["sport_key"])
        if sport is None or sport not in sports:
            skipped[r["sport_key"]] += 1
            continue

        home_id = names.team_id(sport, r["home_team"])
        away_id = names.team_id(sport, r["away_team"])
        blob = json.loads(r["bookmaker_data"]) if r["bookmaker_data"] not in ("", "NULL", None) else []
        base = {
            "event_id": r["game_key"], "sport": sport,
            "commence_time": _as_utc_iso(r["commence_time"]),
            "home_team_id": home_id, "away_team_id": away_id,
            "home_name": r["home_team"], "away_name": r["away_team"],
        }
        out = by_sport.setdefault(sport, [])
        for bk in blob:
            for market in bk.get("markets") or []:
                mkey = market.get("key", "")
                for oc in market.get("outcomes") or []:
                    name = oc.get("name", "")
                    if mkey == "totals":
                        outcome = name.upper()
                    else:  # h2h, spreads: outcome is a team name
                        outcome = names.team_id(sport, name) or name
                    point = oc.get("point")
                    out.append({
                        **base,
                        "snapshot_ts": bk.get("last_update", ""),
                        "bookmaker": bk.get("key", ""),
                        "market": mkey,
                        "outcome": outcome,
                        "price_american": oc.get("price", ""),
                        "point": point if point is not None else "",
                    })

    return by_sport, skipped


def merge_games(sport: str, season: str, new_games: list[Game]) -> Path:
    """Upsert new_games into the season's games CSV by game_id."""
    path = ADAPTERS[sport].games_csv_path(season)
    existing: dict[str, Game] = {}
    if path.exists():
        existing = {g.game_id: g for g in core.read_games_csv(path, "n/a")}
    for g in new_games:
        existing[g.game_id] = g
    merged = sorted(existing.values(), key=lambda g: (g.date, g.game_id))
    return core.write_games_csv(merged, path)


def write_event_map_identity(sport: str, games: list[Game]) -> Path:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{
        "event_id": g.game_id, "game_id": g.game_id, "sport": sport,
        "commence_time": g.start_time_utc, "matched_on": "external_backfill_exact",
        "matched_at": now,
    } for g in games]
    return upsert_event_map(sport, rows)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.backfill_external",
                                description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scores", required=True, help="games_scores-shaped CSV export")
    p.add_argument("--odds", help="games_odds-shaped CSV export (optional)")
    p.add_argument("--sports", default=",".join(sorted(ADAPTERS)),
                   help=f"comma-separated subset (default: {','.join(sorted(ADAPTERS))})")
    p.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = p.parse_args(argv)

    sports = {s.strip().upper() for s in args.sports.split(",") if s.strip()}
    unknown = sorted(sports - set(ADAPTERS))
    if unknown:
        sys.exit(f"unknown sport(s) {', '.join(unknown)} — choose from {', '.join(sorted(ADAPTERS))}")

    names = NameMaps()

    with open(args.scores, newline="") as f:
        score_rows = list(csv.DictReader(f))
    by_sport_season, skipped_games, unmapped = build_games(score_rows, sports, names)

    total_games = sum(len(v) for v in by_sport_season.values())
    print(f"scores: {len(score_rows)} rows -> {total_games} games across "
          f"{len(by_sport_season)} sport/season file(s)")
    for (sport, season), games in sorted(by_sport_season.items()):
        finals = sum(1 for g in games if g.status == "final")
        print(f"  {sport} {season}: {len(games)} games ({finals} final)")
        if not args.dry_run:
            path = merge_games(sport, season, games)
            print(f"    -> {path}")
            write_event_map_identity(sport, games)

    if skipped_games:
        print(f"skipped (no team registry / excluded): "
              f"{sum(skipped_games.values())} rows across {len(skipped_games)} sport_key(s)")
        for key, n in skipped_games.most_common(10):
            print(f"  {key}: {n}")
    if unmapped:
        print(f"unmapped team names in otherwise in-scope rows ({len(unmapped)}):")
        for line in unmapped[:20]:
            print(f"  {line}")

    if args.odds:
        with open(args.odds, newline="") as f:
            odds_rows = list(csv.DictReader(f))
        by_sport_odds, skipped_odds = build_odds_rows(odds_rows, sports, names)
        total_odds = sum(len(v) for v in by_sport_odds.values())
        print(f"\nodds: {len(odds_rows)} rows -> {total_odds} normalized quote rows")
        for sport, rows in sorted(by_sport_odds.items()):
            print(f"  {sport}: {len(rows)} rows ({len({r['event_id'] for r in rows})} events)")
            if not args.dry_run:
                path = write_snapshot(sport, rows, "backfill-external")
                print(f"    -> {path}")
        if skipped_odds:
            print(f"skipped odds rows: {sum(skipped_odds.values())} "
                  f"across {len(skipped_odds)} sport_key(s)")


if __name__ == "__main__":
    main()
