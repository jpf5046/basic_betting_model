"""Games played — evidence input for the my_model.md §5 confidence score."""
from pipeline.features.registry import register


@register("games_played", version=1)
def games_played(ctx, game, team_id):
    """Completed regular-season games before as_of."""
    return ctx.games_played(team_id)
