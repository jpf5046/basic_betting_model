#!/usr/bin/env python3
"""Feature frame builder — one wide row per slate game, every registered
feature for both sides. This is the early prototype of the PLAN.md §5
canonical dataframe: the daily run and the backtester call exactly this
code with different dates.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from pipeline.ingest.core import REPO_ROOT, Game

from pipeline.features.context import FeatureContext
from pipeline.features.registry import features_for

FEATURES_DIR = REPO_ROOT / "data" / "features"

IDENTITY_COLS = ["game_id", "sport", "date", "away_team_id", "home_team_id", "status"]


def _flatten(prefix: str, value) -> dict[str, object]:
    """A feature value becomes one column (scalar) or several (dict)."""
    if value is None:
        return {prefix: ""}
    if isinstance(value, dict):
        return {f"{prefix}_{k}": v for k, v in value.items()}
    return {prefix: value}


def build_rows(ctx: FeatureContext, slate: list[Game]) -> tuple[list[str], list[dict]]:
    """Compute every registered feature for every slate game.

    Returns (columns, rows). Column set is the union across rows so games
    with missing data (None features) still line up.
    """
    specs = features_for(ctx.sport)
    rows: list[dict] = []
    columns: list[str] = list(IDENTITY_COLS)
    seen_cols = set(columns)

    for game in slate:
        row: dict[str, object] = {
            "game_id": game.game_id,
            "sport": ctx.sport,
            "date": game.date,
            "away_team_id": game.away_team_id,
            "home_team_id": game.home_team_id,
            "status": game.status,
        }
        for spec in specs:
            if spec.side == "game":
                row.update(_flatten(spec.name, spec.fn(ctx, game, None)))
            else:
                for side_name, team_id in (("away", game.away_team_id),
                                           ("home", game.home_team_id)):
                    row.update(_flatten(f"{side_name}_{spec.name}",
                                        spec.fn(ctx, game, team_id)))
        for col in row:
            if col not in seen_cols:
                seen_cols.add(col)
                columns.append(col)
        rows.append(row)
    return columns, rows


def write_frame(sport: str, on: date, columns: list[str], rows: list[dict]) -> Path:
    path = FEATURES_DIR / sport.lower() / f"features_{on.isoformat()}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, restval="")
        w.writeheader()
        w.writerows(rows)
    return path
