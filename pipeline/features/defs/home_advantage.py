"""Home advantage in scoring units — my_model.md method 4 factory defaults.

These are the sport-level factory constants (runs / points / goals). A
model config can override its own copy; this feature exists so the value
is visible in the feature frame next to everything else. WNBA is not in
my_model.md — 2.5 points is a provisional NBA-scaled default, tune via
backtest.
"""
from pipeline.features.registry import register

HOME_ADV = {"MLB": 0.35, "NBA": 3.0, "NHL": 0.25, "WNBA": 2.5}


@register("home_advantage", version=1, side="game")
def home_advantage(ctx, game, team_id):
    """Factory home-advantage constant for this sport, in scoring units."""
    return HOME_ADV[ctx.sport]
