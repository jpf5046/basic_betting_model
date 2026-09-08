#!/usr/bin/env python3
"""Daily orchestrator — PLAN.md §7, the centerpiece.

One run chains the whole morning loop as an ordered DAG of idempotent
stages, per sport, each safe to re-run for any date:

    1. INGEST    fetch the schedule feed (yesterday's finals + today's slate)
    2. FRESH     check the recent slate actually came back final — a stale
                 games CSV keeps every other stage green (see freshness)
    3. GRADE     grade the picks published yesterday (skipped if none were)
    4. REGRADE   re-grade earlier dates still stuck on PENDING, so a feed
                 that comes back also settles what it stranded
    5. PREDICT   run the model over today's slate — feature computation
                 happens inside via the point-in-time FeatureContext
    6. ODDS      fetch today's odds + event matching, then the edge report
                 (skipped when ODDS_API_KEY is not set or --skip-odds)
    7. REPORT    write data/reports/daily_<date>.md — grade card, pick
                 sheet, edge report, and the stage log — and print a summary
    8. MIRROR    reload the CSVs into the DB mirror the dashboard reads from
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
  * Green stages are not the same as good data. Every stage downstream of
    ingest reads the games CSV, which SURVIVES a failed fetch, so a broken
    feed shows up as a full slate of picks graded PENDING rather than as a
    red run — that is how MLB went 28 days without a final. The `fresh`
    stage exists to make that visible, and --allow-partial no longer
    swallows an ingest failure on a sport that has games on the board.
  * Re-running for a missed day is `python3 run_daily.py --date 2026-07-03`.
  * Still open from §7: standings snapshots (blocked on §2's standings
    ingestion) and failure notifications (start with the Actions cron in
    .github/workflows/daily.yml once it's enabled).
"""

from __future__ import annotations

import argparse
import csv
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

# A game the feed has not resolved yet. Postponed/suspended/cancelled are
# terminal non-results, so they are NOT evidence of a stale feed.
UNFINISHED_STATUSES = ("scheduled", "live")

# The `fresh` stage looks at the games the schedule says were played over
# this trailing window, ending yesterday (today's are still in progress at
# the 6:30am ET cron). More than STALE_TOLERANCE of them still unresolved
# means the games CSV stopped being updated — a couple is a postponement
# the feed spells oddly, a whole slate is a broken ingest.
FRESHNESS_LOOKBACK_DAYS = 3
STALE_TOLERANCE = 2

# How far back the catch-up regrade reaches, and how many dates it will
# re-run in one go (each is a subprocess; a healthy pipeline has none).
REGRADE_LOOKBACK_DAYS = 45
MAX_REGRADES_PER_RUN = 30


@dataclass
class StageResult:
    stage: str   # ingest | fresh | grade | regrade | predict | odds | edge
    sport: str
    status: str  # ok | skipped | failed
    detail: str = ""   # one-liner for the stage log
    output: str = ""   # full captured stdout+stderr (report body material)
    tolerable: bool = False  # ingest only: the sport looks off-season, so
                             # --allow-partial may swallow this failure


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


def load_schedule(sport: str, on: date) -> list[core.Game] | None:
    """The sport's games CSV, or None when it doesn't exist yet.

    This is whatever the last SUCCESSFUL ingest left behind, which is the
    point: the schedule survives a broken fetch, so it can still answer
    "was this sport playing?" when today's fetch is the thing that failed.
    """
    path = ADAPTERS[sport].games_csv_path(default_season(sport, on))
    if not path.exists():
        return None
    return core.read_games_csv(
        path, f"python3 -m pipeline.ingest.{sport.lower()} fetch"
    )


def todays_game_count(sport: str, on: date) -> int | None:
    """Games on the slate for `on`, or None when the games CSV is missing."""
    games = load_schedule(sport, on)
    return None if games is None else len(core.todays_games(games, on))


def recent_slate(games: list[core.Game], on: date,
                 lookback: int = FRESHNESS_LOOKBACK_DAYS) -> list[core.Game]:
    """The games the schedule puts in the `lookback` days ending yesterday."""
    window = {(on - timedelta(days=d)).isoformat() for d in range(1, lookback + 1)}
    return [g for g in games if g.date in window]


def freshness(sport: str, on: date) -> tuple[int, int] | None:
    """(games recently scheduled, of those still unresolved), or None with
    no games CSV.

    The MLB ingest broke on 2026-08-11 and nobody noticed for 28 days,
    because a stale games CSV still holds a full forward schedule: predict
    kept publishing picks off it and grade kept writing all-PENDING grade
    cards, so every stage stayed green while the leaderboard quietly
    stopped moving. Comparing the schedule against its own finals is what
    catches that — a slate the feed never resolved is the symptom no
    individual stage's exit code can see.
    """
    games = load_schedule(sport, on)
    if games is None:
        return None
    played = recent_slate(games, on)
    return len(played), sum(1 for g in played if g.status in UNFINISHED_STATUSES)


def pending_grade_dates(sport: str, on: date,
                        lookback: int = REGRADE_LOOKBACK_DAYS) -> list[date]:
    """Earlier dates whose grade card still carries PENDING rows, oldest first.

    The daily loop only ever grades yesterday, so a date graded while the
    finals were missing keeps its PENDING rows forever — the 28 days of MLB
    picks stranded by the broken ingest would never reach the leaderboard
    even once the feed came back. Grading is idempotent, so re-running them
    costs nothing and settles whatever the fresh finals now cover.

    Yesterday is excluded (the grade stage just did it), as are dates whose
    picks file is gone — those cannot be re-graded, only reported.
    """
    out: list[date] = []
    oldest = on - timedelta(days=lookback)
    for path in sorted((PREDICTIONS_DIR / sport.lower()).glob("grades_*.csv")):
        try:
            day = date.fromisoformat(path.stem.removeprefix("grades_"))
        except ValueError:
            continue
        if not oldest <= day <= on - timedelta(days=2):
            continue
        if not picks_path(sport, day).exists():
            continue
        with open(path, newline="") as f:
            if any(r.get("result") == "PENDING" for r in csv.DictReader(f)):
                out.append(day)
    return out


def daily(on: date, sports: list[str], skip_odds: bool = False,
          offline: bool = False, runner=run_cli) -> list[StageResult]:
    """Run the DAG for one date and return every stage's result."""
    yesterday = on - timedelta(days=1)
    results: list[StageResult] = []

    def record(stage: str, sport: str, status: str, detail: str = "",
               output: str = "", tolerable: bool = False) -> None:
        results.append(StageResult(stage, sport, status, detail, output, tolerable))
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
        code, out = runner(argv)
        if code == 0:
            record("ingest", sport, "ok", out.splitlines()[-1] if out else "", out)
            continue
        # Whether --allow-partial may swallow this is decided here, while we
        # can still see the schedule: a sport with games on the board is
        # playing, so its feed failing is a real outage, not July's NBA.
        played = recent_slate(load_schedule(sport, on) or [], on)
        record("ingest", sport, "failed",
               f"exit {code} — see report for output" if played else
               f"exit {code} — see report for output (no games scheduled in the "
               f"last {FRESHNESS_LOOKBACK_DAYS} days, so plausibly off-season)",
               out, tolerable=not played)

    # 2. FRESH — does the schedule's recent slate actually have its finals?
    #    The stage that would have caught the 2026-08-11 MLB outage on day
    #    one: every other stage reads green off a stale CSV (see freshness).
    for sport in sports:
        counts = freshness(sport, on)
        if counts is None:
            record("fresh", sport, "skipped", "no games CSV — did ingest fail?")
            continue
        played, unfinished = counts
        window = f"the last {FRESHNESS_LOOKBACK_DAYS} days"
        if not played:
            record("fresh", sport, "skipped", f"no games scheduled in {window} (off-season?)")
        elif unfinished > STALE_TOLERANCE:
            record("fresh", sport, "failed",
                   f"{unfinished} of {played} games in {window} never went final — "
                   f"the games CSV is stale, so picks are being published and "
                   f"graded against a schedule the feed stopped updating")
        else:
            record("fresh", sport, "ok", f"{played} games in {window}, {unfinished} unresolved")

    # 3. GRADE yesterday's published picks against the freshly fetched finals.
    for sport in sports:
        if not picks_path(sport, yesterday).exists():
            record("grade", sport, "skipped", f"no picks published for {yesterday.isoformat()}")
            continue
        run_stage("grade", sport,
                  ["pipeline.grading", "grade", "--sport", sport, "--date", yesterday.isoformat()])

    # 4. REGRADE earlier dates still stuck on PENDING — the catch-up that
    #    lets the leaderboard heal itself once a broken feed comes back.
    for sport in sports:
        stuck = pending_grade_dates(sport, on)
        if not stuck:
            record("regrade", sport, "skipped", "no earlier dates left pending")
            continue
        batch = stuck[:MAX_REGRADES_PER_RUN]
        outputs, failed = [], []
        for day in batch:
            code, out = runner(["pipeline.grading", "grade",
                                "--sport", sport, "--date", day.isoformat()])
            outputs.append(f"$ grade --date {day.isoformat()}\n{out}")
            if code != 0:
                failed.append(day.isoformat())
        left = len(pending_grade_dates(sport, on))
        detail = (f"re-graded {len(batch)} of {len(stuck)} pending date(s); "
                  f"{left} still unresolved")
        if failed:
            record("regrade", sport, "failed",
                   f"{detail} — grading failed for {', '.join(failed)}",
                   "\n\n".join(outputs))
        else:
            record("regrade", sport, "ok", detail, "\n\n".join(outputs))

    # 5. PREDICT today's slate (features computed point-in-time inside).
    slate_size: dict[str, int | None] = {s: todays_game_count(s, on) for s in sports}
    for sport in sports:
        if slate_size[sport] is None:
            record("predict", sport, "skipped", "no games CSV — did ingest fail?")
        elif slate_size[sport] == 0:
            record("predict", sport, "skipped", f"no games on {on.isoformat()} (off-season?)")
        else:
            run_stage("predict", sport,
                      ["pipeline.models", "predict", "--sport", sport, "--date", on.isoformat()])

    # 6. ODDS fetch + event matching, then the model-vs-market edge report.
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
                   help="don't fail the run when an OFF-SEASON sport's ingest fails "
                        "(a dead or unreachable out-of-season feed) — the failure is "
                        "still reported. A sport with games on the schedule still "
                        "fails the run, as does every sport's ingest failing at once, "
                        "or a later stage (fresh/grade/regrade/predict/odds/edge) "
                        "failing on a fetched sport")
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
        # An ingest failure on an OFF-SEASON sport is a dead feed nobody is
        # waiting on — tolerated so the sports that DID fetch still publish and
        # commit. A sport with games on the board is a different story: that is
        # a live outage, and swallowing it is exactly how the MLB feed stayed
        # broken from 2026-08-11 to 2026-09-07 with every run showing green.
        # A total feed outage (every sport's ingest failed) still deserves an
        # alert, as does any later-stage failure, which signals a real code/data
        # bug on a sport that fetched fine rather than a missing feed.
        ingest_fails = [r for r in failures if r.stage == "ingest"]
        all_ingest_failed = len(ingest_fails) == len(sports)
        tolerated = [] if all_ingest_failed else [r for r in ingest_fails if r.tolerable]
        fatal = [r for r in failures if r not in tolerated]
        if tolerated:
            note = ", ".join(f"{r.sport}" for r in tolerated)
            print(f"tolerated {len(tolerated)} off-season ingest failure(s) "
                  f"(--allow-partial): {note}")
        if not fatal:
            return
        failures = fatal

    stages = ", ".join(f"{r.stage} {r.sport}" for r in failures)
    sys.exit(f"{len(failures)} stage(s) failed: {stages}")


if __name__ == "__main__":
    main()
