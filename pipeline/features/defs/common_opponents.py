"""Common-opponent scoring — my_model.md method 2 (highest-weighted method)."""
from pipeline.features.registry import register


@register("common_opponents", version=1)
def common_opponents(ctx, game, team_id):
    """This side's games-weighted scoring vs opponents shared with the other side."""
    other = game.home_team_id if team_id == game.away_team_id else game.away_team_id
    out = ctx.common_opponents(team_id, other)
    mine = out[team_id]
    if not mine["games"]:
        return None
    return {
        "opponents": len(out["common_opponents"]),
        "games": mine["games"],
        "wrf": mine["wRF"],
        "wra": mine["wRA"],
    }
