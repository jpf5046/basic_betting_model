#!/usr/bin/env python3
"""Read the pipeline's artifacts off disk and shape them for the console.

The thin seam between raw files and the API: every function takes a
``data_dir`` and returns plain dicts/lists built by ``parsers`` /
``reasoning``. Stdlib-only and offline, so it is unit-tested directly
against a fixture data tree.
"""

from __future__ import annotations

import csv
import glob
import re
from pathlib import Path

from pipeline.api import parsers, reasoning
from pipeline.api.paths import (
    SPORTS,
    frame_path,
    grades_path,
    picks_path,
    predictions_dir,
    predictions_path,
    report_path,
    reports_dir,
)
from pipeline.api.teams import TeamRegistry
from pipeline.models.config import FACTORY_DEFAULTS

_REPORT_DATE_RE = re.compile(r"daily_(\d{4}-\d{2}-\d{2})\.md$")
_PRED_DATE_RE = re.compile(r"predictions_(\d{4}-\d{2}-\d{2})\.csv$")


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------- runs

def list_report_dates(data_dir: Path) -> list[str]:
    out = []
    for p in reports_dir(data_dir).glob("daily_*.md") if reports_dir(data_dir).exists() else []:
        m = _REPORT_DATE_RE.search(p.name)
        if m:
            out.append(m.group(1))
    return sorted(out, reverse=True)


def read_report(data_dir: Path, on: str) -> dict | None:
    path = report_path(data_dir, on)
    if not path.exists():
        return None
    return parsers.parse_report(path.read_text(), on)


def list_runs(data_dir: Path) -> list[dict]:
    runs = []
    for on in list_report_dates(data_dir):
        parsed = read_report(data_dir, on)
        if parsed:
            runs.append(parsers.summarize_report(parsed))
    return runs


# ----------------------------------------------------- picks / predictions

def available_dates(data_dir: Path, sport: str) -> list[str]:
    d = predictions_dir(data_dir, sport)
    out = []
    for p in d.glob("predictions_*.csv") if d.exists() else []:
        m = _PRED_DATE_RE.search(p.name)
        if m:
            out.append(m.group(1))
    return sorted(out, reverse=True)


def _frame_index(data_dir: Path, sport: str) -> dict[str, list[dict]]:
    """All frame rows for a sport (across seasons) indexed by game_id."""
    index: dict[str, list[dict]] = {}
    pattern = str(frame_path(data_dir, sport, "*"))
    for path in glob.glob(pattern):
        for row in _read_csv(Path(path)):
            index.setdefault(row.get("game_id", ""), []).append(row)
    return index


def _frame_for(index: dict, game_id: str, on: str) -> dict | None:
    rows = index.get(game_id, [])
    if not rows:
        return None
    same_date = [r for r in rows if r.get("date") == on]
    return (same_date or rows)[0]


def read_predictions(data_dir: Path, sport: str, on: str,
                     teams: TeamRegistry) -> dict:
    """Full prediction table for a date + published-pick overlay."""
    pred_rows = [parsers.shape_prediction(r) for r in
                 _read_csv(predictions_path(data_dir, sport, on))]
    pick_rows = _read_csv(picks_path(data_dir, sport, on))

    # Which (game_id, bet_type) pairs were actually published.
    published = {(p["game_id"], p["bet_type"]) for p in pick_rows}

    def enrich(row: dict) -> dict:
        away = teams.get(row["away_team_id"])
        home = teams.get(row["home_team_id"])
        winner = teams.get(row["winner_team_id"]) if row["winner_team_id"] else None
        return {
            **row,
            "away": {"abbrev": away["abbrev"], "name": away["name"]},
            "home": {"abbrev": home["abbrev"], "name": home["name"]},
            "winner_abbrev": winner["abbrev"] if winner else "",
            "published_ml": (row["game_id"], "ML") in published,
            "published_total": (row["game_id"], "TOTAL") in published,
        }

    # De-dup identical rows (doubleheader feeds can repeat a game_id row).
    seen = set()
    unique = []
    for r in pred_rows:
        key = (r["game_id"], r["away_team_id"], r["home_team_id"],
               r["pred_away"], r["pred_home"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(enrich(r))

    return {
        "sport": sport, "date": on,
        "predictions": unique,
        "picks": pick_rows,
        "counts": {
            "games": len(unique),
            "published": len(pick_rows),
            "toss_up": sum(1 for r in unique if r["ml_lean"] == "TOSS-UP"),
            "no_prediction": sum(1 for r in unique if r["status"] != "ok"),
        },
    }


def read_reasoning(data_dir: Path, sport: str, on: str, game_id: str,
                   params: dict, teams: TeamRegistry) -> dict | None:
    rows = [parsers.shape_prediction(r) for r in
            _read_csv(predictions_path(data_dir, sport, on))]
    pred = next((r for r in rows if r["game_id"] == game_id), None)
    if pred is None:
        return None
    frame_row = _frame_for(_frame_index(data_dir, sport), game_id, on)
    return reasoning.build_reasoning(
        pred, frame_row, params,
        teams.abbrev(pred["away_team_id"]), teams.abbrev(pred["home_team_id"]),
    )


# ------------------------------------------------------------- performance

def all_grades(data_dir: Path) -> list[dict]:
    grades = []
    for sport in SPORTS:
        d = predictions_dir(data_dir, sport)
        for p in d.glob("grades_*.csv") if d.exists() else []:
            grades.extend(parsers.shape_grade(r) for r in _read_csv(p))
    return grades


def performance(data_dir: Path) -> dict:
    grades = all_grades(data_dir)
    perf = parsers.performance(grades)
    perf["has_grades"] = bool(grades)
    perf["total_graded_files"] = len(grades)
    return perf


# ------------------------------------------------------------------ teams

def team_rows(data_dir: Path, teams: TeamRegistry) -> list[dict]:
    return [teams.get(tid) for tid in teams._by_id]  # noqa: SLF001 (internal, single-user)
