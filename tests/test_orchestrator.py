"""Offline tests for the daily orchestrator (pipeline/orchestrator.py).

No network, no subprocesses: a fake runner records the CLI calls the DAG
would make and returns canned results, and the picks/predictions/report
paths are redirected to a temp dir. From the repo root:

    python3 -m unittest tests.test_orchestrator -v
"""
import contextlib
import io
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from pipeline import orchestrator
from pipeline.ingest import core
from pipeline.orchestrator import StageResult, daily, write_report

ON = date(2026, 7, 7)
YESTERDAY = "2026-07-06"


class FakeRunner:
    """Records every CLI invocation; returns per-prefix canned results."""

    def __init__(self, fail_prefixes: tuple = ()):
        self.calls: list[list[str]] = []
        self.fail_prefixes = fail_prefixes

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        for prefix in self.fail_prefixes:
            if joined.startswith(prefix):
                return 1, f"boom: {joined}"
        return 0, f"ran: {joined}"

    def stages_called(self) -> list[str]:
        return [argv[0].split(".")[1] for argv in self.calls]


class OrchestratorCase(unittest.TestCase):
    """Shared temp-dir plumbing: predictions/reports live under a tmp dir,
    slate sizes are stubbed, and stdout is swallowed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for attr in ("PREDICTIONS_DIR", "REPORTS_DIR"):
            patcher = mock.patch.object(orchestrator, attr, self.tmp / attr.lower())
            patcher.start()
            self.addCleanup(patcher.stop)
        # Default: 2 games today for every sport; tests override as needed.
        self.slates = {"WNBA": 2, "MLB": 5}
        patcher = mock.patch.object(
            orchestrator, "todays_game_count",
            side_effect=lambda sport, on: self.slates.get(sport),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # Default: the recent slate came back fully settled, and no earlier
        # date is stuck pending. Both read the real data/ tree otherwise.
        self.fresh = {"WNBA": (6, 0), "MLB": (15, 0)}
        patcher = mock.patch.object(
            orchestrator, "freshness", side_effect=lambda sport, on: self.fresh.get(sport),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pending = {}
        patcher = mock.patch.object(
            orchestrator, "pending_grade_dates",
            side_effect=lambda sport, on: self.pending.get(sport, []),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            orchestrator, "recent_slate", side_effect=lambda games, on: [],
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict("os.environ", {"ODDS_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_picks(self, sport: str, day: str) -> None:
        p = orchestrator.PREDICTIONS_DIR / sport.lower() / f"picks_{day}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("pick_id\n")

    def write_predictions(self, sport: str, day: str) -> None:
        p = orchestrator.PREDICTIONS_DIR / sport.lower() / f"predictions_{day}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("status\n")

    def run_daily(self, sports=("WNBA",), **kw) -> tuple[list, FakeRunner]:
        runner = kw.pop("runner", FakeRunner())
        with contextlib.redirect_stdout(io.StringIO()):
            results = daily(ON, list(sports), runner=runner, **kw)
        return results, runner

    def by_stage(self, results, stage, sport):
        return next(r for r in results if r.stage == stage and r.sport == sport)


class TestHappyPath(OrchestratorCase):
    def test_all_stages_run_in_order(self):
        self.write_picks("WNBA", YESTERDAY)
        self.write_predictions("WNBA", ON.isoformat())
        results, runner = self.run_daily()

        self.assertEqual(
            runner.calls,
            [
                ["pipeline.ingest.wnba", "fetch"],
                ["pipeline.grading", "grade", "--sport", "WNBA", "--date", YESTERDAY],
                ["pipeline.models", "predict", "--sport", "WNBA", "--date", ON.isoformat()],
                ["pipeline.odds", "fetch", "--sport", "WNBA"],
                ["pipeline.odds", "edge", "--sport", "WNBA", "--date", ON.isoformat()],
            ],
        )
        self.assertEqual(
            [(r.stage, r.status) for r in results],
            [("ingest", "ok"), ("fresh", "ok"), ("grade", "ok"),
             ("regrade", "skipped"), ("predict", "ok"), ("odds", "ok"), ("edge", "ok")],
        )

    def test_offline_flag_reaches_ingest(self):
        _, runner = self.run_daily(offline=True)
        self.assertEqual(runner.calls[0], ["pipeline.ingest.wnba", "fetch", "--offline"])


class TestSkips(OrchestratorCase):
    def test_grade_skipped_without_yesterdays_picks(self):
        results, runner = self.run_daily()
        grade = self.by_stage(results, "grade", "WNBA")
        self.assertEqual(grade.status, "skipped")
        self.assertIn(YESTERDAY, grade.detail)
        self.assertNotIn("grading", runner.stages_called())

    def test_predict_and_odds_skipped_offseason(self):
        self.slates["WNBA"] = 0
        results, runner = self.run_daily()
        self.assertEqual(self.by_stage(results, "predict", "WNBA").status, "skipped")
        self.assertIn("off-season", self.by_stage(results, "predict", "WNBA").detail)
        self.assertEqual(self.by_stage(results, "odds", "WNBA").status, "skipped")
        self.assertNotIn("models", runner.stages_called())
        self.assertNotIn("odds", runner.stages_called())

    def test_predict_skipped_when_games_csv_missing(self):
        self.slates["WNBA"] = None
        results, _ = self.run_daily()
        predict = self.by_stage(results, "predict", "WNBA")
        self.assertEqual(predict.status, "skipped")
        self.assertIn("ingest fail", predict.detail)

    def test_odds_skipped_without_api_key(self):
        with mock.patch.dict("os.environ", {"ODDS_API_KEY": ""}):
            results, runner = self.run_daily()
        self.assertEqual(self.by_stage(results, "odds", "WNBA").status, "skipped")
        self.assertNotIn("odds", runner.stages_called())

    def test_skip_odds_flag(self):
        results, runner = self.run_daily(skip_odds=True)
        self.assertEqual(self.by_stage(results, "odds", "WNBA").detail, "--skip-odds")
        self.assertNotIn("odds", runner.stages_called())

    def test_edge_skipped_without_todays_predictions(self):
        # Odds fetch succeeds, but predict wrote nothing (e.g. it failed).
        results, runner = self.run_daily()
        self.assertEqual(self.by_stage(results, "edge", "WNBA").status, "skipped")
        self.assertNotIn(["pipeline.odds", "edge", "--sport", "WNBA",
                          "--date", ON.isoformat()], runner.calls)


class TestFailureIsolation(OrchestratorCase):
    def test_one_sports_failure_does_not_stop_the_other(self):
        self.write_picks("MLB", YESTERDAY)
        runner = FakeRunner(fail_prefixes=("pipeline.ingest.wnba",))
        results, runner = self.run_daily(sports=("MLB", "WNBA"), runner=runner)

        self.assertEqual(self.by_stage(results, "ingest", "WNBA").status, "failed")
        self.assertEqual(self.by_stage(results, "ingest", "MLB").status, "ok")
        self.assertEqual(self.by_stage(results, "grade", "MLB").status, "ok")
        self.assertEqual(self.by_stage(results, "predict", "MLB").status, "ok")

    def test_failed_odds_fetch_skips_edge_call(self):
        self.write_predictions("WNBA", ON.isoformat())
        runner = FakeRunner(fail_prefixes=("pipeline.odds fetch",))
        results, runner = self.run_daily(runner=runner)
        self.assertEqual(self.by_stage(results, "odds", "WNBA").status, "failed")
        self.assertFalse([c for c in runner.calls if c[:2] == ["pipeline.odds", "edge"]])


class TestReport(OrchestratorCase):
    def test_report_contains_stage_log_and_outputs(self):
        results = [
            StageResult("ingest", "WNBA", "ok", "wrote 44 games"),
            StageResult("grade", "WNBA", "ok", "2 picks graded", "GRADE CARD BODY"),
            StageResult("predict", "WNBA", "ok", "2 picks", "PICK SHEET BODY"),
            StageResult("odds", "WNBA", "failed", "exit 1", "quota exceeded"),
        ]
        path = write_report(ON, results)
        text = path.read_text()
        self.assertEqual(path.name, f"daily_{ON.isoformat()}.md")
        self.assertIn("| ingest | WNBA | ok | wrote 44 games |", text)
        self.assertIn("GRADE CARD BODY", text)
        self.assertIn("PICK SHEET BODY", text)
        self.assertIn("## Failures", text)
        self.assertIn("quota exceeded", text)

    def test_report_notes_empty_sections(self):
        path = write_report(ON, [StageResult("predict", "WNBA", "skipped", "no games")])
        self.assertIn("_nothing today", path.read_text())


class TestExitPolicy(unittest.TestCase):
    """How main() turns stage failures into an exit code, strict vs
    --allow-partial (the mode the daily cron runs in)."""

    @staticmethod
    def run_main(results, argv):
        with mock.patch.object(orchestrator, "daily", return_value=results), \
             mock.patch.object(orchestrator, "write_report", return_value=Path("x.md")), \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                orchestrator.main(argv)
            except SystemExit as e:
                return str(e.code)
        return None

    def test_strict_fails_on_any_failure(self):
        results = [StageResult("ingest", "MLB", "ok"),
                   StageResult("ingest", "NBA", "failed")]
        msg = self.run_main(results, ["--sports", "MLB,NBA"])
        self.assertIn("ingest NBA", msg)

    def test_partial_tolerates_an_offseason_ingest_failure(self):
        results = [StageResult("ingest", "MLB", "ok"),
                   StageResult("ingest", "NBA", "failed", tolerable=True)]
        # MLB fetched and NBA has no games on the board — July's dead NBA feed
        # is not worth reddening the run over.
        self.assertIsNone(self.run_main(results, ["--sports", "MLB,NBA", "--allow-partial"]))

    def test_partial_does_not_tolerate_an_in_season_ingest_failure(self):
        """The 28-day MLB outage: --allow-partial swallowed a live feed
        failure because it could not tell off-season from broken."""
        results = [StageResult("ingest", "NHL", "ok"),
                   StageResult("ingest", "MLB", "failed", tolerable=False)]
        msg = self.run_main(results, ["--sports", "NHL,MLB", "--allow-partial"])
        self.assertIn("ingest MLB", msg)

    def test_partial_does_not_tolerate_a_stale_games_csv(self):
        results = [StageResult("ingest", "MLB", "ok"),
                   StageResult("fresh", "MLB", "failed")]
        msg = self.run_main(results, ["--sports", "MLB", "--allow-partial"])
        self.assertIn("fresh MLB", msg)

    def test_partial_still_fails_when_every_ingest_fails(self):
        results = [StageResult("ingest", "MLB", "failed"),
                   StageResult("ingest", "NBA", "failed")]
        msg = self.run_main(results, ["--sports", "MLB,NBA", "--allow-partial"])
        self.assertIsNotNone(msg)  # a total feed outage still alerts

    def test_partial_still_fails_on_a_later_stage(self):
        results = [StageResult("ingest", "MLB", "ok"),
                   StageResult("ingest", "NBA", "failed", tolerable=True),
                   StageResult("predict", "MLB", "failed")]
        msg = self.run_main(results, ["--sports", "MLB,NBA", "--allow-partial"])
        self.assertIn("predict MLB", msg)      # a real bug on a fetched sport
        self.assertNotIn("ingest NBA", msg)    # the tolerated feed isn't in the exit


class TestFreshness(OrchestratorCase):
    """The stage that would have caught the 2026-08-11 MLB outage on day one."""

    def test_stale_games_csv_fails_the_stage(self):
        self.fresh = {"WNBA": (18, 18)}
        results, _ = self.run_daily()
        stage = self.by_stage(results, "fresh", "WNBA")
        self.assertEqual(stage.status, "failed")
        self.assertIn("18 of 18", stage.detail)

    def test_a_couple_of_unresolved_games_is_not_stale(self):
        """A postponement the feed spells oddly must not cry wolf daily."""
        self.fresh = {"WNBA": (18, 2)}
        self.assertEqual(self.by_stage(self.run_daily()[0], "fresh", "WNBA").status, "ok")

    def test_no_recent_games_is_offseason_not_stale(self):
        self.fresh = {"WNBA": (0, 0)}
        stage = self.by_stage(self.run_daily()[0], "fresh", "WNBA")
        self.assertEqual(stage.status, "skipped")
        self.assertIn("off-season", stage.detail)

    def test_missing_games_csv_is_skipped(self):
        self.fresh = {}
        self.assertEqual(self.by_stage(self.run_daily()[0], "fresh", "WNBA").status, "skipped")

    def test_in_season_ingest_failure_is_not_tolerable(self):
        with mock.patch.object(orchestrator, "recent_slate", side_effect=lambda games, on: ["a game"]):
            results, _ = self.run_daily(runner=FakeRunner(fail_prefixes=("pipeline.ingest",)))
        self.assertFalse(self.by_stage(results, "ingest", "WNBA").tolerable)

    def test_offseason_ingest_failure_is_tolerable(self):
        results, _ = self.run_daily(runner=FakeRunner(fail_prefixes=("pipeline.ingest",)))
        stage = self.by_stage(results, "ingest", "WNBA")
        self.assertTrue(stage.tolerable)
        self.assertIn("plausibly off-season", stage.detail)


class TestRegrade(OrchestratorCase):
    """Catch-up grading — how the leaderboard heals once a feed comes back."""

    def test_pending_dates_are_regraded(self):
        self.pending = {"WNBA": [date(2026, 6, 30), date(2026, 7, 1)]}
        _, runner = self.run_daily()
        self.assertIn(["pipeline.grading", "grade", "--sport", "WNBA", "--date", "2026-06-30"],
                      runner.calls)
        self.assertIn(["pipeline.grading", "grade", "--sport", "WNBA", "--date", "2026-07-01"],
                      runner.calls)

    def test_regrade_is_capped_per_run(self):
        self.pending = {"WNBA": [date(2026, 6, 1) + timedelta(days=i) for i in range(40)]}
        with mock.patch.object(orchestrator, "MAX_REGRADES_PER_RUN", 3):
            results, runner = self.run_daily()
        regrades = [c for c in runner.calls if c[:2] == ["pipeline.grading", "grade"]]
        self.assertEqual(len(regrades), 3)  # the cap (yesterday published no picks)
        self.assertIn("re-graded 3 of 40", self.by_stage(results, "regrade", "WNBA").detail)

    def test_failed_regrade_fails_the_stage(self):
        self.pending = {"WNBA": [date(2026, 6, 30)]}
        runner = FakeRunner(fail_prefixes=("pipeline.grading grade --sport WNBA --date 2026-06-30",))
        results, _ = self.run_daily(runner=runner)
        stage = self.by_stage(results, "regrade", "WNBA")
        self.assertEqual(stage.status, "failed")
        self.assertIn("2026-06-30", stage.detail)


class TestScheduleHelpers(unittest.TestCase):
    """The real helpers (unstubbed) against a temp predictions/games tree."""

    GRADE_HEADER = "game_id,sport,date,bet_type,selection,result,pnl\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(orchestrator, "PREDICTIONS_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_graded(self, day: str, result: str, with_picks: bool = True) -> None:
        d = orchestrator.PREDICTIONS_DIR / "mlb"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"grades_{day}.csv").write_text(
            self.GRADE_HEADER + f"1,MLB,{day},ML,mlb-nyy,{result},0\n")
        if with_picks:
            (d / f"picks_{day}.csv").write_text("game_id\n1\n")

    def test_pending_dates_are_found_oldest_first(self):
        self.write_graded("2026-08-10", "PENDING")
        self.write_graded("2026-08-11", "PENDING")
        self.write_graded("2026-08-12", "WIN")
        self.assertEqual(
            orchestrator.pending_grade_dates("MLB", date(2026, 9, 8)),
            [date(2026, 8, 10), date(2026, 8, 11)],
        )

    def test_yesterday_is_left_to_the_grade_stage(self):
        self.write_graded("2026-09-07", "PENDING")
        self.assertEqual(orchestrator.pending_grade_dates("MLB", date(2026, 9, 8)), [])

    def test_dates_beyond_the_lookback_are_dropped(self):
        self.write_graded("2026-06-01", "PENDING")
        self.assertEqual(orchestrator.pending_grade_dates("MLB", date(2026, 9, 8)), [])

    def test_dates_without_picks_cannot_be_regraded(self):
        self.write_graded("2026-08-10", "PENDING", with_picks=False)
        self.assertEqual(orchestrator.pending_grade_dates("MLB", date(2026, 9, 8)), [])

    def test_recent_slate_is_the_window_ending_yesterday(self):
        games = [
            _game("2026-09-08", "scheduled"),   # today — still in progress
            _game("2026-09-07", "final"),
            _game("2026-09-06", "final"),
            _game("2026-09-04", "final"),       # outside a 3-day window
        ]
        picked = orchestrator.recent_slate(games, date(2026, 9, 8), lookback=3)
        self.assertEqual([g.date for g in picked], ["2026-09-07", "2026-09-06"])

    def test_postponed_games_do_not_read_as_stale(self):
        games = [_game("2026-09-07", "postponed"), _game("2026-09-07", "final")]
        played = orchestrator.recent_slate(games, date(2026, 9, 8))
        unfinished = [g for g in played if g.status in orchestrator.UNFINISHED_STATUSES]
        self.assertEqual(unfinished, [])


def _game(day: str, status: str) -> core.Game:
    return core.Game(
        game_id=day + status, season="2026", season_type="regular", date=day,
        start_time_utc=f"{day}T18:00:00Z", status=status,
        away_team_id="mlb-nyy", home_team_id="mlb-bos",
        away_abbrev="NYY", home_abbrev="BOS",
        away_score="", home_score="", venue="Fenway Park",
    )


class TestMirrorRefresh(unittest.TestCase):
    """run_daily.py refreshes the DB mirror the dashboard reads from, as a
    best-effort post-run step that never affects the exit code."""

    def test_refresh_mirror_runs_the_load_cli(self):
        runner = FakeRunner()
        with contextlib.redirect_stdout(io.StringIO()):
            res = orchestrator.refresh_mirror(["MLB", "WNBA"], runner=runner)
        self.assertEqual(runner.calls,
                         [["pipeline.db", "load", "--sports", "MLB,WNBA"]])
        self.assertEqual(res.status, "ok")

    def test_refresh_mirror_failure_is_nonfatal(self):
        runner = FakeRunner(fail_prefixes=("pipeline.db",))
        with contextlib.redirect_stdout(io.StringIO()):
            res = orchestrator.refresh_mirror(["MLB"], runner=runner)
        self.assertEqual(res.status, "failed")
        self.assertIn("CSVs unaffected", res.detail)

    def _run_main(self, argv):
        results = [StageResult("ingest", "MLB", "ok")]
        with mock.patch.object(orchestrator, "daily", return_value=results), \
             mock.patch.object(orchestrator, "write_report", return_value=Path("x.md")), \
             mock.patch.object(orchestrator, "refresh_mirror") as rm, \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                orchestrator.main(argv)
            except SystemExit:
                pass
        return rm

    def test_main_refreshes_mirror_by_default(self):
        rm = self._run_main(["--sports", "MLB"])
        rm.assert_called_once_with(["MLB"])

    def test_skip_db_load_suppresses_refresh(self):
        rm = self._run_main(["--sports", "MLB", "--skip-db-load"])
        rm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
