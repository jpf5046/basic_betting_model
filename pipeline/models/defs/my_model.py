"""My Model v1 — the exact my_model.md specification as a model plugin.

Five weighted methods blend into a predicted score; the margin becomes a
win probability via a logistic curve; the evidence behind the prediction
scores a HIGH/MEDIUM/LOW confidence label. Every constant comes from the
params dict (pipeline/models/config.py factory defaults = the numbers in
my_model.md), so a saved config can retune any of it per sport.

Method summary (my_model.md §2):
  1. season   (0.30)  midpoints of own offense vs opponent defense
  2. common   (0.35)  same midpoints over shared opponents; dropped if none
  3. form     (0.20)  baseline scaled by the ratio of last-10 multipliers
  4. home     (0.10)  fixed home bump / half away debit
  5. h2h      (0.05)  season meetings, away team's perspective; dropped if none

predict() returns None when season scoring is missing for either team —
the model "couldn't run" case; no pick is ever fabricated.
"""

from __future__ import annotations

import math

from pipeline.models.base import Prediction
from pipeline.models.blend import REQUIRED_FEATURES, blend_scores
from pipeline.models.config import TIEBREAK_SPORTS
from pipeline.models.registry import register_model


@register_model("my_model")
class MyModel:
    required_features = REQUIRED_FEATURES

    def predict(self, ctx, game, params) -> Prediction | None:
        blend = blend_scores(ctx, game, params)
        if blend is None:
            return None  # couldn't run: no season baseline for a team
        pred_away, pred_home = blend["pred_away"], blend["pred_home"]
        estimates, norm, f = blend["estimates"], blend["weights"], blend["features"]

        # Win probability from the margin (my_model.md §3).
        p_away = 1.0 / (1.0 + math.exp(-(pred_away - pred_home) * params["k"]))
        if p_away > 0.5:
            winner, win_prob = game.away_team_id, p_away
        else:
            winner, win_prob = game.home_team_id, 1.0 - p_away

        disp_away, disp_home = self._display_scores(
            ctx.sport, pred_away, pred_home, winner == game.away_team_id
        )

        return Prediction(
            game_id=game.game_id,
            sport=ctx.sport,
            date=game.date,
            away_team_id=game.away_team_id,
            home_team_id=game.home_team_id,
            pred_away=round(pred_away, 4),
            pred_home=round(pred_home, 4),
            disp_away=disp_away,
            disp_home=disp_home,
            pred_total=round(pred_away + pred_home, 4),
            pred_spread=round(pred_away - pred_home, 4),
            win_prob=round(win_prob, 2),
            winner_team_id=winner,
            pred_confidence=self._confidence(params, f),
            method_details={
                "estimates": {k: (round(a, 4), round(h, 4)) for k, (a, h) in estimates.items()},
                "weights_used": {k: round(v, 6) for k, v in norm.items()},
            },
        )

    @staticmethod
    def _display_scores(sport: str, pred_away: float, pred_home: float,
                        away_wins: bool) -> tuple[int, int]:
        """Round for display; break a rounded tie toward the likelier
        winner in MLB/NHL (my_model.md §2 — basketball skips it)."""
        disp_away, disp_home = round(pred_away), round(pred_home)
        if disp_away == disp_home and sport in TIEBREAK_SPORTS:
            if away_wins:
                disp_away += 1
            else:
                disp_home += 1
        return disp_away, disp_home

    @staticmethod
    def _confidence(params: dict, f: dict) -> str:
        """Evidence score -> HIGH/MEDIUM/LOW label (my_model.md §5)."""
        tiers = params["confidence"]
        points = 0

        co = f["away"]["common_opponents"]
        n_common = co["opponents"] if co else 0
        for threshold, pts in tiers["common_tiers"]:  # highest tier only
            if n_common >= threshold:
                points += pts
                break

        if f["away"]["head_to_head"]:
            points += 1

        gp_a, gp_h = f["away"]["games_played"], f["home"]["games_played"]
        both_gp = min(gp_a or 0, gp_h or 0)
        for threshold, pts in tiers["gp_tiers"]:  # highest tier only
            if both_gp >= threshold:
                points += pts
                break

        if points >= 5:
            return "HIGH"
        if points >= 3:
            return "MEDIUM"
        return "LOW"
