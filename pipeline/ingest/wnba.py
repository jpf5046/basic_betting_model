#!/usr/bin/env python3
"""WNBA data ingester — PLAN.md §2 source adapter.

Source: the WNBA's public CDN schedule feed
    https://cdn.wnba.com/static/json/staticData/scheduleLeagueV2_1.json
One unauthenticated GET returns the entire season: every game (past and
upcoming) with tip-off times, status, and final scores for completed games.

Raw responses are cached under data/raw/wnba/ before parsing; normalized
games land in data/wnba/games_<season>.csv keyed by the canonical team_ids
from data/teams.csv. The shared Game record, queries (today / scores /
common-opponents), and CLI live in pipeline/ingest/core.py — this module
owns only what is WNBA-specific: the feed URL, tricode aliasing, and feed
parsing.

Usage (from the repo root):
    python3 -m pipeline.ingest.wnba fetch [--offline]
    python3 -m pipeline.ingest.wnba today [--date YYYY-MM-DD]
    python3 -m pipeline.ingest.wnba scores [--team NYL] [--last 10]
    python3 -m pipeline.ingest.wnba common-opponents NYL LVA

NOTE: outbound requests to cdn.wnba.com are blocked from the Claude Code
sandbox this was authored in; `fetch` is meant to run on a normal machine.
Everything downstream of the raw file is covered by tests/test_wnba_ingest.py.
"""

from __future__ import annotations

import csv
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

SPORT = "WNBA"
SCHEDULE_URL = "https://cdn.wnba.com/static/json/staticData/scheduleLeagueV2_1.json"
RAW_DIR = REPO_ROOT / "data" / "raw" / "wnba"
OUT_DIR = REPO_ROOT / "data" / "wnba"

# Feed tricodes seen across NBA-family feeds -> abbrev used in data/teams.csv.
TRICODE_ALIASES = {
    "LV": "LVA",
    "LA": "LAS",
    "NY": "NYL",
    "CONN": "CON",
    "PHO": "PHX",
    "GS": "GSV",
    "GSW": "GSV",
    "WSH": "WAS",
}

# 3rd digit of a gameId ("10<T>YY#####") is the season type.
SEASON_TYPES = {"1": "preseason", "2": "regular", "3": "allstar", "4": "playoffs", "5": "playin"}

GAME_STATUS = {1: "scheduled", 2: "live", 3: "final"}


def load_team_map() -> dict[str, str]:
    """abbrev -> team_id for WNBA rows of data/teams.csv, plus feed aliases."""
    with open(TEAMS_CSV, newline="") as f:
        by_abbrev = {
            r["abbrev"]: r["team_id"] for r in csv.DictReader(f) if r["sport"] == SPORT
        }
    out = dict(by_abbrev)
    for alias, canonical in TRICODE_ALIASES.items():
        if canonical in by_abbrev:
            out.setdefault(alias, by_abbrev[canonical])
    return out


def fetch_raw(offline: bool = False) -> Path:
    """Download the schedule feed into data/raw/wnba/ and return the file path.

    With offline=True, return the newest previously cached file instead.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if offline:
        cached = sorted(RAW_DIR.glob("scheduleLeagueV2_1.*.json"))
        if not cached:
            sys.exit(f"no cached feed in {RAW_DIR} — run `fetch` without --offline first")
        return cached[-1]

    body = http_get(SCHEDULE_URL)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_DIR / f"scheduleLeagueV2_1.{stamp}.json"
    path.write_bytes(body)
    return path


def _eastern_date(game: dict) -> str:
    """The US/Eastern calendar date a game belongs to."""
    if game.get("gameDateTimeUTC"):
        return utc_to_eastern_date(game["gameDateTimeUTC"])
    # Fallback: gameDateEst's date component is already the Eastern date.
    est = game.get("gameDateEst") or game.get("gameDateTimeEst") or ""
    return est[:10]


def parse_games(doc: dict, team_map: dict[str, str]) -> list[Game]:
    """Normalize the feed document into Game rows.

    Games whose teams can't be mapped are skipped for non-regular-season
    play (All-Star exhibitions use synthetic tricodes) but are a hard error
    in the regular season / playoffs — silent name mismatches are the #1
    source of bad data (PLAN.md §1).
    """
    try:
        schedule = doc["leagueSchedule"]
        game_dates = schedule["gameDates"]
    except (KeyError, TypeError):
        raise RuntimeError(
            "unexpected feed shape: missing leagueSchedule.gameDates "
            "(inspect the cached raw file in data/raw/wnba/)"
        )
    season = str(schedule.get("seasonYear", ""))[:4]

    games: list[Game] = []
    for day in game_dates:
        for g in day.get("games", []):
            gid = str(g.get("gameId", ""))
            season_type = SEASON_TYPES.get(gid[2:3], "other")
            away, home = g.get("awayTeam") or {}, g.get("homeTeam") or {}
            away_tri = (away.get("teamTricode") or "").upper()
            home_tri = (home.get("teamTricode") or "").upper()
            away_id, home_id = team_map.get(away_tri), team_map.get(home_tri)
            if away_id is None or home_id is None:
                if season_type in ("regular", "playoffs", "playin"):
                    raise UnmappedTeamError(
                        f"game {gid}: unmapped tricode "
                        f"{away_tri if away_id is None else home_tri!r} "
                        f"(feed teamIds {away.get('teamId')}/{home.get('teamId')}) — "
                        "add the team or an alias to data/teams.csv / TRICODE_ALIASES"
                    )
                continue  # preseason/all-star exhibition with synthetic teams

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
                    away_team_id=away_id,
                    home_team_id=home_id,
                    away_abbrev=away_tri,
                    home_abbrev=home_tri,
                    away_score=str(away.get("score", "")) if is_final else "",
                    home_score=str(home.get("score", "")) if is_final else "",
                    venue=g.get("arenaName") or "",
                )
            )
    # Normalize aliased abbrevs to the canonical ones from teams.csv.
    id_to_abbrev = {v: k for k, v in load_team_map().items() if k not in TRICODE_ALIASES}
    for game in games:
        game.away_abbrev = id_to_abbrev.get(game.away_team_id, game.away_abbrev)
        game.home_abbrev = id_to_abbrev.get(game.home_team_id, game.home_abbrev)
    games.sort(key=lambda x: (x.date, x.game_id))
    return games


def games_csv_path(season: str) -> Path:
    return OUT_DIR / f"games_{season}.csv"


def main(argv: list[str] | None = None) -> None:
    p = make_parser(
        prog="python3 -m pipeline.ingest.wnba",
        description=__doc__,
        season_default=str(datetime.now(EASTERN).year),
        season_help="season year, e.g. 2026",
    )
    args = p.parse_args(argv)
    team_map = load_team_map()

    if args.cmd == "fetch":
        raw = fetch_raw(offline=args.offline)
        doc = read_feed_json(raw)
        games = parse_games(doc, team_map)
        path = write_games_csv(games, games_csv_path(games[0].season))
        finals = sum(1 for g in games if g.status == "final")
        print(f"raw feed: {raw}")
        print(f"wrote {len(games)} games -> {path} ({finals} final, {len(todays_games(games))} today)")
        return

    games = read_games_csv(games_csv_path(args.season), "python3 -m pipeline.ingest.wnba fetch")
    run_query_command(args, games, team_map, SPORT, example="NYL")


if __name__ == "__main__":
    main()
