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

from pipeline.api.paths import default_data_dir, predictions_dir
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

# The per-date model/grader CSVs. Each entry: (table, file prefix, columns, pk).
# Columns/pk match pipeline.db.core's CREATE TABLE for a byte-faithful mirror.
PRED_COLS = ["sport", "game_id", "date", "status", "ml_lean", "total_lean",
             "away_team_id", "home_team_id", "pred_away", "pred_home",
             "disp_away", "disp_home", "pred_total", "pred_spread", "win_prob",
             "winner_team_id", "pred_confidence", "method_details"]
PICK_COLS = ["sport", "game_id", "date", "bet_type", "selection", "confidence", "line"]
GRADE_COLS = ["sport", "game_id", "date", "bet_type", "selection", "confidence",
              "line", "game_status", "actual_away", "actual_home", "result",
              "pnl", "price_american"]

_DATED = [
    ("predictions", PRED_COLS, ["sport", "game_id", "date"]),
    ("picks", PICK_COLS, ["sport", "game_id", "date", "bet_type"]),
    ("grades", GRADE_COLS, ["sport", "game_id", "date", "bet_type"]),
]


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


def load_dated(db: Db, sport: str, name: str, cols: list[str], pk: list[str],
               data_dir: Path | None = None) -> int:
    """Upsert every dated CSV (``<name>_<date>.csv``) for a sport.

    Covers predictions / picks / grades, which are organised per run date
    rather than per season. Values are stored verbatim (TEXT) so the table
    mirrors the CSV exactly; identical repeated rows collapse on the key."""
    d = predictions_dir(data_dir or default_data_dir(), sport)
    if not d.exists():
        return 0
    total = 0
    for path in sorted(d.glob(f"{name}_*.csv")):
        with open(path, newline="") as f:
            rows = [tuple((r.get(c) or "") for c in cols) for r in csv.DictReader(f)]
        total += db.executemany(upsert_sql(name, cols, pk), rows)
    db.commit()
    return total


def load_all(db: Db, sports: list[str], season_override: str | None,
             on: date, data_dir: Path | None = None) -> dict[str, dict[str, int]]:
    """Init schema, then upsert teams + each sport's games, frames, and the
    per-date predictions / picks / grades outputs."""
    db.init_schema()
    report: dict[str, dict[str, int]] = {"teams": {"all": load_teams(db)}}
    for sport in sports:
        season = season_override or default_season(sport, on)
        stats = {
            "games": load_games(db, sport, season),
            "frames": load_frames(db, sport, season),
        }
        for name, cols, pk in _DATED:
            stats[name] = load_dated(db, sport, name, cols, pk, data_dir)
        report[sport] = stats
    return report
