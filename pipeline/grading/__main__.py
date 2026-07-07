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
    g.add_argument("--no-odds", action="store_true",
                   help="force flat $100 grading even when matched odds exist")

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

    odds_lookup = None if args.no_odds else _make_odds_lookup(args.sport)
    grades = grade_picks(picks, games, odds_lookup)
    path = write_grades_csv(grades, picks_path.with_name(f"grades_{on.isoformat()}.csv"))

    for gr in grades:
        score = f"{gr.actual_away}-{gr.actual_home}" if gr.actual_away else gr.game_status
        sel = gr.selection if gr.bet_type == "TOTAL" else gr.selection.split("-")[-1].upper()
        line = f" {gr.line}" if gr.bet_type == "TOTAL" else ""
        at = f" @ {gr.price_american}" if gr.price_american else ""
        pnl = f"{gr.pnl:+.2f}" if gr.result in ("WIN", "LOSS") else "0"
        print(f"{gr.date}  {gr.bet_type:<5} {sel}{line:<7}{at:<9} {gr.result:<8} "
              f"{score:>9}  ${pnl}")

    s = summarize(grades)["overall"]
    extras = []
    if s["voids"]:
        extras.append(f"{s['voids']} void")
    if s["pending"]:
        extras.append(f"{s['pending']} pending")
    extra_txt = f" ({', '.join(extras)})" if extras else ""
    priced = sum(1 for gr in grades if gr.price_american)
    basis = (f"{priced}/{len(grades)} picks at market prices" if priced
             else "flat $100 stakes (no matched odds)")
    print(f"\nrecord {s['wins']}-{s['losses']}-{s['pushes']}{extra_txt}, "
          f"P&L ${s['pnl']:+.2f} — {basis}")
    print(f"grades -> {path}")


def _make_odds_lookup(sport: str):
    """Join picks to consensus market prices via the odds event map.
    Returns None when no odds data exists for the sport yet."""
    from pipeline.odds.match import load_event_map
    from pipeline.odds.store import consensus_h2h, consensus_total, load_snapshot_rows

    emap = load_event_map(sport)
    rows = load_snapshot_rows(sport)
    if not emap or not rows:
        return None
    by_game = {r["game_id"]: r["event_id"] for r in emap.values()}

    def lookup(pick: dict):
        event_id = by_game.get(pick["game_id"])
        if not event_id:
            return None, None
        if pick["bet_type"] == "ML":
            h2h = consensus_h2h(rows, event_id, pick["selection"])
            return (h2h[0], None) if h2h else (None, None)
        total = consensus_total(rows, event_id, pick["selection"])
        return (total[1], total[0]) if total else (None, None)

    return lookup


if __name__ == "__main__":
    main()
