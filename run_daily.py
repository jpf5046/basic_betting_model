#!/usr/bin/env python3
"""Run the whole daily pipeline locally, from the repo root:

    python3 run_daily.py [--date YYYY-MM-DD] [--sports WNBA,MLB] [--skip-odds]
                         [--offline] [--allow-partial] [--skip-db-load]

This is the local face of the orchestrator (PLAN.md §7) — identical to
`python3 -m pipeline daily`. Stages, flags, and the daily report format
are documented in pipeline/orchestrator.py.

After the run it reloads the flat CSVs into the DB mirror the dashboard
reads from (so today's predictions show up without a manual
`python3 -m pipeline.db load`); pass --skip-db-load to skip that.
"""

from pipeline.orchestrator import main

if __name__ == "__main__":
    main()
