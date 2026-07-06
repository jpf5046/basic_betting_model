"""Feature registry package (PLAN.md §3).

Importing this package auto-imports every module in pipeline/features/defs/,
which is what makes "one new file = one new feature" literal: drop a module
with an @register-decorated function into defs/ and it is live everywhere —
the CLI, the feature frame, and any model that asks for it by name.
"""
import importlib
import pkgutil

from pipeline.features import defs as _defs
from pipeline.features.context import FeatureContext  # noqa: F401
from pipeline.features.registry import (  # noqa: F401
    FeatureSpec,
    all_features,
    features_for,
    get_feature,
    register,
)

for _mod in pkgutil.iter_modules(_defs.__path__):
    importlib.import_module(f"pipeline.features.defs.{_mod.name}")
