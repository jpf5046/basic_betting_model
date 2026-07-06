#!/usr/bin/env python3
"""Canonical dataframe CLI.

    python3 -m pipeline.frame build --sport WNBA --season 2026 [--from --to]

Reads the season's games CSV (run the ingester fetch / backfill first)
and writes data/frames/<sport>/frame_<season>.csv — one row per game,
point-in-time features + outcomes.
"""

from __future__ import annotations

import argparse

from pipeline.features.context import ADAPTERS
from pipeline.frame import build_frame, write_frame_csv


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.frame", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build the season frame")
    b.add_argument("--sport", required=True, choices=sorted(ADAPTERS))
    b.add_argument("--season", required=True)
    b.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    b.add_argument("--to", dest="date_to", help="YYYY-MM-DD")

    args = p.parse_args(argv)
    columns, rows = build_frame(args.sport, args.season, args.date_from, args.date_to)
    path = write_frame_csv(args.sport, args.season, columns, rows)
    finals = sum(1 for r in rows if r.get("final_away") != "")
    print(f"wrote {len(rows)} games x {len(columns)} columns ({finals} with outcomes) -> {path}")


if __name__ == "__main__":
    main()
