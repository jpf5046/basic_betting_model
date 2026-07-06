"""Model registry package (PLAN.md §4).

Importing this package auto-imports every module in pipeline/models/defs/,
so a new model is exactly one new file with an @register_model class —
same app-factory pattern as pipeline/features.
"""
import importlib
import pkgutil

import pipeline.features  # noqa: F401  (models consume registered features)
from pipeline.models import defs as _defs
from pipeline.models.base import Pick, Prediction, gather_features  # noqa: F401
from pipeline.models.config import (  # noqa: F401
    FACTORY_DEFAULTS,
    ConfigError,
    load_params,
    validate_params,
)
from pipeline.models.picks import make_picks, moneyline_lean, totals_lean  # noqa: F401
from pipeline.models.registry import all_models, get_model, register_model  # noqa: F401

for _mod in pkgutil.iter_modules(_defs.__path__):
    importlib.import_module(f"pipeline.models.defs.{_mod.name}")
