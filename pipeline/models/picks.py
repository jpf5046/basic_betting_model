#!/usr/bin/env python3
"""Pick policy — my_model.md §4, separate from prediction (PLAN.md §4).

Turns a Prediction into zero, one, or two Picks using the gates in the
params dict. TOSS-UP and PASS produce no Pick — sitting out is a
deliberate output, recorded on the prediction sheet, not here.
"""

from __future__ import annotations

from pipeline.models.base import Pick, Prediction


def moneyline_lean(pred: Prediction, params: dict) -> str:
    """STRONG | LEAN | TOSS-UP from the win-probability gates."""
    gates = params["ml_gates"]
    if pred.win_prob >= gates["strong"]:
        return "STRONG"
    if pred.win_prob >= gates["lean"]:
        return "LEAN"
    return "TOSS-UP"


def totals_lean(pred: Prediction, params: dict) -> str:
    """OVER | UNDER | PASS from the fixed per-sport total thresholds."""
    totals = params["totals"]
    if pred.pred_total >= totals["over"]:
        return "OVER"
    if pred.pred_total <= totals["under"]:
        return "UNDER"
    return "PASS"


def make_picks(pred: Prediction, params: dict) -> list[Pick]:
    """The published rows for one game (my_model.md 'What gets published')."""
    picks: list[Pick] = []

    ml = moneyline_lean(pred, params)
    if ml != "TOSS-UP":
        picks.append(Pick(
            game_id=pred.game_id, sport=pred.sport, date=pred.date,
            bet_type="ML", selection=pred.winner_team_id, confidence=ml,
            line=f"{params['ml_gates']['lean' if ml == 'LEAN' else 'strong']:g}",
        ))

    total = totals_lean(pred, params)
    if total != "PASS":
        threshold = params["totals"]["over" if total == "OVER" else "under"]
        picks.append(Pick(
            game_id=pred.game_id, sport=pred.sport, date=pred.date,
            bet_type="TOTAL", selection=total, confidence="",
            line=f"{threshold:g}",
        ))
    return picks
