#!/usr/bin/env python3
"""Load the flat-file artifacts into the database (PLAN.md §1).

The CSVs stay the source of truth; every load is a full idempotent
upsert, so re-running after a fresh fetch/backfill just refreshes rows.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from pipeline.backfill import OUTCOME_COLS, frame_csv_path
from pipeline.db.core import Db, upsert_sql
from pipeline.features.context import ADAPTERS, default_season
from pipeline.ingest.core import CSV_FIELDS, TEAMS_CSV

TEAM_COLS = ["team_id", "sport", "name", "abbrev", "conference", "division",
             "external_ids", "venue_name", "venue_lat", "venue_lon", "timezone"]

GAME_COLS = ["sport"] + CSV_FIELDS  # the shared Game record, plus its sport

FRAME_FIXED = ["sport", "game_id", "season", "season_type", "date", "as_of_date",
               "away_team_id", "home_team_id", "status",
               "features", "feature_versions",
               "final_away", "final_home", "actual_total", "actual_margin", "winner"]

# Frame CSV columns that are NOT part of the per-game feature vector.
_NON_FEATURE = set(FRAME_FIXED) - {"features"}


def _int_or_none(value: str):
    return int(value) if value not in ("", None) else None


def _num_or_str(value: str):
    """Feature CSV cells -> json values: numbers stay numbers, '' -> null."""
    if value in ("", None):
        return None
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except ValueError:
        return value


def load_teams(db: Db) -> int:
    with open(TEAMS_CSV, newline="") as f:
        rows = [tuple(r[c] or None for c in TEAM_COLS) for r in csv.DictReader(f)]
    n = db.executemany(upsert_sql("teams", TEAM_COLS, ["team_id"]), rows)
    db.commit()
    return n


def load_games(db: Db, sport: str, season: str) -> int:
    path = ADAPTERS[sport].games_csv_path(season)
    if not path.exists():
        return 0
    with open(path, newline="") as f:
        rows = []
        for r in csv.DictReader(f):
            r["sport"] = sport
            r["away_score"] = _int_or_none(r["away_score"])
            r["home_score"] = _int_or_none(r["home_score"])
            rows.append(tuple(r[c] for c in GAME_COLS))
    n = db.executemany(upsert_sql("games", GAME_COLS, ["sport", "game_id"]), rows)
    db.commit()
    return n


def load_frames(db: Db, sport: str, season: str) -> int:
    path = frame_csv_path(sport, season)
    if not path.exists():
        return 0
    with open(path, newline="") as f:
        rows = []
        for r in csv.DictReader(f):
            features = {k: _num_or_str(v) for k, v in r.items() if k not in _NON_FEATURE}
            row = dict(r, features=json.dumps(features, sort_keys=True))
            for c in OUTCOME_COLS:
                if c != "winner":
                    row[c] = _int_or_none(row.get(c, ""))
            rows.append(tuple(row.get(c) for c in FRAME_FIXED))
    n = db.executemany(upsert_sql("frames", FRAME_FIXED, ["sport", "game_id"]), rows)
    db.commit()
    return n


def load_all(db: Db, sports: list[str], season_override: str | None,
             on: date) -> dict[str, dict[str, int]]:
    """Init schema, then upsert teams + each sport's games and frame rows."""
    db.init_schema()
    report: dict[str, dict[str, int]] = {"teams": {"all": load_teams(db)}}
    for sport in sports:
        season = season_override or default_season(sport, on)
        report[sport] = {
            "games": load_games(db, sport, season),
            "frames": load_frames(db, sport, season),
        }
    return report
