"""Offline tests for the MLB ingester (pipeline/ingest/mlb.py).

Runs entirely against tests/fixtures/mlb_schedule_sample.json — no network.
From the repo root:  python3 -m unittest tests.test_mlb_ingest -v
"""
import copy
import json
import unittest
from datetime import date
from pathlib import Path

from pipeline.ingest.mlb import (
    UnmappedTeamError,
    common_opponents,
    completed_regular_season,
    load_team_maps,
    parse_games,
    team_log,
    todays_games,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mlb_schedule_sample.json"


def load_fixture_games():
    doc = json.loads(FIXTURE.read_text())
    by_mlb_id, _ = load_team_maps()
    return parse_games(doc, by_mlb_id), doc


class TestTeamMaps(unittest.TestCase):
    def test_all_30_teams_mapped_by_mlb_id(self):
        by_mlb_id, by_abbrev = load_team_maps()
        self.assertEqual(len(by_mlb_id), 30)
        self.assertEqual(len(by_abbrev), 30)
        self.assertEqual(by_mlb_id[147], ("mlb-nyy", "NYY"))
        self.assertEqual(by_abbrev["BOS"], "mlb-bos")


class TestParse(unittest.TestCase):
    def setUp(self):
        self.games, self.doc = load_fixture_games()

    def test_counts_and_exhibition_skipped(self):
        # 13 fixture games, minus the exhibition vs an unmapped college team.
        self.assertEqual(len(self.games), 12)
        self.assertNotIn("813013", {g.game_id for g in self.games})

    def test_season_type_from_game_type(self):
        by_id = {g.game_id: g for g in self.games}
        self.assertEqual(by_id["813010"].season_type, "preseason")
        self.assertEqual(by_id["813001"].season_type, "regular")

    def test_postponed_never_a_result(self):
        # abstractGameState says "Final" but detailedState "Postponed" wins.
        by_id = {g.game_id: g for g in self.games}
        ppd = by_id["813009"]
        self.assertEqual(ppd.status, "postponed")
        self.assertEqual((ppd.away_score, ppd.home_score), ("", ""))

    def test_scores_only_on_finals(self):
        by_id = {g.game_id: g for g in self.games}
        final = by_id["813001"]
        self.assertEqual(final.status, "final")
        self.assertEqual((final.away_score, final.home_score), ("5", "3"))
        scheduled = by_id["813011"]
        self.assertEqual(scheduled.status, "scheduled")
        self.assertEqual((scheduled.away_score, scheduled.home_score), ("", ""))

    def test_doubleheader_games_kept_distinct(self):
        by_id = {g.game_id: g for g in self.games}
        g1, g2 = by_id["813005"], by_id["813006"]
        self.assertEqual((g1.doubleheader, g1.game_number), ("Y", "1"))
        self.assertEqual((g2.doubleheader, g2.game_number), ("Y", "2"))
        self.assertEqual(g1.date, g2.date)

    def test_official_date_used(self):
        by_id = {g.game_id: g for g in self.games}
        # 23:10Z would already be June 2 in UTC; officialDate keeps it June 1.
        self.assertEqual(by_id["813001"].date, "2026-06-01")

    def test_unmapped_regular_season_team_is_fatal(self):
        doc = copy.deepcopy(self.doc)
        bad = doc["dates"][2]["games"][0]  # first regular-season game
        bad["teams"]["home"]["team"]["id"] = 99999
        by_mlb_id, _ = load_team_maps()
        with self.assertRaises(UnmappedTeamError):
            parse_games(doc, by_mlb_id)


class TestQueries(unittest.TestCase):
    def setUp(self):
        self.games, _ = load_fixture_games()

    def test_todays_games(self):
        today = todays_games(self.games, date(2026, 7, 5))
        self.assertEqual({g.game_id for g in today}, {"813011", "813012"})

    def test_completed_excludes_spring_postponed_scheduled(self):
        finals = completed_regular_season(self.games)
        self.assertEqual(len(finals), 8)
        ids = {g.game_id for g in finals}
        self.assertNotIn("813010", ids)  # spring training final
        self.assertNotIn("813009", ids)  # postponed
        self.assertNotIn("813011", ids)  # scheduled

    def test_team_log_includes_both_doubleheader_games(self):
        log = team_log(self.games, "mlb-bal")
        self.assertEqual(len(log), 3)
        dh_rows = [r for r in log if r["date"] == "2026-06-05"]
        self.assertEqual(len(dh_rows), 2)
        self.assertEqual([r["result"] for r in dh_rows], ["L", "W"])

    def test_common_opponents_math(self):
        # Hand-computed from the fixture:
        # NYY vs common opps {TB x2, BAL x1}: wRF = (5+2+4)/3, wRA = (3+6+1)/3
        # BOS vs common opps {TB x1, BAL x2}: wRF = (7+5+2)/3, wRA = (4+3+8)/3
        out = common_opponents(self.games, "mlb-nyy", "mlb-bos")
        self.assertEqual(out["common_opponents"], ["mlb-bal", "mlb-tb"])

        nyy = out["mlb-nyy"]
        self.assertEqual(nyy["games"], 3)
        self.assertAlmostEqual(nyy["wRF"], 3.6667)
        self.assertAlmostEqual(nyy["wRA"], 3.3333)
        self.assertEqual(nyy["per_opponent"]["mlb-tb"], {"games": 2, "avg_scored": 3.5, "avg_allowed": 4.5})

        bos = out["mlb-bos"]
        self.assertEqual(bos["games"], 3)
        self.assertAlmostEqual(bos["wRF"], 4.6667)
        self.assertAlmostEqual(bos["wRA"], 5.0)
        self.assertEqual(bos["per_opponent"]["mlb-bal"], {"games": 2, "avg_scored": 3.5, "avg_allowed": 5.5})

    def test_h2h_games_excluded_from_common_opponents(self):
        # NYY@BOS on 06-06 must not leak into either side's rows.
        out = common_opponents(self.games, "mlb-nyy", "mlb-bos")
        self.assertNotIn("mlb-bos", out["mlb-nyy"]["per_opponent"])
        self.assertNotIn("mlb-nyy", out["mlb-bos"]["per_opponent"])


if __name__ == "__main__":
    unittest.main()
