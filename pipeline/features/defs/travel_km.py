"""Travel distance — the PLAN.md §3 canary feature.

Great-circle distance (haversine) from the venue of the team's most recent
game to today's game venue, using the coordinates that have been sitting in
data/teams.csv since day one. A past game's venue is the home side's
registry venue (neutral-site games aren't modeled yet); today's venue is
the home team's. Returns None before a team's first game.

This file is the acceptance test of the registry design: it was added
without touching the pipeline, the context, or any other feature.
"""
from math import asin, cos, radians, sin, sqrt

from pipeline.features.registry import register

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


@register("travel_km", version=1)
def travel_km(ctx, game, team_id):
    """Km from the venue of the team's previous game to today's venue."""
    log = ctx.team_log(team_id)
    if not log:
        return None
    last = log[-1]
    prev_venue_team = team_id if last["is_home"] else last["opponent_id"]
    prev = ctx.team(prev_venue_team)
    here = ctx.team(game.home_team_id)
    return round(haversine_km(prev.venue_lat, prev.venue_lon, here.venue_lat, here.venue_lon), 1)
