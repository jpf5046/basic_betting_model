#!/usr/bin/env python3
"""The five-method score blend, shared by my_model (v1) and my_model_v2.

This is the part both models agree on: turn the season / common-opponent /
form / home / h2h estimates into a single (pred_away, pred_home) pair of
*mean* expected scores (my_model.md §2). Where the two models differ is
purely downstream — v1 pushes the margin through a logistic curve, v2 puts
a run distribution around these means — so that shared upstream logic lives
here, computed identically for both.

`blend_scores` returns None in exactly the case v1 did: no season baseline
for one of the teams (the "couldn't run" output). Otherwise it returns the
blended means plus the per-method estimates and normalized weights, so a
caller can still populate Prediction.method_details unchanged.
"""

from __future__ import annotations

from pipeline.models.base import gather_features

REQUIRED_FEATURES = [
    "season_scoring", "common_opponents", "last10_form",
    "head_to_head", "games_played",
]


def blend_scores(ctx, game, params) -> dict | None:
    """Blended mean scores for one game, or None if a team has no season
    baseline. Shape: {pred_away, pred_home, estimates, weights, features}."""
    f = gather_features(ctx, game, REQUIRED_FEATURES)
    ss_a, ss_h = f["away"]["season_scoring"], f["home"]["season_scoring"]
    if ss_a is None or ss_h is None:
        return None  # couldn't run: no season baseline for a team

    # Method 1 — season averages (also the fallback baseline).
    base_away = (ss_a["spg"] + ss_h["sapg"]) / 2
    base_home = (ss_h["spg"] + ss_a["sapg"]) / 2

    # Method 2 — common opponents.
    co_a, co_h = f["away"]["common_opponents"], f["home"]["common_opponents"]
    if co_a and co_h:
        common_away = (co_a["wrf"] + co_h["wra"]) / 2
        common_home = (co_h["wrf"] + co_a["wra"]) / 2
        common_weight = params["weights"]["common"]
    else:
        common_away, common_home = base_away, base_home
        common_weight = 0.0

    # Method 3 — recent form (0.5 L10_pct when a team has no data).
    l10_a, l10_h = f["away"]["last10_form"], f["home"]["last10_form"]
    pct_a = l10_a["l10_pct"] if l10_a else 0.5
    pct_h = l10_h["l10_pct"] if l10_h else 0.5
    form_a = params["form_base"] + pct_a * params["form_range"]
    form_h = params["form_base"] + pct_h * params["form_range"]
    form_away = base_away * form_a / form_h
    form_home = base_home * form_h / form_a

    # Method 4 — home-field adjustment.
    home_adj_away = base_away - params["home_adv"] / 2
    home_adj_home = base_home + params["home_adv"]

    # Method 5 — head-to-head, away team's perspective (my_model.md §2).
    h2h_a = f["away"]["head_to_head"]
    if h2h_a:
        h2h_away, h2h_home = h2h_a["avg_scored"], h2h_a["avg_allowed"]
        h2h_weight = params["weights"]["h2h"]
    else:
        h2h_away, h2h_home = base_away, base_home
        h2h_weight = 0.0

    # Blend with dropped weights renormalized to sum to 1.
    weights = {
        "season": params["weights"]["season"],
        "common": common_weight,
        "form": params["weights"]["form"],
        "home": params["weights"]["home"],
        "h2h": h2h_weight,
    }
    total_w = sum(weights.values())
    norm = {k: v / total_w for k, v in weights.items()}
    estimates = {
        "season": (base_away, base_home),
        "common": (common_away, common_home),
        "form": (form_away, form_home),
        "home": (home_adj_away, home_adj_home),
        "h2h": (h2h_away, h2h_home),
    }
    pred_away = sum(estimates[k][0] * norm[k] for k in norm)
    pred_home = sum(estimates[k][1] * norm[k] for k in norm)

    return {
        "pred_away": pred_away,
        "pred_home": pred_home,
        "estimates": estimates,
        "weights": norm,
        "features": f,
    }
