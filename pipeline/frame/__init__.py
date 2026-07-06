#!/usr/bin/env python3
"""The canonical dataframe — PLAN.md §5 / build-order item 8.

One call returns the leak-free modeling table for a season: one row per
game, every registered feature for both sides computed AS OF that game's
date (a fresh point-in-time context per date — the same code path the
daily run uses), plus the outcome columns once the game is final.

    from pipeline.frame import build_frame
    columns, rows = build_frame("WNBA", "2026")

This is the dataset the backtester replays and the future ML-fitting
dataset. Stored as CSV (the repo is stdlib-only; parquet becomes worth it
when pandas enters the picture — the columns are already flat and typed
for that day).
"""

from __future__ import annotations

import csv
from datetime import date as date_cls
from pathlib import Path

from pipeline.features.context import ADAPTERS, FeatureContext
from pipeline.features.frame import build_rows
from pipeline.ingest import core
from pipeline.ingest.core import REPO_ROOT, Game

FRAMES_DIR = REPO_ROOT / "data" / "frames"

OUTCOME_COLS = ["final_away", "final_home", "actual_total", "actual_margin",
                "winner_team_id", "ot"]


def outcome_fields(game: Game) -> dict:
    """Outcome columns for one game — blank unless final (never guessed)."""
    if game.status != "final" or not (game.away_score and game.home_score):
        return {c: "" for c in OUTCOME_COLS}
    away, home = int(game.away_score), int(game.home_score)
    winner = game.away_team_id if away > home else game.home_team_id if home > away else ""
    return {
        "final_away": away,
        "final_home": home,
        "actual_total": away + home,
        "actual_margin": away - home,  # positive = away won by that much
        "winner_team_id": winner,
        "ot": game.ot,
    }


def load_games(sport: str, season: str, games: list[Game] | None = None) -> list[Game]:
    if games is not None:
        return games
    return core.read_games_csv(
        ADAPTERS[sport].games_csv_path(season),
        f"python3 -m pipeline.backfill --sport {sport} --seasons {season}",
    )


def build_frame(sport: str, season: str, date_from: str | None = None,
                date_to: str | None = None, games: list[Game] | None = None,
                ) -> tuple[list[str], list[dict]]:
    """One row per game in the range, features as-of the game date plus
    outcomes. Returns (columns, rows) with a stable union column set."""
    games = load_games(sport, season, games)
    in_range = [g for g in games if g.date
                and (date_from is None or g.date >= date_from)
                and (date_to is None or g.date <= date_to)]

    columns: list[str] = []
    seen = set()
    rows: list[dict] = []
    for day in sorted({g.date for g in in_range}):
        ctx = FeatureContext(sport, date_cls.fromisoformat(day), games)
        slate = [g for g in in_range if g.date == day]
        day_cols, day_rows = build_rows(ctx, slate)
        by_id = {g.game_id: g for g in slate}
        for row in day_rows:
            row.update(outcome_fields(by_id[row["game_id"]]))
        for col in day_cols + OUTCOME_COLS:
            if col not in seen:
                seen.add(col)
                columns.append(col)
        rows.extend(day_rows)
    return columns, rows


def frame_csv_path(sport: str, season: str) -> Path:
    return FRAMES_DIR / sport.lower() / f"frame_{season}.csv"


def write_frame_csv(sport: str, season: str, columns: list[str],
                    rows: list[dict]) -> Path:
    path = frame_csv_path(sport, season)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, restval="")
        w.writeheader()
        w.writerows(rows)
    return path
