#!/usr/bin/env python3
"""MLB data ingester — PLAN.md §2, `mlb_statsapi` source adapter.

Source: the official MLB Stats API schedule endpoint
    https://statsapi.mlb.com/api/v1/schedule?sportId=1&season=<YYYY>&gameTypes=R,F,D,W,L
One unauthenticated GET returns the whole season — every game past and
upcoming, with start times, status, and final scores for completed games.
That single document covers all three needs of this milestone:

  * today's games          -> `today` command
  * this season's scores   -> `scores` command
  * common opponents       -> `common-opponents` command (my_model.md method 2 inputs)

Raw responses are cached under data/raw/mlb/ before parsing (PLAN.md §2:
a re-run never needs to re-hit the API; history can be rebuilt offline).
Normalized games land in data/mlb/games_<season>.csv keyed by the canonical
team_ids from data/teams.csv (matched on the `mlb` external id, never on
team names).

MLB-specific edges handled here because grading will need them later:
  * doubleheaders — both games kept, distinguished by game_number/doubleheader
  * postponed / suspended / cancelled games — never counted as finals
  * spring training (gameType S) parsed but excluded from logs and
    common-opponent math; the fetch default requests regular season +
    postseason only

Usage (from the repo root):
    python3 -m pipeline.ingest.mlb fetch [--season 2026]  # download + write CSV
    python3 -m pipeline.ingest.mlb fetch --offline        # reuse newest cached raw file
    python3 -m pipeline.ingest.mlb today [--date YYYY-MM-DD]
    python3 -m pipeline.ingest.mlb scores [--team NYY] [--last 10]
    python3 -m pipeline.ingest.mlb common-opponents NYY BOS

NOTE: outbound requests to statsapi.mlb.com are blocked from the Claude Code
sandbox this was authored in; `fetch` is meant to run on a normal machine
(laptop, GitHub Actions). Everything downstream of the raw file is covered
by offline tests in tests/test_mlb_ingest.py.

The query layer (team_log / common_opponents / todays_games) is deliberately
duplicated from pipeline/ingest/wnba.py for now — extract a shared core once
both adapters are merged and a third sport lands (PLAN.md §2's common
interface), rather than coupling two in-flight PRs.
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

SPORT = "MLB"
SCHEDULE_URL = (
    "https://statsapi.mlb.com/api/v1/schedule"
    "?sportId=1&season={season}&gameTypes=R,F,D,W,L"
)
REPO_ROOT = Path(__file__).resolve().parents[2]
TEAMS_CSV = REPO_ROOT / "data" / "teams.csv"
RAW_DIR = REPO_ROOT / "data" / "raw" / "mlb"
OUT_DIR = REPO_ROOT / "data" / "mlb"
EASTERN = ZoneInfo("America/New_York")

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


@dataclass
class Game:
    game_id: str          # MLB gamePk — already unique per doubleheader game
    season: str
    season_type: str
    date: str             # officialDate (local scheduled date), YYYY-MM-DD
    start_time_utc: str
    status: str           # scheduled | live | final | postponed | suspended | cancelled
    away_team_id: str
    home_team_id: str
    away_abbrev: str
    home_abbrev: str
    away_score: str       # empty unless final
    home_score: str
    venue: str
    doubleheader: str     # N = no, Y = traditional, S = split
    game_number: str      # 1 or 2 within a doubleheader day


CSV_FIELDS = [f.name for f in fields(Game)]


class UnmappedTeamError(RuntimeError):
    pass


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

    url = SCHEDULE_URL.format(season=season)
    req = urllib.request.Request(url, headers={"User-Agent": "basic-betting-model/0.1"})
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
        raise RuntimeError(f"could not fetch {url}: {last_err}")

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
    if game.get("officialDate"):
        return game["officialDate"]
    # Fallback: Eastern calendar date of first pitch.
    raw = game.get("gameDate") or ""
    if raw:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(EASTERN).date().isoformat()
    return ""


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
            away_mlb = (away.get("team") or {}).get("id")
            home_mlb = (home.get("team") or {}).get("id")
            away_mapped = by_mlb_id.get(away_mlb)
            home_mapped = by_mlb_id.get(home_mlb)
            if away_mapped is None or home_mapped is None:
                if season_type in ("regular", "playoffs"):
                    bad = away_mlb if away_mapped is None else home_mlb
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
        sys.exit(f"{path} not found — run `python3 -m pipeline.ingest.mlb fetch` first")
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
        sys.exit(f"unknown MLB team {token!r} — use an abbrev from data/teams.csv (e.g. NYY)")
    return team_id


def _print_games(games: list[Game]) -> None:
    for g in games:
        when = g.start_time_utc or g.date
        score = f"{g.away_score}-{g.home_score}" if g.status == "final" else g.status
        dh = f" (DH G{g.game_number})" if g.doubleheader in ("Y", "S") else ""
        print(f"{g.date}  {g.away_abbrev:>3} @ {g.home_abbrev:<3} {score:>10}  {when}  {g.venue}{dh}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.ingest.mlb", description=__doc__)
    p.add_argument("--season", default=str(datetime.now(EASTERN).year))
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download the season schedule and write games CSV")
    f.add_argument("--offline", action="store_true", help="parse the newest cached raw file")

    t = sub.add_parser("today", help="show games on a date (default: today, US/Eastern)")
    t.add_argument("--date", help="YYYY-MM-DD")

    s = sub.add_parser("scores", help="completed regular-season games")
    s.add_argument("--team", help="filter by abbrev, e.g. NYY")
    s.add_argument("--last", type=int, default=0, help="only the most recent N")

    c = sub.add_parser("common-opponents", help="common-opponent breakdown for two teams")
    c.add_argument("team_a")
    c.add_argument("team_b")

    args = p.parse_args(argv)
    by_mlb_id, by_abbrev = load_team_maps()

    if args.cmd == "fetch":
        raw = fetch_raw(args.season, offline=args.offline)
        doc = json.loads(raw.read_text())
        games = parse_games(doc, by_mlb_id)
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
