#!/usr/bin/env python3
"""NHL data ingester — PLAN.md §2, `nhl_api` source adapter.

Source: the NHL's modern public API. There is no single league-wide
full-season feed, so this adapter pulls each club's season schedule:

    https://api-web.nhle.com/v1/club-schedule-season/{TRICODE}/{seasonId}

one unauthenticated GET per team (32 total, cached individually), then
dedupes games by id — every game appears in exactly two club feeds.

Raw responses are cached under data/raw/nhl/ before parsing; normalized
games land in data/nhl/games_<seasonId>.csv keyed by the canonical
team_ids from data/teams.csv — matched on the `nhl` external id first,
tricode second (teams.csv NHL abbrevs ARE the official tricodes). The
shared Game record, queries, and CLI live in pipeline/ingest/core.py —
this module owns only what is NHL-specific:

  * season ids span years — "20252026", derived by default_season()
  * OT/shootout outcome kept in the `ot` column (REG/OT/SO) — the NHL
    last-10 form multiplier in my_model.md is points-based (2xW + OTL),
    so team logs mark OT/SO losses as OTL (handled in core.team_log)
  * postponed / suspended / cancelled via gameScheduleState — never results
  * preseason exhibitions vs prospect squads skipped; playoffs parsed but
    excluded from regular-season logs

Usage (from the repo root):
    python3 -m pipeline.ingest.nhl fetch [--offline]     # 32 GETs + write CSV
    python3 -m pipeline.ingest.nhl today [--date YYYY-MM-DD]
    python3 -m pipeline.ingest.nhl scores [--team BOS] [--last 10]
    python3 -m pipeline.ingest.nhl common-opponents BOS TOR

NOTE: outbound requests to api-web.nhle.com are blocked from the Claude
Code sandbox this was authored in; `fetch` is meant to run on a normal
machine. Everything downstream of the raw files is covered by
tests/test_nhl_ingest.py.
"""

from __future__ import annotations

import csv
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from pipeline.ingest.core import (  # noqa: F401  (query names re-exported for tests/callers)
    EASTERN,
    REPO_ROOT,
    TEAMS_CSV,
    Game,
    HttpStatusError,
    UnmappedTeamError,
    common_opponents,
    completed_regular_season,
    http_get,
    make_parser,
    read_games_csv,
    run_query_command,
    team_log,
    todays_games,
    utc_to_eastern_date,
    write_games_csv,
)

SPORT = "NHL"
SCHEDULE_URL = "https://api-web.nhle.com/v1/club-schedule-season/{tricode}/{season}"
RAW_DIR = REPO_ROOT / "data" / "raw" / "nhl"
OUT_DIR = REPO_ROOT / "data" / "nhl"

GAME_TYPES = {1: "preseason", 2: "regular", 3: "playoffs"}

# gameState strings -> our status vocabulary (unknown states => scheduled,
# so a new state string never fabricates a result).
GAME_STATES = {
    "FUT": "scheduled",
    "PRE": "scheduled",
    "LIVE": "live",
    "CRIT": "live",
    "FINAL": "final",
    "OFF": "final",  # official final
}

# gameScheduleState values that override gameState — these are not results.
SCHEDULE_STATES = {"PPD": "postponed", "SUSP": "suspended", "CNCL": "cancelled"}


def load_team_maps() -> tuple[dict[int, tuple[str, str]], dict[str, str]]:
    """Two lookups from data/teams.csv NHL rows:
    by_nhl_id: NHL API teamId -> (team_id, abbrev); by_abbrev: tricode -> team_id.
    """
    by_nhl_id: dict[int, tuple[str, str]] = {}
    by_abbrev: dict[str, str] = {}
    with open(TEAMS_CSV, newline="") as f:
        for r in csv.DictReader(f):
            if r["sport"] != SPORT:
                continue
            by_abbrev[r["abbrev"]] = r["team_id"]
            nhl_id = json.loads(r["external_ids"]).get("nhl")
            if nhl_id is not None:
                by_nhl_id[int(nhl_id)] = (r["team_id"], r["abbrev"])
    return by_nhl_id, by_abbrev


def default_season(today: date | None = None) -> str:
    """NHL seasons span years: Oct 2025 – Jun 2026 is "20252026"."""
    today = today or datetime.now(EASTERN).date()
    start = today.year if today.month >= 8 else today.year - 1
    return f"{start}{start + 1}"


def fetch_raw(season: str, offline: bool = False) -> list[Path]:
    """Download every club's season schedule into data/raw/nhl/ and return
    the file paths. With offline=True, return the newest cached file per club.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    _, by_abbrev = load_team_maps()
    tricodes = sorted(by_abbrev)

    if offline:
        paths = []
        for tri in tricodes:
            cached = sorted(RAW_DIR.glob(f"club-schedule-season.{tri}.{season}.*.json"))
            if cached:
                paths.append(cached[-1])
        if not paths:
            sys.exit(f"no cached feeds in {RAW_DIR} — run `fetch` without --offline first")
        return paths

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths = []
    for tri in tricodes:
        try:
            body = http_get(SCHEDULE_URL.format(tricode=tri, season=season),
                            delays=(0, 2, 4, 8))
        except HttpStatusError as e:
            if e.status == 404:
                # Club didn't exist that season (UTA before 2024-25, SEA
                # before 2021-22, ARI after 2023-24) — expected in backfill.
                print(f"  {tri}: no {season} schedule (404) — skipped")
                continue
            raise
        path = RAW_DIR / f"club-schedule-season.{tri}.{season}.{stamp}.json"
        path.write_bytes(body)
        paths.append(path)
        time.sleep(0.3)  # politeness between the requests
    return paths


def _map_team(side: dict, by_nhl_id: dict, by_abbrev: dict) -> tuple[str, str] | None:
    """Resolve a feed team to (team_id, abbrev): numeric id first, tricode second."""
    mapped = by_nhl_id.get(side.get("id"))
    if mapped:
        return mapped
    tri = (side.get("abbrev") or "").upper()
    if tri in by_abbrev:
        return by_abbrev[tri], tri
    return None


def parse_games(docs: list[dict], by_nhl_id: dict, by_abbrev: dict) -> list[Game]:
    """Normalize one or more club feed documents into deduped Game rows.

    Every game appears in two club feeds — dedupe by game id. Unmapped
    teams are a hard error for regular season and playoffs (silent ID
    mismatches are the #1 source of bad data, PLAN.md §1) and skipped in
    preseason, where split-squad/prospect opponents can appear.
    """
    seen: set[str] = set()
    games: list[Game] = []
    for doc in docs:
        if not isinstance(doc, dict) or "games" not in doc:
            raise RuntimeError(
                "unexpected feed shape: missing top-level 'games' "
                "(inspect the cached raw files in data/raw/nhl/)"
            )
        for g in doc["games"]:
            gid = str(g.get("id", ""))
            if not gid or gid in seen:
                continue
            season_type = GAME_TYPES.get(g.get("gameType"), "other")
            away, home = g.get("awayTeam") or {}, g.get("homeTeam") or {}
            away_mapped = _map_team(away, by_nhl_id, by_abbrev)
            home_mapped = _map_team(home, by_nhl_id, by_abbrev)
            if away_mapped is None or home_mapped is None:
                if season_type in ("regular", "playoffs"):
                    bad = away if away_mapped is None else home
                    raise UnmappedTeamError(
                        f"game {gid}: team id {bad.get('id')} ({bad.get('abbrev')!r}) "
                        "not in data/teams.csv — add or fix the mapping"
                    )
                seen.add(gid)
                continue  # preseason exhibition opponent

            schedule_state = (g.get("gameScheduleState") or "OK").upper()
            status = SCHEDULE_STATES.get(
                schedule_state, GAME_STATES.get((g.get("gameState") or "").upper(), "scheduled")
            )
            is_final = status == "final"
            outcome = (g.get("gameOutcome") or {}).get("lastPeriodType") or ""
            seen.add(gid)
            games.append(
                Game(
                    game_id=gid,
                    season=str(g.get("season", "")),
                    season_type=season_type,
                    date=g.get("gameDate") or utc_to_eastern_date(g.get("startTimeUTC") or ""),
                    start_time_utc=g.get("startTimeUTC") or "",
                    status=status,
                    away_team_id=away_mapped[0],
                    home_team_id=home_mapped[0],
                    away_abbrev=away_mapped[1],
                    home_abbrev=home_mapped[1],
                    away_score=str(away.get("score", "")) if is_final else "",
                    home_score=str(home.get("score", "")) if is_final else "",
                    venue=(g.get("venue") or {}).get("default") or "",
                    ot=outcome if is_final else "",
                )
            )
    games.sort(key=lambda x: (x.date, x.game_id))
    return games


def games_csv_path(season: str) -> Path:
    return OUT_DIR / f"games_{season}.csv"


def past_seasons(back: int) -> list[str]:
    """The `back` seasons before the current one, newest first. The
    club-schedule-season endpoint serves any past seasonId directly."""
    start = int(default_season()[:4])
    return [f"{start - i}{start - i + 1}" for i in range(1, back + 1)]


def run_fetch(season: str | None = None, offline: bool = False) -> tuple[list[Game], Path]:
    """The backfill contract: fetch ANY season into its games CSV.
    Clubs that didn't exist that season 404 and are skipped; historic
    clubs (Arizona Coyotes) map via inactive data/teams.csv rows."""
    season = season or default_season()
    by_nhl_id, by_abbrev = load_team_maps()
    raws = fetch_raw(season, offline=offline)
    docs = [json.loads(p_.read_text()) for p_ in raws]
    games = parse_games(docs, by_nhl_id, by_abbrev)
    print(f"raw feeds: {len(raws)} files in {RAW_DIR}")
    path = write_games_csv(games, games_csv_path(season))
    return games, path


def main(argv: list[str] | None = None) -> None:
    p = make_parser(
        prog="python3 -m pipeline.ingest.nhl",
        description=__doc__,
        season_default=default_season(),
        season_help='NHL seasonId, e.g. "20252026" (any past season works too)',
    )
    args = p.parse_args(argv)
    by_nhl_id, by_abbrev = load_team_maps()

    if args.cmd == "fetch":
        games, path = run_fetch(args.season, offline=args.offline)
        finals = sum(1 for g in games if g.status == "final")
        print(f"wrote {len(games)} games -> {path} ({finals} final, {len(todays_games(games))} today)")
        return

    games = read_games_csv(games_csv_path(args.season), "python3 -m pipeline.ingest.nhl fetch")
    run_query_command(args, games, by_abbrev, SPORT, example="BOS")


if __name__ == "__main__":
    main()
