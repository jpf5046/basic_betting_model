#!/usr/bin/env python3
"""FeatureContext — the only data source a feature is allowed to read.

Point-in-time by construction (PLAN.md §0.1): the context is built for an
`as_of` date and serves ONLY completed regular-season games strictly
before that date, so a feature literally cannot leak future information.
A backtest for June 1 and the live June 1 daily run see the same context.

Also exposes the static team registry (venue coordinates, timezone) for
geography features like the travel canary.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date

from pipeline.ingest import core, mlb, nba, nhl, wnba
from pipeline.ingest.core import TEAMS_CSV, Game

# sport -> adapter module (games_csv_path + season defaults live there)
ADAPTERS = {"MLB": mlb, "NBA": nba, "NHL": nhl, "WNBA": wnba}


def default_season(sport: str, on: date) -> str:
    if sport in ("NBA",):
        return nba.default_season(on)
    if sport in ("NHL",):
        return nhl.default_season(on)
    return str(on.year)  # MLB / WNBA seasons live inside one calendar year


@dataclass
class TeamInfo:
    team_id: str
    abbrev: str
    name: str
    venue_name: str
    venue_lat: float
    venue_lon: float
    timezone: str


class FeatureContext:
    """Point-in-time view of one sport's season for feature computation."""

    def __init__(self, sport: str, as_of: date, games: list[Game]):
        self.sport = sport
        self.as_of = as_of
        cutoff = as_of.isoformat()
        # Strictly before as_of, finals, regular season only — the entire
        # point-in-time guarantee lives on this one line.
        self._past = [g for g in core.completed_regular_season(games) if g.date < cutoff]
        self._teams: dict[str, TeamInfo] = {}
        with open(TEAMS_CSV, newline="") as f:
            for r in csv.DictReader(f):
                if r["sport"] != sport:
                    continue
                self._teams[r["team_id"]] = TeamInfo(
                    team_id=r["team_id"],
                    abbrev=r["abbrev"],
                    name=r["name"],
                    venue_name=r["venue_name"],
                    venue_lat=float(r["venue_lat"]),
                    venue_lon=float(r["venue_lon"]),
                    timezone=r["timezone"],
                )

    @classmethod
    def load(cls, sport: str, as_of: date, season: str | None = None) -> "FeatureContext":
        """Build a context from the sport's games CSV (run the adapter's
        `fetch` first)."""
        adapter = ADAPTERS[sport]
        season = season or default_season(sport, as_of)
        games = core.read_games_csv(
            adapter.games_csv_path(season),
            f"python3 -m pipeline.ingest.{sport.lower()} fetch",
        )
        return cls(sport, as_of, games)

    # ------------------------------------------------------- data access

    @property
    def past_games(self) -> list[Game]:
        return list(self._past)

    def team_log(self, team_id: str) -> list[dict]:
        """This team's completed regular-season games before as_of,
        oldest first (core.team_log shape, incl. the NHL ot/OTL marking)."""
        return core.team_log(self._past, team_id)

    def games_played(self, team_id: str) -> int:
        return len(self.team_log(team_id))

    def common_opponents(self, team_a: str, team_b: str) -> dict:
        """my_model.md method 2 inputs (see core.common_opponents)."""
        return core.common_opponents(self._past, team_a, team_b)

    def head_to_head(self, team_a: str, team_b: str) -> list[dict]:
        """team_a's log rows from games against team_b this season."""
        return [r for r in self.team_log(team_a) if r["opponent_id"] == team_b]

    def team(self, team_id: str) -> TeamInfo:
        if team_id not in self._teams:
            raise KeyError(f"{team_id!r} not in data/teams.csv for {self.sport}")
        return self._teams[team_id]
