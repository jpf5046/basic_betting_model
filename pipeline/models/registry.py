#!/usr/bin/env python3
"""Model registry — PLAN.md §4, the model half of the app factory.

A Model is a plugin: a class registered under a name that consumes named
features (via the point-in-time FeatureContext) and a params dict, and
emits a standardized Prediction. Pick policy is deliberately separate
(picks.py) so "same predictions, different gates" is a cheap experiment.

Registering:

    from pipeline.models.registry import register_model

    @register_model("my_model")
    class MyModel:
        required_features = ["season_scoring", ...]
        def predict(self, ctx, game, params) -> Prediction | None: ...

Model modules live in pipeline/models/defs/ — one file per model,
auto-imported by the package, so a new model is one new file.
predict() returning None means the model could not run for that game
(missing inputs); sitting out is an output (PLAN.md §0.3), never an
invented number.
"""

from __future__ import annotations

_MODELS: dict[str, object] = {}


def register_model(name: str):
    """Decorator that instantiates and registers a model class."""

    def deco(cls):
        if name in _MODELS:
            raise ValueError(f"model {name!r} is already registered")
        instance = cls()
        instance.name = name
        _MODELS[name] = instance
        return cls

    return deco


def get_model(name: str):
    if name not in _MODELS:
        raise KeyError(f"no model named {name!r}; registered: {sorted(_MODELS)}")
    return _MODELS[name]


def all_models() -> list:
    return [m for _, m in sorted(_MODELS.items())]
