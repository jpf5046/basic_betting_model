#!/usr/bin/env python3
"""Validate data/teams.csv — the canonical team registry.

Checks structure, uniqueness, coordinate ranges, IANA timezones,
external_ids JSON, and per-league team counts. Run from the repo root:

    python3 scripts/validate_teams.py

Exits non-zero with a list of problems if anything fails.
"""
import csv
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "teams.csv"

EXPECTED_FIELDS = [
    "team_id", "sport", "name", "abbrev", "conference", "division",
    "external_ids", "venue_name", "venue_lat", "venue_lon", "timezone",
]

# Update these when leagues expand/contract (or when CBB/WCBB get added).
EXPECTED_COUNTS = {
    "NFL": 32,
    "NBA": 30,
    "MLB": 30,
    "NHL": 32,
    "WNBA": 15,   # 2026: includes Golden State, Portland, Toronto expansions
    "CFB": 136,   # 2026 FBS membership
}

# Rough bounding box for North America (incl. Hawaii) — catches sign flips
# and transposed lat/lon, not small inaccuracies.
LAT_RANGE = (18.0, 60.0)
LON_RANGE = (-160.0, -60.0)


def main() -> int:
    errors: list[str] = []

    if not CSV_PATH.exists():
        print(f"FAIL: {CSV_PATH} does not exist")
        return 1

    with open(CSV_PATH, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != EXPECTED_FIELDS:
            errors.append(
                f"header mismatch: expected {EXPECTED_FIELDS}, got {reader.fieldnames}"
            )
        rows = list(reader)

    seen_ids: set[str] = set()
    seen_abbrevs: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}

    for i, row in enumerate(rows, start=2):  # line numbers incl. header
        where = f"line {i} ({row.get('team_id', '?')})"
        sport = row["sport"]
        counts[sport] = counts.get(sport, 0) + 1

        tid = row["team_id"]
        if tid in seen_ids:
            errors.append(f"{where}: duplicate team_id")
        seen_ids.add(tid)
        if not tid or tid != tid.lower() or " " in tid:
            errors.append(f"{where}: team_id must be a lowercase slug")
        if not tid.startswith(sport.lower() + "-"):
            errors.append(f"{where}: team_id must start with '{sport.lower()}-'")

        key = (sport, row["abbrev"])
        if not row["abbrev"]:
            errors.append(f"{where}: abbrev is empty")
        elif key in seen_abbrevs:
            errors.append(f"{where}: duplicate abbrev within {sport}")
        seen_abbrevs.add(key)

        if not row["name"]:
            errors.append(f"{where}: name is empty")

        try:
            ext = json.loads(row["external_ids"])
            if not isinstance(ext, dict):
                errors.append(f"{where}: external_ids must be a JSON object")
        except json.JSONDecodeError as e:
            errors.append(f"{where}: external_ids is not valid JSON ({e})")

        try:
            lat, lon = float(row["venue_lat"]), float(row["venue_lon"])
            if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1]):
                errors.append(f"{where}: venue_lat {lat} outside {LAT_RANGE}")
            if not (LON_RANGE[0] <= lon <= LON_RANGE[1]):
                errors.append(f"{where}: venue_lon {lon} outside {LON_RANGE}")
        except ValueError:
            errors.append(f"{where}: venue_lat/venue_lon not numeric")

        try:
            ZoneInfo(row["timezone"])
        except Exception:
            errors.append(f"{where}: unknown IANA timezone '{row['timezone']}'")

    for sport, expected in EXPECTED_COUNTS.items():
        actual = counts.get(sport, 0)
        if actual != expected:
            errors.append(f"{sport}: expected {expected} teams, found {actual}")
    for sport in counts:
        if sport not in EXPECTED_COUNTS:
            errors.append(
                f"{sport}: league present in CSV but has no expected count "
                "(add it to EXPECTED_COUNTS)"
            )

    if errors:
        print(f"FAIL: {len(errors)} problem(s) in {CSV_PATH}:")
        for e in errors:
            print(f"  - {e}")
        return 1

    total = sum(counts.values())
    per = ", ".join(f"{s}={n}" for s, n in sorted(counts.items()))
    print(f"OK: {total} teams ({per}), all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
