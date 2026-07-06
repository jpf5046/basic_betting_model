#!/usr/bin/env python3
"""NHL data ingester — PLAN.md §2, `nhl_api` source adapter.

Source: the NHL's modern public API. There is no single league-wide
full-season feed, so this adapter pulls each club's season schedule:

    https://api-web.nhle.com/v1/club-schedule-season/{TRICODE}/{seasonId}

one unauthenticated GET per team (32 total, cached individually), then
dedupes games by id — every game appears in exactly two club feeds. Each
feed carries start times, status, final scores, and the OT/SO outcome.
Together they cover all three needs of this milestone:

  * today's games          -> `today` command
  * this season's scores   -> `scores` command
  * common opponents       -> `common-opponents` command (my_model.md method 2 inputs)

Raw responses are cached under data/raw/nhl/ before parsing (PLAN.md §2:
a re-run never needs to re-hit the API; history can be rebuilt offline).
Normalized games land in data/nhl/games_<seasonId>.csv keyed by the
canonical team_ids from data/teams.csv — matched on the `nhl` external id
first, tricode second (teams.csv NHL abbrevs ARE the official tricodes).

NHL specifics handled here:
  * season ids span years — "20252026", derived by default_season()
  * OT/shootout outcome kept as an `ot` column (REG/OT/SO) — the NHL
    last-10 form multiplier in my_model.md is points-based (2xW + OTL),
    so the grader/features need to know how a loss ended
  * postponed / suspended / cancelled via gameScheduleState — never results
  * preseason (gameType 1) parsed but excluded from logs; playoffs
    (gameType 3) parsed, excluded from regular-season logs

Usage (from the repo root):
    python3 -m pipeline.ingest.nhl fetch                 # 32 GETs + write CSV
    python3 -m pipeline.ingest.nhl fetch --offline       # reuse newest cached raw files
    python3 -m pipeline.ingest.nhl today [--date YYYY-MM-DD]
    python3 -m pipeline.ingest.nhl scores [--team BOS] [--last 10]
    python3 -m pipeline.ingest.nhl common-opponents BOS TOR

NOTE: outbound requests to api-web.nhle.com are blocked from the Claude
Code sandbox this was authored in; `fetch` is meant to run on a normal
machine (laptop, GitHub Actions). Everything downstream of the raw files
is covered by offline tests in tests/test_nhl_ingest.py.

The query layer (team_log / common_opponents / todays_games) is deliberately
duplicated from the in-flight WNBA/MLB/NBA adapters — extract a shared core
once those PRs are merged rather than coupling open branches.
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

SPORT = "NHL"
SCHEDULE_URL = "https://api-web.nhle.com/v1/club-schedule-season/{tricode}/{season}"
REPO_ROOT = Path(__file__).resolve().parents[2]
TEAMS_CSV = REPO_ROOT / "data" / "teams.csv"
RAW_DIR = REPO_ROOT / "data" / "raw" / "nhl"
OUT_DIR = REPO_ROOT / "data" / "nhl"
EASTERN = ZoneInfo("America/New_York")

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


@dataclass
class Game:
    game_id: str
    season: str          # NHL seasonId, e.g. "20252026"
    season_type: str
    date: str            # NHL gameDate (local calendar date), YYYY-MM-DD
    start_time_utc: str
    status: str          # scheduled | live | final | postponed | suspended | cancelled
    away_team_id: str
    home_team_id: str
    away_abbrev: str
    home_abbrev: str
    away_score: str      # empty unless final
    home_score: str
    venue: str
    ot: str              # REG | OT | SO, empty unless final


CSV_FIELDS = [f.name for f in fields(Game)]


class UnmappedTeamError(RuntimeError):
    pass


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


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "basic-betting-model/0.1"})
    last_err: Exception | None = None
    for attempt, delay in enumerate((0, 2, 4, 8)):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            print(f"fetch attempt {attempt + 1} failed for {url}: {e}", file=sys.stderr)
    raise RuntimeError(f"could not fetch {url}: {last_err}")


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
        body = _get(SCHEDULE_URL.format(tricode=tri, season=season))
        path = RAW_DIR / f"club-schedule-season.{tri}.{season}.{stamp}.json"
        path.write_bytes(body)
        paths.append(path)
        time.sleep(0.3)  # politeness between the 32 requests
    return paths


def _eastern_date(game: dict) -> str:
    """gameDate is the NHL's local calendar date; fall back to Eastern."""
    if game.get("gameDate"):
        return game["gameDate"]
    utc = game.get("startTimeUTC") or ""
    if utc:
        dt = datetime.fromisoformat(utc.replace("Z", "+00:00"))
        return dt.astimezone(EASTERN).date().isoformat()
    return ""


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
                    date=_eastern_date(g),
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
        sys.exit(f"{path} not found — run `python3 -m pipeline.ingest.nhl fetch` first")
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
    (PLAN.md game_logs shape, including the ot_flag the NHL form calc needs)."""
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

def _resolve_team(token: str, by_abbrev: dict[str, str]) -> str:
    if token in by_abbrev.values():
        return token
    team_id = by_abbrev.get(token.upper())
    if team_id is None:
        sys.exit(f"unknown NHL team {token!r} — use a tricode from data/teams.csv (e.g. BOS)")
    return team_id


def _print_games(games: list[Game]) -> None:
    for g in games:
        when = g.start_time_utc or g.date
        if g.status == "final":
            suffix = f" ({g.ot})" if g.ot in ("OT", "SO") else ""
            score = f"{g.away_score}-{g.home_score}{suffix}"
        else:
            score = g.status
        print(f"{g.date}  {g.away_abbrev:>3} @ {g.home_abbrev:<3} {score:>12}  {when}  {g.venue}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.ingest.nhl", description=__doc__)
    p.add_argument("--season", default=default_season(), help='NHL seasonId, e.g. "20252026"')
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download all 32 club schedules and write games CSV")
    f.add_argument("--offline", action="store_true", help="parse the newest cached raw files")

    t = sub.add_parser("today", help="show games on a date (default: today, US/Eastern)")
    t.add_argument("--date", help="YYYY-MM-DD")

    s = sub.add_parser("scores", help="completed regular-season games")
    s.add_argument("--team", help="filter by tricode, e.g. BOS")
    s.add_argument("--last", type=int, default=0, help="only the most recent N")

    c = sub.add_parser("common-opponents", help="common-opponent breakdown for two teams")
    c.add_argument("team_a")
    c.add_argument("team_b")

    args = p.parse_args(argv)
    by_nhl_id, by_abbrev = load_team_maps()

    if args.cmd == "fetch":
        raws = fetch_raw(args.season, offline=args.offline)
        docs = [json.loads(p_.read_text()) for p_ in raws]
        games = parse_games(docs, by_nhl_id, by_abbrev)
        path = write_games_csv(games)
        finals = sum(1 for g in games if g.status == "final")
        today = len(todays_games(games))
        print(f"raw feeds: {len(raws)} files in {RAW_DIR}")
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
