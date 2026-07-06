#!/usr/bin/env python3
"""Historic backfill — PLAN.md item 7, and the contract that keeps it true
for every future sport.

    python3 -m pipeline.backfill --sport MLB --back 3
    python3 -m pipeline.backfill --sport all --back 3
    python3 -m pipeline.backfill --sport WNBA --seasons 2024,2023
    python3 -m pipeline.backfill --sport NHL --back 2 --offline

Fetches the current season plus the last `--back` seasons (or an explicit
--seasons list) into the same per-season games CSVs the daily fetch
writes — data/<sport>/games_<season>.csv — so features, predictions,
grading, odds matching, and the future backtester read history and today
through one code path. Failures are per-season: one bad season is
reported and the rest continue.

THE CONTRACT (why adding a sport automatically includes backfill):
every ingest adapter must expose two functions —

    run_fetch(season, offline=False) -> (games, csv_path)   # ANY season
    past_seasons(back) -> [season_key, ...]                 # newest first

This module is generic over that contract; it contains zero per-sport
logic. A new sport that implements the contract is backfillable the day
it lands, and one that doesn't will fail loudly here, not silently lack
history. Odds for backfilled seasons are a separate, quota-costing step:
`python3 -m pipeline.odds fetch --sport X --historical <ISO>` joins them
to these games through the same event map.
"""

from __future__ import annotations

import argparse
import sys

from pipeline.ingest import mlb, nba, nhl, wnba

ADAPTERS = {"MLB": mlb, "NBA": nba, "NHL": nhl, "WNBA": wnba}
CONTRACT = ("run_fetch", "past_seasons")


def seasons_for(sport: str, back: int, explicit: list[str] | None) -> list[str]:
    adapter = ADAPTERS[sport]
    missing = [fn for fn in CONTRACT if not hasattr(adapter, fn)]
    if missing:
        raise NotImplementedError(
            f"{sport} adapter doesn't implement the backfill contract "
            f"(missing {missing}) — every adapter must; see pipeline/backfill.py"
        )
    if explicit:
        return explicit
    return [None] + adapter.past_seasons(back)  # None = current season


def backfill(sport: str, back: int, explicit: list[str] | None,
             offline: bool) -> list[str]:
    """Run one sport; returns error strings (empty = clean)."""
    adapter = ADAPTERS[sport]
    errors: list[str] = []
    for season in seasons_for(sport, back, explicit):
        label = season or "current"
        try:
            games, path = adapter.run_fetch(season, offline=offline)
            finals = sum(1 for g in games if g.status == "final")
            print(f"{sport} {label}: {len(games)} games ({finals} final) -> {path}")
        except SystemExit as e:  # offline-cache misses exit; keep going
            errors.append(f"{sport} {label}: {e}")
            print(f"{sport} {label}: FAILED — {e}", file=sys.stderr)
        except Exception as e:
            errors.append(f"{sport} {label}: {e}")
            print(f"{sport} {label}: FAILED — {e}", file=sys.stderr)
    return errors


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.backfill", description=__doc__)
    p.add_argument("--sport", required=True, choices=sorted(ADAPTERS) + ["all"])
    p.add_argument("--back", type=int, default=3,
                   help="past seasons to fetch in addition to the current one (default 3)")
    p.add_argument("--seasons", help="explicit comma-separated season keys instead of --back")
    p.add_argument("--offline", action="store_true", help="reparse cached raw files only")
    args = p.parse_args(argv)

    explicit = args.seasons.split(",") if args.seasons else None
    sports = sorted(ADAPTERS) if args.sport == "all" else [args.sport]

    all_errors: list[str] = []
    for sport in sports:
        all_errors.extend(backfill(sport, args.back, explicit, args.offline))

    if all_errors:
        print(f"\n{len(all_errors)} season(s) failed:", file=sys.stderr)
        for e in all_errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    print("\nbackfill complete")


if __name__ == "__main__":
    main()
