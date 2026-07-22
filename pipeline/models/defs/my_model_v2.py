"""My Model v2 — distributional scoring on top of the v1 blend.

Same five-method blended means as v1 (imported wholesale from
pipeline.models.blend, so the two models never drift on that part), but two
deliberate changes downstream, each aimed at a specific failure the backtest
exposed in v1:

  1. Park factors (config `park_factors`) scale the game's run environment
     by the home venue — the one cheap source of *real* totals signal v1
     ignored entirely.

  2. A Negative-Binomial run distribution replaces the logistic win-prob
     curve. Win probability is P(away runs > home runs) read off the implied
     outcome distribution (ties renormalized away — baseball can't tie), and
     the totals side gets a genuine P(over)/P(under) instead of a point
     estimate against a dead-zone threshold. This is what fixes v1's
     overconfidence and its structural all-OVER bias.

The predicted mean scores (and therefore the displayed score) are unchanged
from v1 by design — Tier 1 is about calibrated *probabilities*, not about
inventing score spread the inputs don't support. `method_details` carries
the distributional extras (p_over, p_home_win, park factor) so pick policy
and the dashboard can use them.
"""

from __future__ import annotations

from pipeline.models.base import Prediction
from pipeline.models.blend import REQUIRED_FEATURES, blend_scores
from pipeline.models.config import TIEBREAK_SPORTS
from pipeline.models.defs.my_model import MyModel
from pipeline.models.registry import register_model
from pipeline.models.scoring_dist import outcome_probabilities

# Fallbacks when a sport's config predates the v2 params (keeps v2 runnable
# on any sport; MLB ships tuned values in config.py).
DEFAULT_DISPERSION = 4.0


@register_model("my_model_v2")
class MyModelV2:
    required_features = REQUIRED_FEATURES

    def predict(self, ctx, game, params) -> Prediction | None:
        blend = blend_scores(ctx, game, params)
        if blend is None:
            return None
        pred_away, pred_home = blend["pred_away"], blend["pred_home"]
        estimates, norm, f = blend["estimates"], blend["weights"], blend["features"]

        # 1) Park factor for the home venue scales the run environment.
        pf = params.get("park_factors", {}).get(game.home_team_id, 1.0)
        mu_away, mu_home = pred_away * pf, pred_home * pf

        # 2) Run distribution -> calibrated probabilities.
        r = params.get("nb_dispersion", DEFAULT_DISPERSION)
        exp_total = mu_away + mu_home
        probs = outcome_probabilities(mu_away, mu_home, r, line=exp_total)

        # Win prob with ties renormalized away (no ties in these leagues).
        decided = probs["p_away_win"] + probs["p_home_win"]
        p_away = probs["p_away_win"] / decided if decided else 0.5
        if p_away >= 0.5:
            winner, win_prob = game.away_team_id, p_away
        else:
            winner, win_prob = game.home_team_id, 1.0 - p_away

        disp_away, disp_home = MyModel._display_scores(
            ctx.sport, mu_away, mu_home, winner == game.away_team_id
        )

        return Prediction(
            game_id=game.game_id,
            sport=ctx.sport,
            date=game.date,
            away_team_id=game.away_team_id,
            home_team_id=game.home_team_id,
            pred_away=round(mu_away, 4),
            pred_home=round(mu_home, 4),
            disp_away=disp_away,
            disp_home=disp_home,
            pred_total=round(exp_total, 4),
            pred_spread=round(mu_away - mu_home, 4),
            win_prob=round(win_prob, 2),
            winner_team_id=winner,
            pred_confidence=MyModel._confidence(params, f),
            method_details={
                "estimates": {k: (round(a, 4), round(h, 4)) for k, (a, h) in estimates.items()},
                "weights_used": {k: round(v, 6) for k, v in norm.items()},
                "park_factor": pf,
                "nb_dispersion": r,
                "p_home_win": round(probs["p_home_win"], 4),
                "p_over_exp_total": round(probs["p_over"], 4),
            },
        )
