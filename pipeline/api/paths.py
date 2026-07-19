#!/usr/bin/env python3
"""Where the console reads and writes on disk.

The console never invents new storage (console prompt): every read resolves
to a real artifact the pipeline already writes under ``data/``. The only
new files are the console's own config lifecycle metadata, which lives
under ``data/configs/`` (a directory the pipeline itself does not use).

Every function takes an explicit ``data_dir`` so the parsing/store layer is
trivially testable against a fixture tree — no globals, no import-time I/O.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from pipeline.ingest.core import REPO_ROOT

# The sports the console knows about — the same keys the pipeline registers.
SPORTS = ("MLB", "NBA", "NHL", "WNBA")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SPORT_RE = re.compile(r"^[A-Z]{2,5}$")


def default_data_dir() -> Path:
    return REPO_ROOT / "data"


def reports_dir(data_dir: Path) -> Path:
    return data_dir / "reports"


def predictions_dir(data_dir: Path, sport: str) -> Path:
    return data_dir / "predictions" / sport.lower()


def report_path(data_dir: Path, on: str) -> Path:
    return reports_dir(data_dir) / f"daily_{on}.md"


def predictions_path(data_dir: Path, sport: str, on: str) -> Path:
    return predictions_dir(data_dir, sport) / f"predictions_{on}.csv"


def picks_path(data_dir: Path, sport: str, on: str) -> Path:
    return predictions_dir(data_dir, sport) / f"picks_{on}.csv"


def grades_path(data_dir: Path, sport: str, on: str) -> Path:
    return predictions_dir(data_dir, sport) / f"grades_{on}.csv"


def frame_path(data_dir: Path, sport: str, season: str) -> Path:
    return data_dir / "frames" / f"{sport.lower()}_{season}.csv"


def teams_csv(data_dir: Path) -> Path:
    return data_dir / "teams.csv"


def configs_dir(data_dir: Path) -> Path:
    return data_dir / "configs"


# ------------------------------------------------------------ validation

def valid_date(value: str) -> bool:
    """A YYYY-MM-DD string that is also a real calendar date."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def valid_sport(value: str) -> bool:
    return isinstance(value, str) and value.upper() in SPORTS


def safe_name(value: str) -> bool:
    """A config name safe to use as a filename stem (no path tricks)."""
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value or ""))
