"""Last-10 form — my_model.md method 3 input.

L10_pct is wins/games over the last (up to) 10 completed games; the NHL
variant is points-based, (2xW + OTL) / (2 x games), per my_model.md's form
table. With fewer than 10 games played we use what exists rather than
dividing by a flat 10 — early-season form shouldn't be artificially cold.
Returns None with no games at all (the model's 0.5 fallback is a model
decision, not data).
"""
from pipeline.features.registry import register


@register("last10_form", version=1)
def last10_form(ctx, game, team_id):
    """Record and form percentage over the team's last <=10 games."""
    log = ctx.team_log(team_id)[-10:]
    if not log:
        return None
    wins = sum(1 for r in log if r["result"] == "W")
    otl = sum(1 for r in log if r["result"] == "OTL")
    losses = len(log) - wins - otl
    if ctx.sport == "NHL":
        pct = (2 * wins + otl) / (2 * len(log))
    else:
        pct = wins / len(log)
    return {"wins": wins, "losses": losses, "otl": otl, "l10_pct": round(pct, 4)}
