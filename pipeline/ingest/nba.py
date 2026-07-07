#!/usr/bin/env python3
"""NBA data ingester — PLAN.md §2, `nba_statsapi` source adapter.

Source: the NBA's public CDN schedule feed
    https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json
One unauthenticated GET returns the entire season: every game (past and
upcoming) with tip-off times, status, and final scores for completed games.
Same feed family as the WNBA adapter — the WNBA feed is the WNBA copy of
this one.

Raw responses are cached under data/raw/nba/ before parsing; normalized
games land in data/nba/games_<season>.csv keyed by the canonical team_ids
from data/teams.csv, matched on the `nba` external id (the 1610612xxx
Stats API team ids), never on names or tricodes. The shared Game record,
queries, and CLI live in pipeline/ingest/core.py — this module owns only
what is NBA-specific:

  * seasons span calendar years — the season key is "2025-26"
  * play-in games (gameId type 5) kept out of regular-season logs
  * the NBA Cup final (gameId type 6) carries no standings credit —
    excluded from logs; Cup group-stage games are type 2 and count
  * preseason exhibitions vs non-NBA clubs and All-Star games skipped

Usage (from the repo root):
    python3 -m pipeline.ingest.nba fetch [--offline]
    python3 -m pipeline.ingest.nba today [--date YYYY-MM-DD]
    python3 -m pipeline.ingest.nba scores [--team BOS] [--last 10]
    python3 -m pipeline.ingest.nba common-opponents BOS NYK

NOTE: outbound requests to cdn.nba.com are blocked from the Claude Code
sandbox this was authored in; `fetch` is meant to run on a normal machine.
Everything downstream of the raw file is covered by tests/test_nba_ingest.py.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime, timezone
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

SPORT = "NBA"
SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
RAW_DIR = REPO_ROOT / "data" / "raw" / "nba"
OUT_DIR = REPO_ROOT / "data" / "nba"

# 3rd digit of a gameId ("00<T>YY#####") is the season type.
SEASON_TYPES = {
    "1": "preseason",
    "2": "regular",       # includes NBA Cup group-stage games (they count)
    "3": "allstar",
    "4": "playoffs",
    "5": "playin",
    "6": "nbacup_final",  # Cup championship game — does NOT count in records
}

GAME_STATUS = {1: "scheduled", 2: "live", 3: "final"}


def load_team_maps() -> tuple[dict[int, tuple[str, str]], dict[str, str]]:
    """Two lookups from data/teams.csv NBA rows:
    by_nba_id: Stats API teamId -> (team_id, abbrev); by_abbrev: abbrev -> team_id.
    """
    by_nba_id: dict[int, tuple[str, str]] = {}
    by_abbrev: dict[str, str] = {}
    with open(TEAMS_CSV, newline="") as f:
        for r in csv.DictReader(f):
            if r["sport"] != SPORT:
                continue
            by_abbrev[r["abbrev"]] = r["team_id"]
            nba_id = json.loads(r["external_ids"]).get("nba")
            if nba_id is not None:
                by_nba_id[int(nba_id)] = (r["team_id"], r["abbrev"])
    return by_nba_id, by_abbrev


def default_season(today: date | None = None) -> str:
    """NBA seasons span years: Oct 2025 – Jun 2026 is "2025-26"."""
    today = today or datetime.now(EASTERN).date()
    start = today.year if today.month >= 9 else today.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def fetch_raw(offline: bool = False) -> Path:
    """Download the schedule feed into data/raw/nba/ and return the file path.

    With offline=True, return the newest previously cached file instead.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if offline:
        cached = sorted(RAW_DIR.glob("scheduleLeagueV2.*.json"))
        if not cached:
            sys.exit(f"no cached feed in {RAW_DIR} — run `fetch` without --offline first")
        return cached[-1]

    body = http_get(SCHEDULE_URL, timeout=60)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_DIR / f"scheduleLeagueV2.{stamp}.json"
    path.write_bytes(body)
    return path


def _eastern_date(game: dict) -> str:
    """The US/Eastern calendar date a game belongs to."""
    if game.get("gameDateTimeUTC"):
        return utc_to_eastern_date(game["gameDateTimeUTC"])
    # Fallback: gameDateEst's date component is already the Eastern date.
    est = game.get("gameDateEst") or game.get("gameDateTimeEst") or ""
    return est[:10]


def parse_games(doc: dict, by_nba_id: dict[int, tuple[str, str]]) -> list[Game]:
    """Normalize the feed document into Game rows.

    Unmapped team ids are a hard error for regular season, play-in, and
    playoffs (silent ID mismatches are the #1 source of bad data, PLAN.md
    §1) and skipped otherwise — preseason exhibitions vs international
    clubs and All-Star squads aren't NBA franchises.
    """
    try:
        schedule = doc["leagueSchedule"]
        game_dates = schedule["gameDates"]
    except (KeyError, TypeError):
        raise RuntimeError(
            "unexpected feed shape: missing leagueSchedule.gameDates "
            "(inspect the cached raw file in data/raw/nba/)"
        )
    season = str(schedule.get("seasonYear", ""))

    games: list[Game] = []
    for day in game_dates:
        for g in day.get("games", []):
            gid = str(g.get("gameId", ""))
            season_type = SEASON_TYPES.get(gid[2:3], "other")
            away, home = g.get("awayTeam") or {}, g.get("homeTeam") or {}
            away_mapped = by_nba_id.get(away.get("teamId"))
            home_mapped = by_nba_id.get(home.get("teamId"))
            if away_mapped is None or home_mapped is None:
                if season_type in ("regular", "playin", "playoffs", "nbacup_final"):
                    bad = away if away_mapped is None else home
                    raise UnmappedTeamError(
                        f"game {gid}: teamId {bad.get('teamId')} ({bad.get('teamTricode')!r}) "
                        "not in data/teams.csv external_ids — add or fix the mapping"
                    )
                continue  # preseason exhibition / All-Star squad

            status = GAME_STATUS.get(g.get("gameStatus"), "scheduled")
            is_final = status == "final"
            games.append(
                Game(
                    game_id=gid,
                    season=season,
                    season_type=season_type,
                    date=_eastern_date(g),
                    start_time_utc=g.get("gameDateTimeUTC") or "",
                    status=status,
                    away_team_id=away_mapped[0],
                    home_team_id=home_mapped[0],
                    away_abbrev=away_mapped[1],
                    home_abbrev=home_mapped[1],
                    away_score=str(away.get("score", "")) if is_final else "",
                    home_score=str(home.get("score", "")) if is_final else "",
                    venue=g.get("arenaName") or "",
                )
            )
    games.sort(key=lambda x: (x.date, x.game_id))
    return games


def games_csv_path(season: str) -> Path:
    return OUT_DIR / f"games_{season}.csv"


def main(argv: list[str] | None = None) -> None:
    p = make_parser(
        prog="python3 -m pipeline.ingest.nba",
        description=__doc__,
        season_default=default_season(),
        season_help='e.g. "2025-26"',
    )
    args = p.parse_args(argv)
    by_nba_id, by_abbrev = load_team_maps()

    if args.cmd == "fetch":
        raw = fetch_raw(offline=args.offline)
        doc = read_feed_json(raw)
        games = parse_games(doc, by_nba_id)
        path = write_games_csv(games, games_csv_path(games[0].season))
        finals = sum(1 for g in games if g.status == "final")
        print(f"raw feed: {raw}")
        print(f"wrote {len(games)} games -> {path} ({finals} final, {len(todays_games(games))} today)")
        return

    games = read_games_csv(games_csv_path(args.season), "python3 -m pipeline.ingest.nba fetch")
    run_query_command(args, games, by_abbrev, SPORT, example="BOS")


if __name__ == "__main__":
    main()
