#!/usr/bin/env python3
"""Feature registry CLI.

    python3 -m pipeline.features list [--sport WNBA]
    python3 -m pipeline.features build --sport WNBA --date 2026-07-05 [--season 2026]

`build` loads the sport's games CSV (run the adapter's fetch first),
builds a point-in-time context as of --date, computes every registered
feature for that date's slate, and writes one wide CSV row per game to
data/features/<sport>/features_<date>.csv.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

import pipeline.features  # noqa: F401  (triggers defs/ auto-registration)
from pipeline.features.context import ADAPTERS, FeatureContext, default_season
from pipeline.features.frame import build_rows, write_frame
from pipeline.features.registry import all_features, features_for
from pipeline.ingest import core
from pipeline.ingest.core import EASTERN, todays_games


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.features", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="show registered features")
    ls.add_argument("--sport", choices=sorted(ADAPTERS))

    b = sub.add_parser("build", help="compute the feature frame for one date's slate")
    b.add_argument("--sport", required=True, choices=sorted(ADAPTERS))
    b.add_argument("--date", help="YYYY-MM-DD (default: today, US/Eastern)")
    b.add_argument("--season", help="override the season key of the games CSV")

    args = p.parse_args(argv)

    if args.cmd == "list":
        specs = features_for(args.sport) if args.sport else all_features()
        for s in specs:
            sports = ",".join(s.sports)
            print(f"{s.name:<18} v{s.version}  {s.side:<4}  [{sports}]  {s.description}")
        return

    on = date.fromisoformat(args.date) if args.date else datetime.now(EASTERN).date()
    season = args.season or default_season(args.sport, on)
    games = core.read_games_csv(
        ADAPTERS[args.sport].games_csv_path(season),
        f"python3 -m pipeline.ingest.{args.sport.lower()} fetch",
    )
    ctx = FeatureContext(args.sport, on, games)
    slate = todays_games(games, on)
    if not slate:
        sys.exit(f"no {args.sport} games on {on.isoformat()} — nothing to build")

    columns, rows = build_rows(ctx, slate)
    path = write_frame(args.sport, on, columns, rows)
    print(f"context: {len(ctx.past_games)} completed regular-season games before {on}")
    print(f"wrote {len(rows)} games x {len(columns)} columns -> {path}")


if __name__ == "__main__":
    main()
