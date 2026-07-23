"""Describe the data model for the "Database" page.

Two kinds of things get shown, clearly labelled:

  * **Database tables** — the real SQL tables the storage layer creates
    (``pipeline.db.core.SCHEMA``). Parsed live from that constant so this
    page can never drift from the actual schema.
  * **CSV datasets** — the flat files under ``data/`` that are the source
    of truth the DB mirrors (each dataset is also described from its real
    CSV headers so the on-disk shape stays visible).

Plus the logical relationships between them (the one-to-many links the
user asked to see), since the SQLite schema keeps foreign keys implicit.
"""

from __future__ import annotations

import csv
import glob
import re
from pathlib import Path

from pipeline.db.core import SCHEMA

# ---------------------------------------------------------------- SQL parse

_CREATE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*)\)\s*$", re.S)


def _split_top_level(body: str) -> list[str]:
    """Split a CREATE-TABLE body on commas that are not inside parens."""
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _parse_create(stmt: str) -> dict | None:
    m = _CREATE_RE.search(stmt.strip())
    if not m:
        return None
    name, body = m.group(1), m.group(2)
    columns: list[dict] = []
    pk: list[str] = []
    for raw in _split_top_level(body):
        part = raw.strip()
        if not part:
            continue
        upper = part.upper()
        if upper.startswith("PRIMARY KEY"):
            inside = part[part.find("(") + 1 : part.rfind(")")]
            pk = [c.strip() for c in inside.split(",")]
            continue
        tokens = part.split()
        col = tokens[0]
        col_type = tokens[1] if len(tokens) > 1 else ""
        inline_pk = "PRIMARY KEY" in upper
        not_null = "NOT NULL" in upper
        if inline_pk:
            pk.append(col)
        columns.append(
            {"name": col, "type": col_type, "pk": inline_pk, "not_null": not_null}
        )
    # Mark composite-PK members discovered on the PRIMARY KEY (...) line.
    for c in columns:
        if c["name"] in pk:
            c["pk"] = True
    return {"name": name, "columns": columns, "pk": pk}


# Human notes for each real table / column, so the page reads as documentation
# rather than a raw dump. Missing entries just render without a note.
_TABLE_NOTES = {
    "teams": "Canonical team registry — the primary key everything else points at. "
             "Seeded from data/teams.csv.",
    "games": "One row per scheduled/played game per sport. The schedule + final "
             "scores feed. Mirrors data/<sport>/games_*.csv.",
    "frames": "The labelled canonical row the model trains/predicts on: identity + "
              "the point-in-time feature vector (stored as JSON) + the actual "
              "outcome. Mirrors data/frames/<sport>_<season>.csv.",
    "predictions": "One row per game the model predicted on a date: predicted score, "
                   "win probability, ML/TOTAL leans and confidence. Mirrors "
                   "data/predictions/<sport>/predictions_<date>.csv.",
    "picks": "The subset of predictions that cleared the confidence gate and were "
             "published as bets for a date. Mirrors picks_<date>.csv.",
    "grades": "Published picks scored once the finals are in — WIN/LOSS/PUSH/VOID "
              "and P&L at market price. The ROI source. Mirrors grades_<date>.csv.",
}

_COLUMN_NOTES = {
    ("teams", "team_id"): "PK, e.g. mlb-nyy. Never reused or renamed.",
    ("teams", "external_ids"): "JSON map of source → native id (mlb/nba/nhl/espn).",
    ("games", "game_id"): "Source game id (unique within a sport).",
    ("games", "status"): "scheduled / final / postponed …",
    ("games", "away_team_id"): "→ teams.team_id",
    ("games", "home_team_id"): "→ teams.team_id",
    ("games", "doubleheader"): "Y/N — MLB can play the same pair twice in a day.",
    ("frames", "game_id"): "→ games.game_id",
    ("frames", "features"): "JSON feature vector, so new features need no migration.",
    ("frames", "winner"): "Actual winner team id (label).",
    ("frames", "away_team_id"): "→ teams.team_id",
    ("frames", "home_team_id"): "→ teams.team_id",
    ("predictions", "game_id"): "→ games.game_id (PK also includes date).",
    ("predictions", "win_prob"): "Model win probability for the predicted winner.",
    ("predictions", "winner_team_id"): "→ teams.team_id (predicted winner).",
    ("picks", "game_id"): "→ predictions / games.game_id.",
    ("picks", "bet_type"): "ML or TOTAL (part of the key).",
    ("grades", "game_id"): "→ picks / games.game_id.",
    ("grades", "result"): "WIN / LOSS / PUSH / VOID / PENDING.",
    ("grades", "pnl"): "Profit/loss for a flat $100 stake at the market price.",
}


def db_tables() -> list[dict]:
    """The real SQL tables, parsed from ``pipeline.db.core.SCHEMA``."""
    out = []
    for stmt in SCHEMA:
        parsed = _parse_create(stmt)
        if not parsed:
            continue
        parsed["note"] = _TABLE_NOTES.get(parsed["name"], "")
        for c in parsed["columns"]:
            c["note"] = _COLUMN_NOTES.get((parsed["name"], c["name"]), "")
        out.append(parsed)
    return out


# ------------------------------------------------------------- CSV datasets

# Model / grader outputs. These are mirrored into like-named DB tables (above),
# but the flat CSVs remain the source of truth; columns are read from a real
# file so they stay true.
_CSV_DATASETS = [
    {
        "name": "predictions",
        "glob": "data/predictions/*/predictions_*.csv",
        "note": "One row per game the model predicted on a date: predicted score, "
                "win probability, ML/TOTAL leans and confidence.",
        "key": "game_id (+ sport, date) → games.game_id",
    },
    {
        "name": "picks",
        "glob": "data/predictions/*/picks_*.csv",
        "note": "The subset of predictions that cleared the confidence gate and were "
                "actually published as bets for a date.",
        "key": "game_id (+ bet_type) → predictions / games",
    },
    {
        "name": "grades",
        "glob": "data/predictions/*/grades_*.csv",
        "note": "Yesterday's published picks scored once finals are in — "
                "WIN/LOSS/PUSH/VOID and P&L at market price. The ROI source.",
        "key": "game_id (+ bet_type) → picks / games",
    },
]

_CSV_COLUMN_NOTES = {
    ("predictions", "win_prob"): "Model win probability for the predicted winner.",
    ("predictions", "ml_lean"): "Moneyline lean (or TOSS-UP).",
    ("predictions", "total_lean"): "OVER/UNDER/PASS on the total.",
    ("predictions", "winner_team_id"): "→ teams.team_id (predicted winner).",
    ("grades", "result"): "WIN / LOSS / PUSH / VOID / PENDING.",
    ("grades", "pnl"): "Profit/loss for a flat $100 stake at the market price.",
    ("grades", "bet_type"): "ML or TOTAL.",
}


def csv_datasets(repo_root: Path) -> list[dict]:
    out = []
    for spec in _CSV_DATASETS:
        matches = sorted(glob.glob(str(repo_root / spec["glob"])))
        columns: list[dict] = []
        example = ""
        if matches:
            example = str(Path(matches[-1]).relative_to(repo_root))
            with open(matches[-1], newline="") as f:
                header = next(csv.reader(f), [])
            columns = [
                {"name": c, "note": _CSV_COLUMN_NOTES.get((spec["name"], c), "")}
                for c in header
            ]
        out.append({**spec, "columns": columns, "example": example,
                    "file_count": len(matches)})
    return out


# ------------------------------------------------------------ relationships

# The one-to-many links the user asked to see. `parent` is the "one" side.
RELATIONSHIPS = [
    {"parent": "teams", "parent_col": "team_id", "child": "games",
     "child_col": "home_team_id / away_team_id", "kind": "one-to-many",
     "desc": "A team hosts or visits many games."},
    {"parent": "teams", "parent_col": "team_id", "child": "frames",
     "child_col": "home_team_id / away_team_id", "kind": "one-to-many",
     "desc": "A team appears in many feature frames."},
    {"parent": "games", "parent_col": "game_id", "child": "frames",
     "child_col": "game_id", "kind": "one-to-one",
     "desc": "Each game has one labelled feature frame (per sport)."},
    {"parent": "games", "parent_col": "game_id", "child": "predictions",
     "child_col": "game_id", "kind": "one-to-many",
     "desc": "A game can be predicted on multiple run dates."},
    {"parent": "predictions", "parent_col": "game_id", "child": "picks",
     "child_col": "game_id", "kind": "one-to-many",
     "desc": "A predicted game can yield ML and/or TOTAL published picks."},
    {"parent": "picks", "parent_col": "game_id", "child": "grades",
     "child_col": "game_id", "kind": "one-to-one",
     "desc": "Each published pick is graded once the final is in."},
]
