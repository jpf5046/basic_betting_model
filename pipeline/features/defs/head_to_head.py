"""Head-to-head scoring — my_model.md method 5."""
from pipeline.features.registry import register


@register("head_to_head", version=1)
def head_to_head(ctx, game, team_id):
    """This side's average scored/allowed across season meetings with the opponent."""
    other = game.home_team_id if team_id == game.away_team_id else game.away_team_id
    meetings = ctx.head_to_head(team_id, other)
    if not meetings:
        return None
    n = len(meetings)
    return {
        "games": n,
        "avg_scored": round(sum(r["scored"] for r in meetings) / n, 4),
        "avg_allowed": round(sum(r["allowed"] for r in meetings) / n, 4),
    }
