#!/usr/bin/env python3
"""Team registry — resolve raw ids like ``mlb-cws`` to real names/abbrevs.

Reads ``data/teams.csv`` directly (the source of truth) so the console can
render ``CWS`` / ``Chicago White Sox`` instead of ``mlb-cws`` everywhere a
team id appears. Stdlib-only and side-effect free; the API caches one
instance per data dir.
"""

from __future__ import annotations

import csv
from pathlib import Path

from pipeline.api.paths import teams_csv


class TeamRegistry:
    def __init__(self, rows: list[dict]):
        self._by_id: dict[str, dict] = {}
        for r in rows:
            self._by_id[r["team_id"]] = {
                "team_id": r["team_id"],
                "sport": r.get("sport", ""),
                "name": r.get("name", r["team_id"]),
                "abbrev": r.get("abbrev", r["team_id"]),
                "conference": r.get("conference", ""),
                "division": r.get("division", ""),
                "venue_name": r.get("venue_name", ""),
                "timezone": r.get("timezone", ""),
            }

    @classmethod
    def load(cls, data_dir: Path) -> "TeamRegistry":
        path = teams_csv(data_dir)
        if not path.exists():
            return cls([])
        with open(path, newline="") as f:
            return cls(list(csv.DictReader(f)))

    def get(self, team_id: str) -> dict:
        return self._by_id.get(
            team_id,
            {"team_id": team_id, "sport": "", "name": team_id, "abbrev": team_id,
             "conference": "", "division": "", "venue_name": "", "timezone": ""},
        )

    def abbrev(self, team_id: str) -> str:
        return self.get(team_id)["abbrev"]

    def name(self, team_id: str) -> str:
        return self.get(team_id)["name"]

    def label(self, team_id: str) -> str:
        """`ABBR` when known, else the raw id — safe for any UI slot."""
        return self.get(team_id)["abbrev"] or team_id
