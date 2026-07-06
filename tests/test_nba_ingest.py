"""Offline tests for the NBA ingester (pipeline/ingest/nba.py).

Runs entirely against tests/fixtures/nba_schedule_sample.json — no network.
From the repo root:  python3 -m unittest tests.test_nba_ingest -v
"""
import copy
import json
import unittest
from datetime import date
from pathlib import Path

from pipeline.ingest.nba import (
    UnmappedTeamError,
    common_opponents,
    completed_regular_season,
    default_season,
    load_team_maps,
    parse_games,
    team_log,
    todays_games,
)

FIXTURE = Path(__file__).parent / "fixtures" / "nba_schedule_sample.json"


def load_fixture_games():
    doc = json.loads(FIXTURE.read_text())
    by_nba_id, _ = load_team_maps()
    return parse_games(doc, by_nba_id), doc


class TestTeamMaps(unittest.TestCase):
    def test_all_30_teams_mapped_by_nba_id(self):
        by_nba_id, by_abbrev = load_team_maps()
        self.assertEqual(len(by_nba_id), 30)
        self.assertEqual(len(by_abbrev), 30)
        self.assertEqual(by_nba_id[1610612738], ("nba-bos", "BOS"))
        self.assertEqual(by_abbrev["NYK"], "nba-nyk")

    def test_default_season_spans_years(self):
        self.assertEqual(default_season(date(2026, 7, 6)), "2025-26")
        self.assertEqual(default_season(date(2026, 10, 20)), "2026-27")
        self.assertEqual(default_season(date(2027, 1, 15)), "2026-27")


class TestParse(unittest.TestCase):
    def setUp(self):
        self.games, self.doc = load_fixture_games()

    def test_counts_and_exhibitions_skipped(self):
        # 13 fixture games, minus the preseason game vs an unmapped
        # international club and the All-Star game (synthetic team ids).
        self.assertEqual(len(self.games), 11)
        ids = {g.game_id for g in self.games}
        self.assertNotIn("0012500001", ids)
        self.assertNotIn("0032500001", ids)

    def test_season_and_types_from_game_id(self):
        by_id = {g.game_id: g for g in self.games}
        self.assertEqual(by_id["0022500101"].season, "2025-26")
        self.assertEqual(by_id["0022500101"].season_type, "regular")
        self.assertEqual(by_id["0062500001"].season_type, "nbacup_final")
        self.assertEqual(by_id["0052500001"].season_type, "playin")

    def test_scores_only_on_finals(self):
        by_id = {g.game_id: g for g in self.games}
        final = by_id["0022500101"]
        self.assertEqual(final.status, "final")
        self.assertEqual((final.away_score, final.home_score), ("110", "100"))
        scheduled = by_id["0022501201"]
        self.assertEqual(scheduled.status, "scheduled")
        self.assertEqual((scheduled.away_score, scheduled.home_score), ("", ""))

    def test_eastern_date_from_utc(self):
        # 2026-04-13T00:30:00Z is still April 12 in US/Eastern.
        by_id = {g.game_id: g for g in self.games}
        self.assertEqual(by_id["0022501202"].date, "2026-04-12")

    def test_unmapped_regular_season_team_is_fatal(self):
        doc = copy.deepcopy(self.doc)
        bad = doc["leagueSchedule"]["gameDates"][1]["games"][0]  # first regular game
        bad["homeTeam"]["teamId"] = 99999
        by_nba_id, _ = load_team_maps()
        with self.assertRaises(UnmappedTeamError):
            parse_games(doc, by_nba_id)


class TestQueries(unittest.TestCase):
    def setUp(self):
        self.games, _ = load_fixture_games()

    def test_todays_games(self):
        today = todays_games(self.games, date(2026, 4, 12))
        self.assertEqual({g.game_id for g in today}, {"0022501201", "0022501202"})

    def test_completed_excludes_cup_final_playin_scheduled(self):
        finals = completed_regular_season(self.games)
        self.assertEqual(len(finals), 7)
        ids = {g.game_id for g in finals}
        self.assertNotIn("0062500001", ids)  # NBA Cup final — no standings credit
        self.assertNotIn("0052500001", ids)  # play-in (scheduled anyway)
        self.assertNotIn("0022501201", ids)  # scheduled

    def test_team_log_excludes_cup_final(self):
        log = team_log(self.games, "nba-bos")
        self.assertEqual(len(log), 4)
        self.assertNotIn("0062500001", {r["game_id"] for r in log})
        results = [r["result"] for r in log]
        self.assertEqual(results, ["W", "W", "W", "W"])

    def test_common_opponents_math(self):
        # Hand-computed from the fixture:
        # BOS vs common opps {MIA x2, PHI x1}: wRF = (110+105+112)/3, wRA = (100+95+88)/3
        # NYK vs common opps {MIA x1, PHI x2}: wRF = (99+120+108)/3,  wRA = (101+115+97)/3
        out = common_opponents(self.games, "nba-bos", "nba-nyk")
        self.assertEqual(out["common_opponents"], ["nba-mia", "nba-phi"])

        bos = out["nba-bos"]
        self.assertEqual(bos["games"], 3)
        self.assertAlmostEqual(bos["wRF"], 109.0)
        self.assertAlmostEqual(bos["wRA"], 94.3333)
        self.assertEqual(bos["per_opponent"]["nba-mia"], {"games": 2, "avg_scored": 107.5, "avg_allowed": 97.5})

        nyk = out["nba-nyk"]
        self.assertEqual(nyk["games"], 3)
        self.assertAlmostEqual(nyk["wRF"], 109.0)
        self.assertAlmostEqual(nyk["wRA"], 104.3333)
        self.assertEqual(nyk["per_opponent"]["nba-phi"], {"games": 2, "avg_scored": 114.0, "avg_allowed": 106.0})

    def test_h2h_games_excluded_from_common_opponents(self):
        # BOS@NYK on 12-13 must not leak into either side's rows.
        out = common_opponents(self.games, "nba-bos", "nba-nyk")
        self.assertNotIn("nba-nyk", out["nba-bos"]["per_opponent"])
        self.assertNotIn("nba-bos", out["nba-nyk"]["per_opponent"])


if __name__ == "__main__":
    unittest.main()
