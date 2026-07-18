#!/usr/bin/env python3
"""Run the whole daily pipeline locally, from the repo root:

    python3 run_daily.py [--date YYYY-MM-DD] [--sports WNBA,MLB] [--skip-odds]
                         [--offline] [--allow-partial]

This is the local face of the orchestrator (PLAN.md §7) — identical to
`python3 -m pipeline daily`. Stages, flags, and the daily report format
are documented in pipeline/orchestrator.py.
"""

from pipeline.orchestrator import main

if __name__ == "__main__":
    main()
