#!/usr/bin/env python3
"""Feature registry — PLAN.md §3, the "add travel time in one file" requirement.

A Feature is a small plugin: a function registered under a name that
computes one model input for one game, using only point-in-time data
served by a FeatureContext (see context.py). Models declare which
features they consume by name; the daily pipeline and the backtester
compute them the same way.

Registering:

    from pipeline.features.registry import register

    @register("travel_km", sports=["MLB", "NBA", "NHL", "WNBA"], version=1)
    def travel_km(ctx, game, team_id):
        ...

Contract:
  * side="team" (default): called twice per game, once per side, with
    team_id set. side="game": called once with team_id=None.
  * Return a float/int, a flat dict of them (multi-valued features like
    season scoring), or None when there is genuinely no data — models
    decide the fallback (My Model drops the method and renormalizes,
    my_model.md §2).
  * Bump `version` when the logic changes; old backtests stay
    attributable to the version that produced them.

Feature modules live in pipeline/features/defs/ — one file per feature.
pipeline/features/__init__ imports every module in that package, so a
new feature is exactly one new file; nothing else is touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

SPORTS = ("MLB", "NBA", "NHL", "WNBA")


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    fn: Callable
    sports: tuple[str, ...]
    version: int
    side: str  # "team" (computed for away and home) | "game" (once per game)
    description: str = field(default="", compare=False)


_REGISTRY: dict[str, FeatureSpec] = {}


def register(name: str, sports: list[str] | tuple[str, ...] = SPORTS,
             version: int = 1, side: str = "team"):
    """Decorator that adds a feature function to the registry."""
    if side not in ("team", "game"):
        raise ValueError(f"feature {name!r}: side must be 'team' or 'game', got {side!r}")
    unknown = set(sports) - set(SPORTS)
    if unknown:
        raise ValueError(f"feature {name!r}: unknown sports {sorted(unknown)}")

    def deco(fn: Callable) -> Callable:
        if name in _REGISTRY:
            raise ValueError(f"feature {name!r} is already registered")
        _REGISTRY[name] = FeatureSpec(
            name=name,
            fn=fn,
            sports=tuple(sports),
            version=version,
            side=side,
            description=(fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else "",
        )
        return fn

    return deco


def get_feature(name: str) -> FeatureSpec:
    if name not in _REGISTRY:
        raise KeyError(f"no feature named {name!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def features_for(sport: str) -> list[FeatureSpec]:
    """All features applicable to a sport, in stable name order."""
    return [s for _, s in sorted(_REGISTRY.items()) if sport in s.sports]


def all_features() -> list[FeatureSpec]:
    return [s for _, s in sorted(_REGISTRY.items())]
