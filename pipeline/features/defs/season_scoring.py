"""Season scoring averages — my_model.md §1 "Scoring per game / allowed"."""
from pipeline.features.registry import register


@register("season_scoring", version=1)
def season_scoring(ctx, game, team_id):
    """Scoring per game (spg), allowed per game (sapg), and games played."""
    log = ctx.team_log(team_id)
    if not log:
        return None
    n = len(log)
    return {
        "spg": round(sum(r["scored"] for r in log) / n, 4),
        "sapg": round(sum(r["allowed"] for r in log) / n, 4),
        "gp": n,
    }
