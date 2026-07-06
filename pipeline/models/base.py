#!/usr/bin/env python3
"""Shared model types and helpers (PLAN.md §4)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.features.registry import get_feature
from pipeline.ingest.core import Game


@dataclass
class Prediction:
    """Standardized model output for one game — everything downstream
    (pick policy, pick sheet, future ensemble/backtester) reads this."""

    game_id: str
    sport: str
    date: str
    away_team_id: str
    home_team_id: str
    pred_away: float          # exact blended score, drives everything
    pred_home: float
    disp_away: int            # rounded display score (my_model.md tie-break)
    disp_home: int
    pred_total: float
    pred_spread: float        # positive = away favored
    win_prob: float           # the predicted winner's probability, >= 0.5
    winner_team_id: str
    pred_confidence: str      # HIGH | MEDIUM | LOW (data-quality label)
    method_details: dict = field(default_factory=dict)


@dataclass
class Pick:
    """One published pick. TOSS-UP / PASS games produce no Pick rows —
    they live in the predictions output instead."""

    game_id: str
    sport: str
    date: str
    bet_type: str    # ML | TOTAL
    selection: str   # team_id for ML; OVER | UNDER for totals
    confidence: str  # STRONG | LEAN for ML; "" for totals
    line: str        # the gate/threshold that fired (no market lines yet)


def gather_features(ctx, game: Game, names: list[str]) -> dict:
    """Compute the named features for one game into a structured dict:
    {"away": {name: value}, "home": {...}, "game": {name: value}}."""
    out = {"away": {}, "home": {}, "game": {}}
    for name in names:
        spec = get_feature(name)
        if ctx.sport not in spec.sports:
            continue
        if spec.side == "game":
            out["game"][name] = spec.fn(ctx, game, None)
        else:
            out["away"][name] = spec.fn(ctx, game, game.away_team_id)
            out["home"][name] = spec.fn(ctx, game, game.home_team_id)
    return out
