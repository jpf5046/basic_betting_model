#!/usr/bin/env python3
"""Shared core for the sport ingest adapters (PLAN.md §2).

Everything here was duplicated across pipeline/ingest/{wnba,mlb,nba,nhl}.py
while those adapters were separate in-flight PRs; this module is the
promised extraction. An adapter now owns only what is genuinely
sport-specific — feed URLs, team-mapping rules, feed parsing, season-id
quirks — and gets from here:

  * the unified Game record (one schema for every sport's games CSV,
    matching the future `games` table in PLAN.md §1)
  * games CSV read/write
  * HTTP GET with retry/backoff
  * the query layer: todays_games / completed_regular_season / team_log /
    common_opponents (my_model.md method 2 inputs)
  * the shared CLI: fetch / today / scores / common-opponents

Behavioral notes baked in from the individual adapters:
  * scores exist only on finals; postponed/suspended/cancelled are never
    results
  * team logs cover completed REGULAR SEASON games only
  * NHL OT/SO losses are marked OTL (the sport's last-10 form multiplier
    is points-based); for sports without an `ot` outcome the field is
    empty and results are plain W/L
  * common opponents exclude head-to-head games and weight each opponent
    by games played against it (the wRF/wRA aggregates)
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
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")  # a game "day" is the US/Eastern date

REPO_ROOT = Path(__file__).resolve().parents[2]
TEAMS_CSV = REPO_ROOT / "data" / "teams.csv"


class UnmappedTeamError(RuntimeError):
    """A feed team could not be resolved to a data/teams.csv team_id."""


@dataclass
class Game:
    """One row per game — a single schema shared by every sport.

    Sport-specific fields default to neutral values: `ot` (NHL) is empty
    unless a final ended in OT/SO; `doubleheader`/`game_number` (MLB)
    default to N/1.
    """

    game_id: str
    season: str
    season_type: str     # preseason | regular | playoffs | allstar | ...
    date: str            # local calendar date of the game, YYYY-MM-DD
    start_time_utc: str  # ISO 8601, empty if the feed omitted it
    status: str          # scheduled | live | final | postponed | suspended | cancelled
    away_team_id: str
    home_team_id: str
    away_abbrev: str
    home_abbrev: str
    away_score: str      # empty unless final
    home_score: str
    venue: str
    ot: str = ""             # REG | OT | SO, empty unless final (NHL)
    doubleheader: str = "N"  # N | Y | S (MLB)
    game_number: str = "1"   # game 1 or 2 of a doubleheader day (MLB)


CSV_FIELDS = [f.name for f in fields(Game)]


# ------------------------------------------------------------------ I/O

def http_get(url: str, timeout: int = 30, delays: tuple = (0, 2, 4, 8, 16)) -> bytes:
    """GET with exponential-backoff retries; raises after the last attempt."""
    req = urllib.request.Request(url, headers={"User-Agent": "basic-betting-model/0.1"})
    last_err: Exception | None = None
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            print(f"fetch attempt {attempt + 1} failed for {url}: {e}", file=sys.stderr)
    raise RuntimeError(f"could not fetch {url}: {last_err}")


def utc_to_eastern_date(utc_iso: str) -> str:
    """US/Eastern calendar date of an ISO UTC timestamp ('' passes through)."""
    if not utc_iso:
        return ""
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    return dt.astimezone(EASTERN).date().isoformat()


def write_games_csv(games: list[Game], path: Path) -> Path:
    if not games:
        raise RuntimeError("no games parsed — refusing to write an empty CSV")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(vars(g) for g in games)
    return path


def read_games_csv(path: Path, fetch_cmd: str) -> list[Game]:
    if not path.exists():
        sys.exit(f"{path} not found — run `{fetch_cmd}` first")
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
    (PLAN.md game_logs shape: opponent, venue, scored, allowed, result,
    ot_flag)."""
    log = []
    for g in completed_regular_season(games):
        if team_id == g.home_team_id:
            scored, allowed, opp, is_home = int(g.home_score), int(g.away_score), g.away_team_id, True
        elif team_id == g.away_team_id:
            scored, allowed, opp, is_home = int(g.away_score), int(g.home_score), g.home_team_id, False
        else:
            continue
        won = scored > allowed
        # NHL point system: a loss in OT/SO is an OTL, worth one point.
        result = "W" if won else ("OTL" if g.ot in ("OT", "SO") else "L")
        log.append({
            "game_id": g.game_id, "date": g.date, "opponent_id": opp,
            "is_home": is_home, "scored": scored, "allowed": allowed,
            "result": result, "ot": g.ot,
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

def resolve_team(token: str, by_abbrev: dict[str, str], sport: str, example: str) -> str:
    """Accept a team_id or an abbrev (or feed alias) and return the team_id."""
    if token in by_abbrev.values():
        return token
    team_id = by_abbrev.get(token.upper())
    if team_id is None:
        sys.exit(
            f"unknown {sport} team {token!r} — use an abbrev from data/teams.csv (e.g. {example})"
        )
    return team_id


def print_games(games: list[Game]) -> None:
    for g in games:
        when = g.start_time_utc or g.date
        if g.status == "final":
            suffix = f" ({g.ot})" if g.ot in ("OT", "SO") else ""
            score = f"{g.away_score}-{g.home_score}{suffix}"
        else:
            score = g.status
        dh = f" (DH G{g.game_number})" if g.doubleheader in ("Y", "S") else ""
        print(f"{g.date}  {g.away_abbrev:>4} @ {g.home_abbrev:<4} {score:>12}  {when}  {g.venue}{dh}")


def make_parser(prog: str, description: str, season_default: str, season_help: str) -> argparse.ArgumentParser:
    """The CLI shared by every adapter: fetch / today / scores / common-opponents."""
    p = argparse.ArgumentParser(prog=prog, description=description)
    p.add_argument("--season", default=season_default, help=season_help)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download the schedule feed(s) and write games CSV")
    f.add_argument("--offline", action="store_true", help="parse the newest cached raw file(s)")

    t = sub.add_parser("today", help="show games on a date (default: today, US/Eastern)")
    t.add_argument("--date", help="YYYY-MM-DD")

    s = sub.add_parser("scores", help="completed regular-season games")
    s.add_argument("--team", help="filter by abbrev")
    s.add_argument("--last", type=int, default=0, help="only the most recent N")

    c = sub.add_parser("common-opponents", help="common-opponent breakdown for two teams")
    c.add_argument("team_a")
    c.add_argument("team_b")
    return p


def run_query_command(args: argparse.Namespace, games: list[Game],
                      by_abbrev: dict[str, str], sport: str, example: str) -> None:
    """Dispatch the non-fetch subcommands (identical across adapters)."""
    if args.cmd == "today":
        on = date.fromisoformat(args.date) if args.date else None
        picked = todays_games(games, on)
        print_games(picked)
        if not picked:
            print("no games")
    elif args.cmd == "scores":
        finals = completed_regular_season(games)
        if args.team:
            team_id = resolve_team(args.team, by_abbrev, sport, example)
            finals = [g for g in finals if team_id in (g.away_team_id, g.home_team_id)]
        if args.last:
            finals = finals[-args.last:]
        print_games(finals)
    elif args.cmd == "common-opponents":
        a = resolve_team(args.team_a, by_abbrev, sport, example)
        b = resolve_team(args.team_b, by_abbrev, sport, example)
        print(json.dumps(common_opponents(games, a, b), indent=2))
