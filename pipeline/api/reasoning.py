#!/usr/bin/env python3
"""Reconstruct the "why" behind a pick from data already on disk.

``method_details`` is empty in the predictions CSV today (the model keeps
it in-memory only). Rather than invent a new backend field (the console
prompt is explicit: surface only what exists), we reconstruct the
*reasoning ingredients* — the active config weights next to the per-game
feature values from the canonical frame — so a pick is legible without
``method_details``.

Pure functions over already-parsed rows; the API layer supplies the frame
row, the prediction row, and the loaded params.
"""

from __future__ import annotations

WEIGHT_LABELS = {
    "season": "Season scoring",
    "common": "Common opponents",
    "form": "Recent form (L10)",
    "home": "Home advantage",
    "h2h": "Head-to-head",
}


def _f(row: dict, key: str):
    if row is None:
        return None
    val = row.get(key)
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return val


def _side_features(row: dict, side: str) -> dict:
    p = f"{side}_"
    return {
        "season_scoring": {
            "spg": _f(row, p + "season_scoring_spg"),
            "sapg": _f(row, p + "season_scoring_sapg"),
            "gp": _f(row, p + "season_scoring_gp"),
        },
        "common_opponents": {
            "games": _f(row, p + "common_opponents_games"),
            "wrf": _f(row, p + "common_opponents_wrf"),
            "wra": _f(row, p + "common_opponents_wra"),
        },
        "last10_form": {
            "wins": _f(row, p + "last10_form_wins"),
            "losses": _f(row, p + "last10_form_losses"),
            "otl": _f(row, p + "last10_form_otl"),
            "l10_pct": _f(row, p + "last10_form_l10_pct"),
        },
        "head_to_head": {
            "games": _f(row, p + "head_to_head_games"),
            "avg_scored": _f(row, p + "head_to_head_avg_scored"),
            "avg_allowed": _f(row, p + "head_to_head_avg_allowed"),
        },
        "travel_km": _f(row, p + "travel_km"),
    }


def weight_blend(params: dict) -> list[dict]:
    """The five method weights as an ordered, labelled list for the stacked
    proportion bar (Parameters + Model-weights screens)."""
    w = params.get("weights", {})
    return [
        {"key": k, "label": WEIGHT_LABELS[k], "weight": w.get(k, 0.0)}
        for k in ("season", "common", "form", "home", "h2h")
    ]


def build_reasoning(pred: dict, frame_row: dict | None, params: dict,
                    away_label: str, home_label: str) -> dict:
    """Assemble the reasoning ingredients for one prediction row.

    ``pred`` is a shaped predictions row; ``frame_row`` is the matchup's
    canonical frame row (or None if this game isn't in the frame yet).
    """
    method_details = pred.get("method_details") or ""
    return {
        "game_id": pred.get("game_id"),
        "away_label": away_label,
        "home_label": home_label,
        "has_frame": frame_row is not None,
        "method_details": method_details,       # rendered when the run recorded it
        "method_details_present": bool(method_details.strip()),
        "blend": weight_blend(params),
        "adjustments": {
            "home_adv": params.get("home_adv"),
            "form_base": params.get("form_base"),
            "form_range": params.get("form_range"),
            "k": params.get("k"),
        },
        "features": {
            "away": _side_features(frame_row, "away") if frame_row else None,
            "home": _side_features(frame_row, "home") if frame_row else None,
            "home_advantage": _f(frame_row, "home_advantage") if frame_row else None,
        },
        "prediction": {
            "pred_away": pred.get("pred_away"),
            "pred_home": pred.get("pred_home"),
            "disp_away": pred.get("disp_away"),
            "disp_home": pred.get("disp_home"),
            "pred_total": pred.get("pred_total"),
            "pred_spread": pred.get("pred_spread"),
            "win_prob": pred.get("win_prob"),
            "winner_team_id": pred.get("winner_team_id"),
            "pred_confidence": pred.get("pred_confidence"),
            "ml_lean": pred.get("ml_lean"),
            "total_lean": pred.get("total_lean"),
        },
    }
