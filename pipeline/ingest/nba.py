#!/usr/bin/env python3
"""NBA data ingester — PLAN.md §2, `nba_statsapi` source adapter.

Source: the NBA's public CDN schedule feed
    https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json
One unauthenticated GET returns the entire season: every game (past and
upcoming) with tip-off times, status, and final scores for completed games.
Same feed family as the WNBA adapter (#3) — the WNBA feed is the WNBA copy
of this one. That single document covers all three needs of this milestone:

  * today's games          -> `today` command
  * this season's scores   -> `scores` command
  * common opponents       -> `common-opponents` command (my_model.md method 2 inputs)

Raw responses are cached under data/raw/nba/ before parsing (PLAN.md §2:
a re-run never needs to re-hit the API; history can be rebuilt offline).
Normalized games land in data/nba/games_<season>.csv keyed by the canonical
team_ids from data/teams.csv, matched on the `nba` external id (the
1610612xxx Stats API team ids), never on team names or tricodes.

NBA specifics handled here:
  * seasons span calendar years — the season key is "2025-26", not "2026"
  * play-in games (gameId type 5) parsed but kept out of regular-season logs
  * the NBA Cup final (gameId type 6) does NOT count in regular-season
    records — excluded from logs; Cup *group-stage* games are type 2 and
    count, which the gameId encoding gets right for free
  * preseason exhibitions vs non-NBA clubs and All-Star games are skipped
    (their team ids aren't in the registry)

Usage (from the repo root):
    python3 -m pipeline.ingest.nba fetch                 # download + write CSV
    python3 -m pipeline.ingest.nba fetch --offline       # reuse newest cached raw file
    python3 -m pipeline.ingest.nba today [--date YYYY-MM-DD]
    python3 -m pipeline.ingest.nba scores [--team BOS] [--last 10]
    python3 -m pipeline.ingest.nba common-opponents BOS NYK

NOTE: outbound requests to cdn.nba.com are blocked from the Claude Code
sandbox this was authored in; `fetch` is meant to run on a normal machine
(laptop, GitHub Actions). Everything downstream of the raw file is covered
by offline tests in tests/test_nba_ingest.py.

The query layer (team_log / common_opponents / todays_games) is deliberately
duplicated from the in-flight WNBA/MLB adapters — extract a shared core once
those PRs are merged rather than coupling open branches.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SPORT = "NBA"
SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
REPO_ROOT = Path(__file__).resolve().parents[2]
TEAMS_CSV = REPO_ROOT / "data" / "teams.csv"
RAW_DIR = REPO_ROOT / "data" / "raw" / "nba"
OUT_DIR = REPO_ROOT / "data" / "nba"
EASTERN = ZoneInfo("America/New_York")  # an NBA "day" is the US/Eastern date

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


@dataclass
class Game:
    game_id: str
    season: str          # e.g. "2025-26"
    season_type: str
    date: str            # US/Eastern date of tip-off, YYYY-MM-DD
    start_time_utc: str  # ISO 8601, empty if feed omitted it
    status: str          # scheduled | live | final
    away_team_id: str
    home_team_id: str
    away_abbrev: str
    home_abbrev: str
    away_score: str      # empty unless final
    home_score: str
    arena: str


CSV_FIELDS = [f.name for f in fields(Game)]


class UnmappedTeamError(RuntimeError):
    pass


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

    req = urllib.request.Request(SCHEDULE_URL, headers={"User-Agent": "basic-betting-model/0.1"})
    last_err: Exception | None = None
    for attempt, delay in enumerate((0, 2, 4, 8, 16)):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
            break
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            print(f"fetch attempt {attempt + 1} failed: {e}", file=sys.stderr)
    else:
        raise RuntimeError(f"could not fetch {SCHEDULE_URL}: {last_err}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_DIR / f"scheduleLeagueV2.{stamp}.json"
    path.write_bytes(body)
    return path


def _eastern_date(game: dict) -> str:
    """The US/Eastern calendar date a game belongs to."""
    utc = game.get("gameDateTimeUTC")
    if utc:
        dt = datetime.fromisoformat(utc.replace("Z", "+00:00"))
        return dt.astimezone(EASTERN).date().isoformat()
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
                    bad_tri = (away if away_mapped is None else home).get("teamTricode")
                    bad_id = (away if away_mapped is None else home).get("teamId")
                    raise UnmappedTeamError(
                        f"game {gid}: teamId {bad_id} ({bad_tri!r}) not in "
                        "data/teams.csv external_ids — add or fix the mapping"
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
                    arena=g.get("arenaName") or "",
                )
            )
    games.sort(key=lambda x: (x.date, x.game_id))
    return games


def games_csv_path(season: str) -> Path:
    return OUT_DIR / f"games_{season}.csv"


def write_games_csv(games: list[Game]) -> Path:
    if not games:
        raise RuntimeError("no games parsed — refusing to write an empty CSV")
    path = games_csv_path(games[0].season)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(vars(g) for g in games)
    return path


def read_games_csv(season: str) -> list[Game]:
    path = games_csv_path(season)
    if not path.exists():
        sys.exit(f"{path} not found — run `python3 -m pipeline.ingest.nba fetch` first")
    with open(path, newline="") as f:
        return [Game(**row) for row in csv.DictReader(f)]


# ---------------------------------------------------------------- queries

def todays_games(games: list[Game], on: date | None = None) -> list[Game]:
    on = on or datetime.now(EASTERN).date()
    return [g for g in games if g.date == on.isoformat()]


def completed_regular_season(games: list[Game]) -> list[Game]:
    return [
        g for g in games
        if g.status == "final" and g.season_type == "regular" and g.away_score and g.home_score
    ]


def team_log(games: list[Game], team_id: str) -> list[dict]:
    """Completed regular-season games for one team, one row per game
    (PLAN.md game_logs shape: opponent, venue, scored, allowed, result)."""
    log = []
    for g in completed_regular_season(games):
        if team_id == g.home_team_id:
            scored, allowed, opp, is_home = int(g.home_score), int(g.away_score), g.away_team_id, True
        elif team_id == g.away_team_id:
            scored, allowed, opp, is_home = int(g.away_score), int(g.home_score), g.home_team_id, False
        else:
            continue
        log.append({
            "game_id": g.game_id, "date": g.date, "opponent_id": opp,
            "is_home": is_home, "scored": scored, "allowed": allowed,
            "result": "W" if scored > allowed else "L",
        })
    return log


def common_opponents(games: list[Game], team_a: str, team_b: str) -> dict:
    """Common-opponent inputs for my_model.md method 2.

    Opponents both teams have faced this regular season (excluding each
    other), each team's per-opponent averages, and the aggregates weighted
    by games played against each opponent (wRF/wRA).
    """
    log_a, log_b = team_log(games, team_a), team_log(games, team_b)
    opps_a = {r["opponent_id"] for r in log_a} - {team_b}
    opps_b = {r["opponent_id"] for r in log_b} - {team_a}
    common = sorted(opps_a & opps_b)

    def side(log: list[dict]) -> dict:
        rows = {}
        total_games = total_scored = total_allowed = 0
        for opp in common:
            vs = [r for r in log if r["opponent_id"] == opp]
            n = len(vs)
            scored, allowed = sum(r["scored"] for r in vs), sum(r["allowed"] for r in vs)
            rows[opp] = {
                "games": n,
                "avg_scored": round(scored / n, 2),
                "avg_allowed": round(allowed / n, 2),
            }
            total_games += n
            total_scored += scored
            total_allowed += allowed
        return {
            "per_opponent": rows,
            "games": total_games,
            "wRF": round(total_scored / total_games, 4) if total_games else None,
            "wRA": round(total_allowed / total_games, 4) if total_games else None,
        }

    return {"common_opponents": common, team_a: side(log_a), team_b: side(log_b)}


# ------------------------------------------------------------------- CLI

def _resolve_team(token: str, by_abbrev: dict[str, str]) -> str:
    if token in by_abbrev.values():
        return token
    team_id = by_abbrev.get(token.upper())
    if team_id is None:
        sys.exit(f"unknown NBA team {token!r} — use an abbrev from data/teams.csv (e.g. BOS)")
    return team_id


def _print_games(games: list[Game]) -> None:
    for g in games:
        when = g.start_time_utc or g.date
        score = f"{g.away_score}-{g.home_score}" if g.status == "final" else g.status
        print(f"{g.date}  {g.away_abbrev:>4} @ {g.home_abbrev:<4} {score:>10}  {when}  {g.arena}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.ingest.nba", description=__doc__)
    p.add_argument("--season", default=default_season(), help='e.g. "2025-26"')
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download the schedule feed and write games CSV")
    f.add_argument("--offline", action="store_true", help="parse the newest cached raw file")

    t = sub.add_parser("today", help="show games on a date (default: today, US/Eastern)")
    t.add_argument("--date", help="YYYY-MM-DD")

    s = sub.add_parser("scores", help="completed regular-season games")
    s.add_argument("--team", help="filter by abbrev, e.g. BOS")
    s.add_argument("--last", type=int, default=0, help="only the most recent N")

    c = sub.add_parser("common-opponents", help="common-opponent breakdown for two teams")
    c.add_argument("team_a")
    c.add_argument("team_b")

    args = p.parse_args(argv)
    by_nba_id, by_abbrev = load_team_maps()

    if args.cmd == "fetch":
        raw = fetch_raw(offline=args.offline)
        doc = json.loads(raw.read_text())
        games = parse_games(doc, by_nba_id)
        path = write_games_csv(games)
        finals = sum(1 for g in games if g.status == "final")
        today = len(todays_games(games))
        print(f"raw feed: {raw}")
        print(f"wrote {len(games)} games -> {path} ({finals} final, {today} today)")
        return

    games = read_games_csv(args.season)
    if args.cmd == "today":
        on = date.fromisoformat(args.date) if args.date else None
        picked = todays_games(games, on)
        _print_games(picked)
        if not picked:
            print("no games")
    elif args.cmd == "scores":
        finals = completed_regular_season(games)
        if args.team:
            team_id = _resolve_team(args.team, by_abbrev)
            finals = [g for g in finals if team_id in (g.away_team_id, g.home_team_id)]
        if args.last:
            finals = finals[-args.last:]
        _print_games(finals)
    elif args.cmd == "common-opponents":
        a, b = _resolve_team(args.team_a, by_abbrev), _resolve_team(args.team_b, by_abbrev)
        print(json.dumps(common_opponents(games, a, b), indent=2))


if __name__ == "__main__":
    main()
