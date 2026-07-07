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
from datetime import date
from pathlib import Path
from unittest import mock

from pipeline import orchestrator
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
        self.assertEqual({r.status for r in results}, {"ok"})

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


if __name__ == "__main__":
    unittest.main()
