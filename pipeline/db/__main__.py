#!/usr/bin/env python3
"""Database CLI (PLAN.md §1). Postgres when DATABASE_URL is set, else
the local SQLite file data/model.db — same commands either way:

    python3 -m pipeline.db init                       # create tables
    python3 -m pipeline.db load [--sports WNBA,MLB] [--season KEY]
    python3 -m pipeline.db status                     # row counts

`load` upserts data/teams.csv, each sport's games CSV, and the backfilled
canonical frame (run `python3 -m pipeline.backfill` first for that part).
The CSVs remain the source of truth — the database is a rebuildable
mirror, so `load` is always safe to re-run.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from pipeline.db.core import Db
from pipeline.db.loader import load_all
from pipeline.features.context import ADAPTERS
from pipeline.ingest.core import EASTERN


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.db", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--database-url", help="overrides the DATABASE_URL env var")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the tables (idempotent)")
    sub.add_parser("status", help="row counts per table")

    ld = sub.add_parser("load", help="upsert teams, games, and backfilled frames")
    ld.add_argument("--sports", default=",".join(sorted(ADAPTERS)),
                    help=f"comma-separated subset (default: {','.join(sorted(ADAPTERS))})")
    ld.add_argument("--season", help="season key override (default: current per sport)")

    args = p.parse_args(argv)

    with Db.connect(url=args.database_url) as db:
        target = "postgres (DATABASE_URL)" if db.dialect == "postgres" else "sqlite data/model.db"
        if args.cmd == "init":
            db.init_schema()
            print(f"schema ready on {target}")
        elif args.cmd == "status":
            db.init_schema()
            for table, n in db.counts().items():
                print(f"{table:<8} {n:>7} rows")
            print(f"({target})")
        elif args.cmd == "load":
            sports = [s.strip().upper() for s in args.sports.split(",") if s.strip()]
            unknown = sorted(set(sports) - set(ADAPTERS))
            if unknown:
                sys.exit(f"unknown sport(s) {', '.join(unknown)} — "
                         f"choose from {', '.join(sorted(ADAPTERS))}")
            on = datetime.now(EASTERN).date()
            report = load_all(db, sports, args.season, on)
            print(f"loaded into {target}:")
            print(f"  teams: {report['teams']['all']} rows")
            for sport in sports:
                r = report[sport]
                note = "" if r["frames"] else "   (no frame CSV — run python3 -m pipeline.backfill?)"
                print(f"  {sport}: {r['games']} games, {r['frames']} frame rows, "
                      f"{r['predictions']} predictions, {r['picks']} picks, "
                      f"{r['grades']} grades{note}")


if __name__ == "__main__":
    main()
