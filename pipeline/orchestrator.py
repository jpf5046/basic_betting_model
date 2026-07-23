#!/usr/bin/env python3
"""Daily orchestrator — PLAN.md §7, the centerpiece.

One run chains the whole morning loop as an ordered DAG of idempotent
stages, per sport, each safe to re-run for any date:

    1. INGEST    fetch the schedule feed (yesterday's finals + today's slate)
    2. GRADE     grade the picks published yesterday (skipped if none were)
    3. PREDICT   run the model over today's slate — feature computation
                 happens inside via the point-in-time FeatureContext
    4. ODDS      fetch today's odds + event matching, then the edge report
                 (skipped when ODDS_API_KEY is not set or --skip-odds)
    5. REPORT    write data/reports/daily_<date>.md — grade card, pick
                 sheet, edge report, and the stage log — and print a summary
    6. MIRROR    reload the CSVs into the DB mirror the dashboard reads from
                 (best-effort, non-fatal; skipped with --skip-db-load)

Usage (from the repo root — both entry points are identical):

    python3 run_daily.py            [--date YYYY-MM-DD] [--sports WNBA,MLB]
                                    [--skip-odds] [--offline]
    python3 -m pipeline daily       [same flags]

Design notes:
  * Each stage shells out to the existing CLI (`python3 -m pipeline.…`)
    with output captured, so a sys.exit or crash in one sport's stage is
    recorded as a failed stage instead of killing the run, and the CLI
    and the orchestrator can never disagree (PLAN.md §8 principle).
  * A sport with no games today short-circuits to a SKIPPED predict/odds
    stage ("off-season" status), not an error (PLAN.md §7).
  * Re-running for a missed day is `python3 run_daily.py --date 2026-07-03`.
  * Still open from §7: standings snapshots (blocked on §2's standings
    ingestion) and failure notifications (start with the Actions cron in
    .github/workflows/daily.yml once it's enabled).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from pipeline.features.context import ADAPTERS, default_season
from pipeline.ingest import core
from pipeline.ingest.core import EASTERN, REPO_ROOT
from pipeline.odds.api import SPORT_KEYS as ODDS_SPORT_KEYS

PREDICTIONS_DIR = REPO_ROOT / "data" / "predictions"
REPORTS_DIR = REPO_ROOT / "data" / "reports"
DEFAULT_SPORTS = sorted(ADAPTERS)  # MLB, NBA, NHL, WNBA

STATUS_MARK = {"ok": " ok ", "skipped": "skip", "failed": "FAIL"}


@dataclass
class StageResult:
    stage: str   # ingest | grade | predict | odds | edge
    sport: str
    status: str  # ok | skipped | failed
    detail: str = ""   # one-liner for the stage log
    output: str = ""   # full captured stdout+stderr (report body material)


def run_cli(module_argv: list[str]) -> tuple[int, str]:
    """Run one pipeline CLI (`python3 -m <module> …`) with output captured."""
    proc = subprocess.run(
        [sys.executable, "-m", *module_argv],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    out = proc.stdout.strip()
    if proc.stderr.strip():
        out = (out + "\n" + proc.stderr.strip()).strip()
    return proc.returncode, out


def picks_path(sport: str, on: date) -> Path:
    return PREDICTIONS_DIR / sport.lower() / f"picks_{on.isoformat()}.csv"


def predictions_path(sport: str, on: date) -> Path:
    return PREDICTIONS_DIR / sport.lower() / f"predictions_{on.isoformat()}.csv"


def todays_game_count(sport: str, on: date) -> int | None:
    """Games on the slate for `on`, or None when the games CSV is missing."""
    path = ADAPTERS[sport].games_csv_path(default_season(sport, on))
    if not path.exists():
        return None
    games = core.read_games_csv(
        path, f"python3 -m pipeline.ingest.{sport.lower()} fetch"
    )
    return len(core.todays_games(games, on))


def daily(on: date, sports: list[str], skip_odds: bool = False,
          offline: bool = False, runner=run_cli) -> list[StageResult]:
    """Run the DAG for one date and return every stage's result."""
    yesterday = on - timedelta(days=1)
    results: list[StageResult] = []

    def record(stage: str, sport: str, status: str, detail: str = "", output: str = "") -> None:
        results.append(StageResult(stage, sport, status, detail, output))
        print(f"[{STATUS_MARK[status]}] {stage:<8} {sport:<5} {detail}")

    def run_stage(stage: str, sport: str, argv: list[str]) -> bool:
        code, out = runner(argv)
        if code == 0:
            last = out.splitlines()[-1] if out else ""
            record(stage, sport, "ok", last, out)
            return True
        record(stage, sport, "failed", f"exit {code} — see report for output", out)
        return False

    # 1. INGEST — one feed fetch brings yesterday's finals and today's slate.
    for sport in sports:
        argv = [f"pipeline.ingest.{sport.lower()}", "fetch"]
        if offline:
            argv.append("--offline")
        run_stage("ingest", sport, argv)

    # 2. GRADE yesterday's published picks against the freshly fetched finals.
    for sport in sports:
        if not picks_path(sport, yesterday).exists():
            record("grade", sport, "skipped", f"no picks published for {yesterday.isoformat()}")
            continue
        run_stage("grade", sport,
                  ["pipeline.grading", "grade", "--sport", sport, "--date", yesterday.isoformat()])

    # 3. PREDICT today's slate (features computed point-in-time inside).
    slate_size: dict[str, int | None] = {s: todays_game_count(s, on) for s in sports}
    for sport in sports:
        if slate_size[sport] is None:
            record("predict", sport, "skipped", "no games CSV — did ingest fail?")
        elif slate_size[sport] == 0:
            record("predict", sport, "skipped", f"no games on {on.isoformat()} (off-season?)")
        else:
            run_stage("predict", sport,
                      ["pipeline.models", "predict", "--sport", sport, "--date", on.isoformat()])

    # 4. ODDS fetch + event matching, then the model-vs-market edge report.
    for sport in sports:
        if skip_odds:
            record("odds", sport, "skipped", "--skip-odds")
            continue
        if not os.environ.get("ODDS_API_KEY"):
            record("odds", sport, "skipped", "ODDS_API_KEY not set")
            continue
        if sport not in ODDS_SPORT_KEYS:
            record("odds", sport, "skipped", "sport not covered by the odds adapter")
            continue
        if not slate_size[sport]:
            record("odds", sport, "skipped", "no slate today")
            continue
        if not run_stage("odds", sport, ["pipeline.odds", "fetch", "--sport", sport]):
            continue
        if predictions_path(sport, on).exists():
            run_stage("edge", sport,
                      ["pipeline.odds", "edge", "--sport", sport, "--date", on.isoformat()])
        else:
            record("edge", sport, "skipped", "no predictions for today")

    return results


def refresh_mirror(sports: list[str], runner=run_cli) -> StageResult:
    """Reload the CSVs into the SQLite/Postgres mirror the dashboard reads.

    Runs after the report so ``run_daily.py`` leaves the dashboard showing the
    day's fresh predictions / picks / grades without a manual
    ``python3 -m pipeline.db load``. Best-effort and non-fatal: the flat CSVs
    stay the source of truth, so a mirror hiccup (e.g. DATABASE_URL set but no
    psycopg driver) is reported but never reds the run."""
    code, out = runner(["pipeline.db", "load", "--sports", ",".join(sports)])
    status = "ok" if code == 0 else "failed"
    if code == 0:
        detail = out.splitlines()[-1] if out else "mirror refreshed"
    else:
        detail = f"exit {code} — mirror not refreshed (CSVs unaffected)"
    print(f"[{STATUS_MARK[status]}] {'db load':<8} {detail}")
    return StageResult("db-load", "all", status, detail, out)


def write_report(on: date, results: list[StageResult]) -> Path:
    """Render the daily report (PLAN.md §7 wish list) as markdown."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"daily_{on.isoformat()}.md"

    lines = [
        f"# Daily run — {on.isoformat()}",
        "",
        f"Generated {datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M %Z')} by the orchestrator "
        f"(`python3 run_daily.py --date {on.isoformat()}`).",
        "",
        "## Stage log",
        "",
        "| stage | sport | status | detail |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r.stage} | {r.sport} | {r.status} | {r.detail} |")

    sections = [
        ("Yesterday's grade card", "grade"),
        ("Today's pick sheet", "predict"),
        ("Model vs market (edge)", "edge"),
    ]
    for title, stage in sections:
        lines += ["", f"## {title}", ""]
        shown = [r for r in results if r.stage == stage and r.status == "ok" and r.output]
        if not shown:
            only = {r.detail for r in results if r.stage == stage}
            lines.append(f"_nothing today ({'; '.join(sorted(only)) or 'stage did not run'})._")
        for r in shown:
            lines += [f"### {r.sport}", "", "```", r.output, "```", ""]

    failures = [r for r in results if r.status == "failed"]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            lines += [f"### {r.stage} {r.sport}", "", "```", r.output or r.detail, "```", ""]

    path.write_text("\n".join(lines) + "\n")
    return path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="python3 run_daily.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--date", help="run for this date (default: today, US/Eastern)")
    p.add_argument("--sports", default=",".join(DEFAULT_SPORTS),
                   help=f"comma-separated subset (default: {','.join(DEFAULT_SPORTS)})")
    p.add_argument("--skip-odds", action="store_true",
                   help="skip odds fetch + edge even if ODDS_API_KEY is set")
    p.add_argument("--offline", action="store_true",
                   help="ingest from cached raw feeds instead of hitting the APIs")
    p.add_argument("--skip-db-load", action="store_true",
                   help="don't refresh the DB mirror after the run. The dashboard "
                        "reads predictions/results from the mirror; the flat CSVs "
                        "stay the source of truth either way. CI sets this because "
                        "it mirrors to Postgres in its own dedicated step")
    p.add_argument("--allow-partial", action="store_true",
                   help="don't fail the run when only SOME sports' ingest fails "
                        "(an off-season or unreachable feed) — the failure is still "
                        "reported. Still fails if every sport's ingest fails, or if a "
                        "later stage (grade/predict/odds/edge) fails on a fetched sport")
    args = p.parse_args(argv)

    on = date.fromisoformat(args.date) if args.date else datetime.now(EASTERN).date()
    sports = [s.strip().upper() for s in args.sports.split(",") if s.strip()]
    unknown = sorted(set(sports) - set(ADAPTERS))
    if unknown:
        sys.exit(f"unknown sport(s) {', '.join(unknown)} — choose from {', '.join(DEFAULT_SPORTS)}")

    results = daily(on, sports, skip_odds=args.skip_odds, offline=args.offline)
    report = write_report(on, results)
    print(f"\nreport -> {report}")

    # Refresh the dashboard's DB mirror (non-fatal — see refresh_mirror). Runs
    # even when a stage failed so the mirror reflects whatever CSVs exist.
    if not args.skip_db_load:
        refresh_mirror(sports)

    failures = [r for r in results if r.status == "failed"]
    if not failures:
        return

    if args.allow_partial:
        # An ingest failure is a dead/off-season/unreachable feed for one sport —
        # tolerated so the sports that DID fetch still publish and commit. But a
        # total feed outage (every sport's ingest failed) still deserves an alert,
        # as does any later-stage failure, which signals a real code/data bug on a
        # sport that fetched fine rather than a missing feed.
        ingest_fails = [r for r in failures if r.stage == "ingest"]
        all_ingest_failed = len(ingest_fails) == len(sports)
        tolerated = [] if all_ingest_failed else ingest_fails
        fatal = [r for r in failures if r not in tolerated]
        if tolerated:
            note = ", ".join(f"{r.sport}" for r in tolerated)
            print(f"tolerated {len(tolerated)} ingest failure(s) (--allow-partial): {note}")
        if not fatal:
            return
        failures = fatal

    stages = ", ".join(f"{r.stage} {r.sport}" for r in failures)
    sys.exit(f"{len(failures)} stage(s) failed: {stages}")


if __name__ == "__main__":
    main()
