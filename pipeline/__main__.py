#!/usr/bin/env python3
"""`python3 -m pipeline daily [...]` — the PLAN.md §7 entry point.

Same flags as `python3 run_daily.py`; both delegate to
pipeline/orchestrator.py.
"""

import sys

from pipeline import orchestrator

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit("usage: python3 -m pipeline daily [--date YYYY-MM-DD] [--sports ...] "
                 "[--skip-odds] [--offline]")
    if argv[0] != "daily":
        sys.exit(f"unknown command {argv[0]!r} — only `daily` exists (PLAN.md §7)")
    orchestrator.main(argv[1:])
