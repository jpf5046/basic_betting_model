#!/usr/bin/env python3
"""Model parameters — factory defaults, validation, and config loading.

Configs are data, not code (PLAN.md §0.5). Every constant in my_model.md
lives here per sport; a saved config is a JSON file of partial overrides
that deep-merges over the factory defaults. The validation rules are the
same ones the future Researcher Console editor will surface (weights sum
to 1.000, ordered gates, etc.) — written once, in the backend.

MLB / NBA / NHL numbers are straight from my_model.md. The WNBA is not
in that document, so its sport-scale constants (home_adv, k, totals
thresholds, confidence tiers) are provisional NBA-scaled defaults —
flagged for tuning by backtest.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

WEIGHT_KEYS = ("season", "common", "form", "home", "h2h")

FACTORY_DEFAULTS: dict[str, dict] = {
    "MLB": {
        "weights": {"season": 0.30, "common": 0.35, "form": 0.20, "home": 0.10, "h2h": 0.05},
        "home_adv": 0.35,
        "form_base": 0.80,
        "form_range": 0.40,
        "k": 0.40,
        "ml_gates": {"strong": 0.60, "lean": 0.55},
        "totals": {"over": 9.2, "under": 7.8},
        "confidence": {"common_tiers": [[15, 3], [8, 2], [3, 1]], "gp_tiers": [[80, 2], [40, 1]]},
    },
    "NBA": {
        "weights": {"season": 0.30, "common": 0.35, "form": 0.20, "home": 0.10, "h2h": 0.05},
        "home_adv": 3.0,
        "form_base": 0.85,
        "form_range": 0.30,
        "k": 0.15,
        "ml_gates": {"strong": 0.60, "lean": 0.55},
        "totals": {"over": 225.0, "under": 215.0},
        "confidence": {"common_tiers": [[15, 3], [8, 2], [4, 1]], "gp_tiers": [[50, 2], [30, 1]]},
    },
    "NHL": {
        "weights": {"season": 0.30, "common": 0.35, "form": 0.20, "home": 0.10, "h2h": 0.05},
        "home_adv": 0.25,
        "form_base": 0.80,
        "form_range": 0.40,
        "k": 0.70,
        "ml_gates": {"strong": 0.60, "lean": 0.55},
        "totals": {"over": 6.3, "under": 5.7},
        "confidence": {"common_tiers": [[10, 3], [5, 2], [2, 1]], "gp_tiers": [[50, 2], [30, 1]]},
    },
    # Provisional (not in my_model.md) — NBA-scaled for a 44-game season.
    "WNBA": {
        "weights": {"season": 0.30, "common": 0.35, "form": 0.20, "home": 0.10, "h2h": 0.05},
        "home_adv": 2.5,
        "form_base": 0.85,
        "form_range": 0.30,
        "k": 0.15,
        "ml_gates": {"strong": 0.60, "lean": 0.55},
        "totals": {"over": 168.0, "under": 158.0},
        "confidence": {"common_tiers": [[10, 3], [5, 2], [2, 1]], "gp_tiers": [[30, 2], [20, 1]]},
    },
}

# Sports where a rounded display tie gets broken toward the likelier
# winner (my_model.md §2: NBA skips it; the WNBA follows the NBA).
TIEBREAK_SPORTS = ("MLB", "NHL")


class ConfigError(ValueError):
    pass


def validate_params(sport: str, params: dict) -> None:
    """Raise ConfigError listing every problem, or return silently."""
    errors: list[str] = []
    w = params.get("weights", {})
    missing = [k for k in WEIGHT_KEYS if k not in w]
    if missing:
        errors.append(f"weights missing {missing}")
    else:
        total = sum(w[k] for k in WEIGHT_KEYS)
        if abs(total - 1.0) > 1e-6:
            errors.append(f"weights must sum to 1.000, got {total:.4f}")
        if any(w[k] < 0 for k in WEIGHT_KEYS):
            errors.append("weights must be non-negative")

    gates = params.get("ml_gates", {})
    if not (1.0 > gates.get("strong", 0) > gates.get("lean", 0) > 0.5):
        errors.append("ml_gates must satisfy 1.0 > strong > lean > 0.5")

    totals = params.get("totals", {})
    if not totals.get("over", 0) > totals.get("under", 0) > 0:
        errors.append("totals must satisfy over > under > 0")

    if params.get("k", 0) <= 0:
        errors.append("k must be positive")
    if params.get("form_base", 0) <= 0 or params.get("form_range", -1) < 0:
        errors.append("form_base must be positive and form_range non-negative")
    if params.get("home_adv", -1) < 0:
        errors.append("home_adv must be non-negative")

    if errors:
        raise ConfigError(f"{sport} params invalid: " + "; ".join(errors))


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_params(sport: str, config_path: str | Path | None = None) -> dict:
    """Factory defaults for the sport, deep-merged with an optional JSON
    override file. The file may be flat params or keyed by sport."""
    if sport not in FACTORY_DEFAULTS:
        raise ConfigError(f"no factory defaults for sport {sport!r}")
    params = copy.deepcopy(FACTORY_DEFAULTS[sport])
    if config_path is not None:
        overrides = json.loads(Path(config_path).read_text())
        if sport in overrides:  # file keyed by sport
            overrides = overrides[sport]
        params = _deep_merge(params, overrides)
    validate_params(sport, params)
    return params
