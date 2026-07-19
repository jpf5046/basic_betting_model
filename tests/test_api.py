"""Offline tests for the console API's file-parsing / serialization layer
(pipeline/api/). No network, no FastAPI — the pure parsing + store + reasoning
modules are exercised directly against a temp data tree built in-test.

From the repo root:  python3 -m unittest tests.test_api -v
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.api import parsers, reasoning, service
from pipeline.api.paths import safe_name, valid_date, valid_sport
from pipeline.api.store import ConfigStore
from pipeline.api.teams import TeamRegistry
from pipeline.models.config import ConfigError, FACTORY_DEFAULTS

# A trimmed but structurally real daily report (matches orchestrator output).
SAMPLE_REPORT = """# Daily run — 2026-07-18

Generated 2026-07-18 10:53 EDT by the orchestrator (`python3 run_daily.py --date 2026-07-18`).

## Stage log

| stage | sport | status | detail |
|---|---|---|---|
| ingest | MLB | ok | wrote 2453 games -> /x/data/mlb/games_2026.csv (1460 final, 17 today) |
| ingest | NBA | failed | exit 1 — see report for output |
| grade | MLB | skipped | no picks published for 2026-07-17 |
| predict | MLB | ok | 10 published picks -> /x/picks_2026-07-18.csv |
| predict | NBA | skipped | no games CSV — did ingest fail? |
| odds | MLB | skipped | ODDS_API_KEY not set |

## Yesterday's grade card

_nothing today (no picks published for 2026-07-17)._

## Today's pick sheet

### MLB

```
2026-07-18   PIT @ CLE  pred 5-4 (PIT p=0.53, HIGH) | ML: TOSS-UP | TOTAL: PASS

17 predictions -> /x/predictions_2026-07-18.csv
10 published picks -> /x/picks_2026-07-18.csv
```

## Model vs market (edge)

_nothing today (stage did not run)._

## Failures

### ingest NBA

```
HTTP Error 403: Forbidden
Traceback (most recent call last):
  boom
```
"""


class TestReportParser(unittest.TestCase):
    def setUp(self):
        self.parsed = parsers.parse_report(SAMPLE_REPORT, "2026-07-18")

    def test_header_and_stage_count(self):
        self.assertEqual(self.parsed["date"], "2026-07-18")
        self.assertIn("Generated 2026-07-18", self.parsed["generated"])
        self.assertEqual(len(self.parsed["stages"]), 6)

    def test_stage_grid_and_row_counts(self):
        mlb = self.parsed["grid"]["MLB"]
        self.assertEqual(mlb["ingest"]["status"], "ok")
        self.assertEqual(mlb["ingest"]["rows"], 2453)
        self.assertEqual(mlb["ingest"]["final"], 1460)
        self.assertEqual(mlb["ingest"]["today"], 17)
        self.assertEqual(mlb["predict"]["rows"], 10)  # "10 published picks"

    def test_failures_capture_traceback(self):
        self.assertEqual(len(self.parsed["failures"]), 1)
        fail = self.parsed["failures"][0]
        self.assertEqual((fail["stage"], fail["sport"]), ("ingest", "NBA"))
        self.assertIn("403: Forbidden", fail["output"])
        self.assertIn("Traceback", fail["output"])

    def test_predict_section_output(self):
        blocks = self.parsed["sections"]["predict"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["sport"], "MLB")
        self.assertIn("PIT @ CLE", blocks[0]["output"])

    def test_summary(self):
        s = parsers.summarize_report(self.parsed)
        self.assertEqual(s["tally"], {"ok": 2, "failed": 1, "skipped": 3})
        self.assertEqual(s["picks"], 10)
        self.assertEqual(s["predictions"], 17)
        self.assertEqual(s["failures"], 1)

    def test_empty_report_is_safe(self):
        empty = parsers.parse_report("", "2026-01-01")
        self.assertEqual(empty["stages"], [])
        self.assertEqual(empty["failures"], [])


class TestRowShaping(unittest.TestCase):
    def test_shape_prediction_types(self):
        row = {"status": "ok", "ml_lean": "LEAN", "total_lean": "OVER",
               "game_id": "g1", "sport": "MLB", "date": "2026-07-18",
               "away_team_id": "mlb-cws", "home_team_id": "mlb-tor",
               "pred_away": "4.71", "pred_home": "4.42", "disp_away": "5",
               "disp_home": "4", "pred_total": "9.17", "pred_spread": "0.33",
               "win_prob": "0.56", "winner_team_id": "mlb-cws",
               "pred_confidence": "HIGH", "method_details": ""}
        s = parsers.shape_prediction(row)
        self.assertEqual(s["pred_away"], 4.71)
        self.assertEqual(s["disp_away"], 5)
        self.assertEqual(s["win_prob"], 0.56)

    def test_shape_prediction_no_prediction_row(self):
        s = parsers.shape_prediction({"game_id": "g", "sport": "MLB",
                                      "status": "no_prediction"})
        self.assertEqual(s["status"], "no_prediction")
        self.assertIsNone(s["pred_away"])  # missing numerics -> None, no crash


class TestPerformance(unittest.TestCase):
    def _grade(self, **kw):
        base = {"game_id": "g", "sport": "MLB", "date": "2026-07-17",
                "bet_type": "ML", "selection": "mlb-nyy", "confidence": "STRONG",
                "line": "0.6", "game_status": "final", "actual_away": "5",
                "actual_home": "3", "result": "WIN", "pnl": 100.0,
                "price_american": ""}
        base.update(kw)
        return base

    def test_roi_and_curve(self):
        grades = [
            self._grade(result="WIN", pnl=100.0, date="2026-07-15"),
            self._grade(result="LOSS", pnl=-100.0, date="2026-07-16"),
            self._grade(result="WIN", pnl=100.0, date="2026-07-17", bet_type="TOTAL", confidence=""),
            self._grade(result="PENDING", pnl=0.0, date="2026-07-18"),
        ]
        perf = parsers.performance(grades)
        o = perf["overall"]
        self.assertEqual((o["wins"], o["losses"], o["pending"]), (2, 1, 1))
        # staked over 3 settled = $300, pnl +100 -> ROI 33.33
        self.assertAlmostEqual(o["roi"], 33.33, places=2)
        self.assertEqual([c["cumulative"] for c in perf["curve"]], [100.0, 0.0, 100.0])
        self.assertIn("ML", perf["by_bet_type"])
        self.assertIn("TOTAL", perf["by_bet_type"])

    def test_empty_grades(self):
        perf = parsers.performance([])
        self.assertIsNone(perf["overall"]["roi"])
        self.assertEqual(perf["curve"], [])


class TestReasoning(unittest.TestCase):
    def test_build_reasoning_with_frame(self):
        pred = {"game_id": "g1", "away_team_id": "mlb-cws", "home_team_id": "mlb-tor",
                "pred_away": 4.71, "pred_home": 4.42, "disp_away": 5, "disp_home": 4,
                "pred_total": 9.17, "pred_spread": 0.33, "win_prob": 0.56,
                "winner_team_id": "mlb-cws", "pred_confidence": "HIGH",
                "ml_lean": "LEAN", "total_lean": "PASS", "method_details": ""}
        frame = {"away_season_scoring_spg": "5.32", "away_season_scoring_sapg": "4.87",
                 "away_season_scoring_gp": "97", "home_season_scoring_spg": "3.97",
                 "away_last10_form_wins": "7", "away_last10_form_losses": "3",
                 "home_advantage": "0.35", "away_travel_km": "183"}
        r = reasoning.build_reasoning(pred, frame, FACTORY_DEFAULTS["MLB"], "CWS", "TOR")
        self.assertTrue(r["has_frame"])
        self.assertFalse(r["method_details_present"])  # empty -> placeholder path
        self.assertEqual(r["features"]["away"]["season_scoring"]["spg"], 5.32)
        self.assertEqual(len(r["blend"]), 5)
        self.assertAlmostEqual(sum(b["weight"] for b in r["blend"]), 1.0, places=6)

    def test_build_reasoning_without_frame(self):
        pred = {"game_id": "g1", "away_team_id": "a", "home_team_id": "b",
                "pred_away": None, "win_prob": None, "method_details": ""}
        r = reasoning.build_reasoning(pred, None, FACTORY_DEFAULTS["MLB"], "A", "B")
        self.assertFalse(r["has_frame"])
        self.assertIsNone(r["features"]["away"])


class TestValidators(unittest.TestCase):
    def test_valid_date(self):
        self.assertTrue(valid_date("2026-07-18"))
        self.assertFalse(valid_date("2026-13-40"))
        self.assertFalse(valid_date("18-07-2026"))
        self.assertFalse(valid_date("nonsense"))

    def test_valid_sport(self):
        self.assertTrue(valid_sport("mlb"))
        self.assertTrue(valid_sport("NHL"))
        self.assertFalse(valid_sport("NFL"))

    def test_safe_name(self):
        self.assertTrue(safe_name("aggressive-totals-v2"))
        self.assertFalse(safe_name("../etc/passwd"))
        self.assertFalse(safe_name("has space"))
        self.assertFalse(safe_name(""))


class TestConfigStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.store = ConfigStore(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def _valid_mlb(self, over=8.8):
        p = json.loads(json.dumps(FACTORY_DEFAULTS["MLB"]))
        p["totals"]["over"] = over
        return {"MLB": p}

    def test_save_fills_all_sports_and_is_pipeline_loadable(self):
        self.store.save("cfg-a", self._valid_mlb(), note="x")
        params = self.store.get_params("cfg-a")
        self.assertEqual(set(params), {"MLB", "NBA", "NHL", "WNBA"})  # all sports filled
        # loadable through the real pipeline loader
        from pipeline.models.config import load_params
        loaded = load_params("MLB", str(self.store.config_path("cfg-a")))
        self.assertEqual(loaded["totals"]["over"], 8.8)

    def test_invalid_params_rejected(self):
        bad = {"MLB": {**FACTORY_DEFAULTS["MLB"],
                       "weights": {"season": 0.9, "common": 0.9, "form": 0.0,
                                   "home": 0.0, "h2h": 0.0}}}
        with self.assertRaises(ConfigError):
            self.store.save("bad", bad)

    def test_overwrite_and_lock(self):
        self.store.save("cfg", self._valid_mlb())
        with self.assertRaises(ConfigError):
            self.store.save("cfg", self._valid_mlb(), overwrite=False)  # exists
        self.store.save("cfg", self._valid_mlb(over=9.0), overwrite=True)  # ok
        self.store.set_live("cfg", "MLB")
        self.assertEqual(self.store.live_map()["MLB"], "cfg")
        self.assertTrue(self.store.describe("cfg")["locked"])
        with self.assertRaises(ConfigError):  # locked -> immutable
            self.store.save("cfg", self._valid_mlb(), overwrite=True)

    def test_set_live_records_promotion_and_rollback(self):
        self.store.save("first", self._valid_mlb(over=8.5))
        self.store.save("second", self._valid_mlb(over=8.0))
        self.store.set_live("first", "MLB")
        self.store.set_live("second", "MLB")
        promos = self.store.promotions()
        self.assertEqual(promos[0]["new"], "second")
        self.assertEqual(promos[0]["old"], "first")
        self.store.rollback("MLB")
        self.assertEqual(self.store.live_map()["MLB"], "first")

    def test_duplicate_and_archive(self):
        self.store.save("orig", self._valid_mlb())
        self.store.duplicate("orig", "copy")
        self.assertTrue(self.store.exists("copy"))
        self.store.archive("copy")
        self.assertTrue(self.store.describe("copy")["archived"])


class TestTeamRegistry(unittest.TestCase):
    def test_resolves_and_falls_back(self):
        reg = TeamRegistry([
            {"team_id": "mlb-cws", "sport": "MLB", "name": "Chicago White Sox",
             "abbrev": "CWS"},
        ])
        self.assertEqual(reg.abbrev("mlb-cws"), "CWS")
        self.assertEqual(reg.name("mlb-cws"), "Chicago White Sox")
        self.assertEqual(reg.label("unknown-id"), "unknown-id")  # graceful fallback


class TestServiceAgainstTempTree(unittest.TestCase):
    """service.py reading a hand-built data tree (no real repo data needed)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        (self.data / "reports").mkdir(parents=True)
        (self.data / "reports" / "daily_2026-07-18.md").write_text(SAMPLE_REPORT)
        pdir = self.data / "predictions" / "mlb"
        pdir.mkdir(parents=True)
        (pdir / "predictions_2026-07-18.csv").write_text(
            "status,ml_lean,total_lean,game_id,sport,date,away_team_id,home_team_id,"
            "pred_away,pred_home,disp_away,disp_home,pred_total,pred_spread,win_prob,"
            "winner_team_id,pred_confidence,method_details\n"
            "ok,LEAN,PASS,g1,MLB,2026-07-18,mlb-cws,mlb-tor,4.71,4.42,5,4,9.17,0.33,0.56,"
            "mlb-cws,HIGH,\n"
        )
        (pdir / "picks_2026-07-18.csv").write_text(
            "game_id,sport,date,bet_type,selection,confidence,line\n"
            "g1,MLB,2026-07-18,ML,mlb-cws,LEAN,0.55\n"
        )
        (self.data / "teams.csv").write_text(
            "team_id,sport,name,abbrev,conference,division,external_ids,venue_name,"
            "venue_lat,venue_lon,timezone\n"
            "mlb-cws,MLB,Chicago White Sox,CWS,AL,Central,{},Rate Field,41.8,-87.6,America/Chicago\n"
            "mlb-tor,MLB,Toronto Blue Jays,TOR,AL,East,{},Rogers Centre,43.6,-79.3,America/Toronto\n"
        )
        self.teams = TeamRegistry.load(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_runs(self):
        runs = service.list_runs(self.data)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["date"], "2026-07-18")
        self.assertEqual(runs[0]["picks"], 10)

    def test_read_predictions_resolves_teams_and_publish_flags(self):
        data = service.read_predictions(self.data, "MLB", "2026-07-18", self.teams)
        self.assertEqual(data["counts"]["games"], 1)
        row = data["predictions"][0]
        self.assertEqual(row["away"]["abbrev"], "CWS")
        self.assertEqual(row["home"]["name"], "Toronto Blue Jays")
        self.assertTrue(row["published_ml"])
        self.assertFalse(row["published_total"])  # only ML was published

    def test_available_dates(self):
        self.assertEqual(service.available_dates(self.data, "MLB"), ["2026-07-18"])

    def test_reasoning_without_frame_uses_placeholder(self):
        r = service.read_reasoning(self.data, "MLB", "2026-07-18", "g1",
                                   FACTORY_DEFAULTS["MLB"], self.teams)
        self.assertIsNotNone(r)
        self.assertFalse(r["method_details_present"])
        self.assertFalse(r["has_frame"])  # no frame CSV in this temp tree

    def test_performance_empty_state(self):
        perf = service.performance(self.data)
        self.assertFalse(perf["has_grades"])


if __name__ == "__main__":
    unittest.main()
