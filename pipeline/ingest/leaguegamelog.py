#!/usr/bin/env python3
"""Historic seasons for the NBA family via the stats leaguegamelog endpoint.

The cdn.nba.com / cdn.wnba.com schedule feeds only serve the CURRENT
season, so backfill uses the leagues' stats APIs instead:

    https://stats.nba.com/stats/leaguegamelog?LeagueID=00&Season=2022-23&...
    https://stats.wnba.com/stats/leaguegamelog?LeagueID=10&Season=2024&...

One call per (season, season type) returns every completed game as TWO
rows — one per team — which this module pairs back into Game records:
the row whose MATCHUP contains " @ " is the away side, " vs. " the home
side. Historic rows carry no tip-off time, so start_time_utc is empty
(odds matching for these games is by teams + date, which is exactly the
matcher's primary key; only MLB needs times, for doubleheaders).

These endpoints are picky about request headers — STATS_HEADERS mimics a
browser; without them the API hangs or 403s.
"""

from __future__ import annotations

import urllib.parse

from pipeline.ingest.core import Game, UnmappedTeamError

STATS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}

SEASON_TYPES = {"Regular Season": "regular", "Playoffs": "playoffs"}


def log_url(host: str, league_id: str, season: str, season_type: str) -> str:
    params = urllib.parse.urlencode({
        "Counter": 1000, "DateFrom": "", "DateTo": "", "Direction": "ASC",
        "LeagueID": league_id, "PlayerOrTeam": "T", "Season": season,
        "SeasonType": season_type, "Sorter": "DATE",
    })
    return f"https://{host}/stats/leaguegamelog?{params}"


def referer_headers(site: str) -> dict:
    return {**STATS_HEADERS, "Referer": f"https://{site}/", "Origin": f"https://{site}"}


def build_games(doc: dict, sport: str, season: str, season_type_label: str,
                resolve_team) -> tuple[list[Game], int]:
    """Pair the two team rows of each game into Game records.

    resolve_team(team_id: int, abbrev: str) -> (our_team_id, our_abbrev)
    or None. An unresolvable team is fatal for regular season/playoffs —
    same loud policy as every other adapter. Returns (games,
    skipped_unpaired) — a game missing its partner row is skipped and
    counted, never guessed at.
    """
    try:
        result = next(rs for rs in doc["resultSets"] if rs["name"] == "LeagueGameLog")
        headers, rows = result["headers"], result["rowSet"]
    except (KeyError, StopIteration, TypeError):
        raise RuntimeError(
            "unexpected leaguegamelog shape: missing resultSets/LeagueGameLog "
            "(inspect the cached raw file in data/raw/)"
        )
    idx = {name: headers.index(name) for name in
           ("TEAM_ID", "TEAM_ABBREVIATION", "GAME_ID", "GAME_DATE", "MATCHUP", "PTS")}

    by_game: dict[str, list] = {}
    for row in rows:
        by_game.setdefault(str(row[idx["GAME_ID"]]), []).append(row)

    season_type = SEASON_TYPES.get(season_type_label, "other")
    games: list[Game] = []
    skipped = 0
    for game_id, pair in sorted(by_game.items()):
        away_row = next((r for r in pair if " @ " in r[idx["MATCHUP"]]), None)
        home_row = next((r for r in pair if " vs. " in r[idx["MATCHUP"]]), None)
        if len(pair) != 2 or away_row is None or home_row is None:
            skipped += 1
            continue

        sides = {}
        for label, row in (("away", away_row), ("home", home_row)):
            mapped = resolve_team(row[idx["TEAM_ID"]], row[idx["TEAM_ABBREVIATION"]])
            if mapped is None:
                raise UnmappedTeamError(
                    f"game {game_id}: team {row[idx['TEAM_ABBREVIATION']]!r} "
                    f"(id {row[idx['TEAM_ID']]}) not mappable via data/teams.csv"
                )
            sides[label] = mapped
        games.append(Game(
            game_id=game_id,
            season=season,
            season_type=season_type,
            date=str(away_row[idx["GAME_DATE"]])[:10],
            start_time_utc="",  # not provided historically
            status="final",
            away_team_id=sides["away"][0],
            home_team_id=sides["home"][0],
            away_abbrev=sides["away"][1],
            home_abbrev=sides["home"][1],
            away_score=str(away_row[idx["PTS"]]),
            home_score=str(home_row[idx["PTS"]]),
            venue="",
        ))
    games.sort(key=lambda g: (g.date, g.game_id))
    return games, skipped
