#!/usr/bin/env python3
"""MLB data ingester — PLAN.md §2, `mlb_statsapi` source adapter.

Source: the official MLB Stats API schedule endpoint
    https://statsapi.mlb.com/api/v1/schedule?sportId=1&season=<YYYY>&gameTypes=R,F,D,W,L
One unauthenticated GET returns the whole season — every game past and
upcoming, with start times, status, and final scores for completed games.

Raw responses are cached under data/raw/mlb/ before parsing; normalized
games land in data/mlb/games_<season>.csv keyed by the canonical team_ids
from data/teams.csv (matched on the `mlb` external id, never on team
names). The shared Game record, queries, and CLI live in
pipeline/ingest/core.py — this module owns only what is MLB-specific:

  * doubleheaders — both games kept, distinguished by game_number/doubleheader
  * postponed / suspended / cancelled detection via detailedState (the
    API marks postponed games abstract-"Final")
  * spring training (gameType S) excluded from logs; the fetch default
    requests regular season + postseason only
  * officialDate used as the game's calendar day

Usage (from the repo root):
    python3 -m pipeline.ingest.mlb fetch [--season 2026] [--offline]
    python3 -m pipeline.ingest.mlb today [--date YYYY-MM-DD]
    python3 -m pipeline.ingest.mlb scores [--team NYY] [--last 10]
    python3 -m pipeline.ingest.mlb common-opponents NYY BOS

NOTE: outbound requests to statsapi.mlb.com are blocked from the Claude
Code sandbox this was authored in; `fetch` is meant to run on a normal
machine. Everything downstream of the raw file is covered by
tests/test_mlb_ingest.py.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.ingest.core import (  # noqa: F401  (query names re-exported for tests/callers)
    EASTERN,
    REPO_ROOT,
    TEAMS_CSV,
    Game,
    UnmappedTeamError,
    common_opponents,
    completed_regular_season,
    http_get,
    make_parser,
    read_feed_json,
    read_games_csv,
    run_query_command,
    team_log,
    todays_games,
    utc_to_eastern_date,
    write_games_csv,
)

SPORT = "MLB"
SCHEDULE_URL = (
    "https://statsapi.mlb.com/api/v1/schedule"
    "?sportId=1&season={season}&gameTypes=R,F,D,W,L"
)
RAW_DIR = REPO_ROOT / "data" / "raw" / "mlb"
OUT_DIR = REPO_ROOT / "data" / "mlb"

# statsapi gameType -> our season_type vocabulary
GAME_TYPES = {
    "S": "preseason",   # spring training
    "E": "exhibition",
    "A": "allstar",
    "R": "regular",
    "F": "playoffs",    # wild card
    "D": "playoffs",    # division series
    "L": "playoffs",    # league championship
    "W": "playoffs",    # world series
}

# detailedState prefixes that override the abstract state — these games are
# NOT results even though the API's abstractGameState may read "Final".
NON_RESULT_STATES = ("Postponed", "Suspended", "Cancelled")

ABSTRACT_STATES = {"Preview": "scheduled", "Live": "live", "Final": "final"}


def load_team_maps() -> tuple[dict[int, tuple[str, str]], dict[str, str]]:
    """Two lookups from data/teams.csv MLB rows:
    by_mlb_id: statsapi teamId -> (team_id, abbrev); by_abbrev: abbrev -> team_id.
    """
    by_mlb_id: dict[int, tuple[str, str]] = {}
    by_abbrev: dict[str, str] = {}
    with open(TEAMS_CSV, newline="") as f:
        for r in csv.DictReader(f):
            if r["sport"] != SPORT:
                continue
            by_abbrev[r["abbrev"]] = r["team_id"]
            mlb_id = json.loads(r["external_ids"]).get("mlb")
            if mlb_id is not None:
                by_mlb_id[int(mlb_id)] = (r["team_id"], r["abbrev"])
    return by_mlb_id, by_abbrev


def fetch_raw(season: str, offline: bool = False) -> Path:
    """Download the season schedule into data/raw/mlb/ and return the path.

    With offline=True, return the newest previously cached file instead.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if offline:
        cached = sorted(RAW_DIR.glob(f"schedule_{season}.*.json"))
        if not cached:
            sys.exit(f"no cached feed in {RAW_DIR} — run `fetch` without --offline first")
        return cached[-1]

    body = http_get(SCHEDULE_URL.format(season=season), timeout=60)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_DIR / f"schedule_{season}.{stamp}.json"
    path.write_bytes(body)
    return path


def _status(game: dict) -> str:
    st = game.get("status") or {}
    detailed = st.get("detailedState") or ""
    for prefix in NON_RESULT_STATES:
        if detailed.startswith(prefix):
            return prefix.lower()
    return ABSTRACT_STATES.get(st.get("abstractGameState"), "scheduled")


def _official_date(game: dict) -> str:
    """officialDate is the local scheduled date; fall back to Eastern."""
    return game.get("officialDate") or utc_to_eastern_date(game.get("gameDate") or "")


def parse_games(doc: dict, by_mlb_id: dict[int, tuple[str, str]]) -> list[Game]:
    """Normalize the schedule document into Game rows.

    Unmapped statsapi team ids are a hard error for regular season and
    playoffs (silent ID mismatches are the #1 source of bad data, PLAN.md
    §1), and skipped for exhibition-type games (all-star squads etc.).
    """
    if not isinstance(doc, dict) or "dates" not in doc:
        raise RuntimeError(
            "unexpected feed shape: missing top-level 'dates' "
            "(inspect the cached raw file in data/raw/mlb/)"
        )

    games: list[Game] = []
    for day in doc["dates"]:
        for g in day.get("games", []):
            season_type = GAME_TYPES.get(g.get("gameType", ""), "other")
            away = (g.get("teams") or {}).get("away") or {}
            home = (g.get("teams") or {}).get("home") or {}
            away_mapped = by_mlb_id.get((away.get("team") or {}).get("id"))
            home_mapped = by_mlb_id.get((home.get("team") or {}).get("id"))
            if away_mapped is None or home_mapped is None:
                if season_type in ("regular", "playoffs"):
                    bad = (away if away_mapped is None else home).get("team", {}).get("id")
                    raise UnmappedTeamError(
                        f"game {g.get('gamePk')}: statsapi team id {bad} not in "
                        "data/teams.csv external_ids — add or fix the mapping"
                    )
                continue

            status = _status(g)
            is_final = status == "final"
            away_score, home_score = away.get("score"), home.get("score")
            games.append(
                Game(
                    game_id=str(g.get("gamePk", "")),
                    season=str(g.get("season", "")),
                    season_type=season_type,
                    date=_official_date(g),
                    start_time_utc=g.get("gameDate") or "",
                    status=status,
                    away_team_id=away_mapped[0],
                    home_team_id=home_mapped[0],
                    away_abbrev=away_mapped[1],
                    home_abbrev=home_mapped[1],
                    away_score=str(away_score) if is_final and away_score is not None else "",
                    home_score=str(home_score) if is_final and home_score is not None else "",
                    venue=(g.get("venue") or {}).get("name") or "",
                    doubleheader=g.get("doubleHeader") or "N",
                    game_number=str(g.get("gameNumber") or 1),
                )
            )
    games.sort(key=lambda x: (x.date, x.start_time_utc, x.game_id))
    return games


def games_csv_path(season: str) -> Path:
    return OUT_DIR / f"games_{season}.csv"


def main(argv: list[str] | None = None) -> None:
    p = make_parser(
        prog="python3 -m pipeline.ingest.mlb",
        description=__doc__,
        season_default=str(datetime.now(EASTERN).year),
        season_help="season year, e.g. 2026",
    )
    args = p.parse_args(argv)
    by_mlb_id, by_abbrev = load_team_maps()

    if args.cmd == "fetch":
        raw = fetch_raw(args.season, offline=args.offline)
        doc = read_feed_json(raw)
        games = parse_games(doc, by_mlb_id)
        path = write_games_csv(games, games_csv_path(games[0].season))
        finals = sum(1 for g in games if g.status == "final")
        print(f"raw feed: {raw}")
        print(f"wrote {len(games)} games -> {path} ({finals} final, {len(todays_games(games))} today)")
        return

    games = read_games_csv(games_csv_path(args.season), "python3 -m pipeline.ingest.mlb fetch")
    run_query_command(args, games, by_abbrev, SPORT, example="NYY")


if __name__ == "__main__":
    main()
