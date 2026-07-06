#!/usr/bin/env python3
"""Grading CLI.

    python3 -m pipeline.grading grade --sport WNBA --date 2026-07-05 [--season 2026]

Grades the picks published for --date (data/predictions/<sport>/
picks_<date>.csv) against the current games CSV — so run the sport's
`fetch` first to pull in the finals. Writes grades_<date>.csv next to the
picks file and prints the grade card. PENDING picks (game not final yet)
stay pending; re-run after the next fetch to resolve them.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from pipeline.features.context import ADAPTERS, default_season
from pipeline.ingest import core
from pipeline.ingest.core import EASTERN, REPO_ROOT
from pipeline.grading.grader import (
    grade_picks,
    read_picks_csv,
    summarize,
    write_grades_csv,
)

PREDICTIONS_DIR = REPO_ROOT / "data" / "predictions"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.grading", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("grade", help="grade one date's published picks")
    g.add_argument("--sport", required=True, choices=sorted(ADAPTERS))
    g.add_argument("--date", help="YYYY-MM-DD (default: yesterday, US/Eastern)")
    g.add_argument("--season", help="override the season key of the games CSV")

    args = p.parse_args(argv)

    if args.date:
        on = date.fromisoformat(args.date)
    else:
        on = date.fromordinal(datetime.now(EASTERN).date().toordinal() - 1)

    picks_path = PREDICTIONS_DIR / args.sport.lower() / f"picks_{on.isoformat()}.csv"
    if not picks_path.exists():
        sys.exit(f"{picks_path} not found — was `python3 -m pipeline.models predict "
                 f"--sport {args.sport} --date {on.isoformat()}` run that day?")
    picks = read_picks_csv(picks_path)
    if not picks:
        print(f"no picks were published for {args.sport} on {on.isoformat()} — nothing to grade")
        return

    season = args.season or default_season(args.sport, on)
    games = core.read_games_csv(
        ADAPTERS[args.sport].games_csv_path(season),
        f"python3 -m pipeline.ingest.{args.sport.lower()} fetch",
    )

    grades = grade_picks(picks, games)
    path = write_grades_csv(grades, picks_path.with_name(f"grades_{on.isoformat()}.csv"))

    for gr in grades:
        score = f"{gr.actual_away}-{gr.actual_home}" if gr.actual_away else gr.game_status
        sel = gr.selection if gr.bet_type == "TOTAL" else gr.selection.split("-")[-1].upper()
        line = f" {gr.line}" if gr.bet_type == "TOTAL" else ""
        pnl = f"{gr.pnl:+d}" if gr.result in ("WIN", "LOSS") else "0"
        print(f"{gr.date}  {gr.bet_type:<5} {sel}{line:<7} {gr.result:<8} {score:>9}  ${pnl}")

    s = summarize(grades)["overall"]
    extras = []
    if s["voids"]:
        extras.append(f"{s['voids']} void")
    if s["pending"]:
        extras.append(f"{s['pending']} pending")
    extra_txt = f" ({', '.join(extras)})" if extras else ""
    print(f"\nrecord {s['wins']}-{s['losses']}-{s['pushes']}{extra_txt}, "
          f"P&L ${s['pnl']:+d} at flat ${100} stakes")
    print(f"grades -> {path}")


if __name__ == "__main__":
    main()
