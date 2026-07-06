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

from pipeline.models.base import Prediction, gather_features
from pipeline.models.config import TIEBREAK_SPORTS
from pipeline.models.registry import register_model


@register_model("my_model")
class MyModel:
    required_features = [
        "season_scoring", "common_opponents", "last10_form",
        "head_to_head", "games_played",
    ]

    def predict(self, ctx, game, params) -> Prediction | None:
        f = gather_features(ctx, game, self.required_features)
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
