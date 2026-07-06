"""Offline tests for the NHL ingester (pipeline/ingest/nhl.py).

Runs entirely against tests/fixtures/nhl_club_schedules_sample.json — a map
of tricode -> club-schedule-season feed (four clubs, overlapping games, so
dedupe is exercised). No network.
From the repo root:  python3 -m unittest tests.test_nhl_ingest -v
"""
import copy
import json
import unittest
from datetime import date
from pathlib import Path

from pipeline.ingest.nhl import (
    UnmappedTeamError,
    common_opponents,
    completed_regular_season,
    default_season,
    load_team_maps,
    parse_games,
    team_log,
    todays_games,
)

FIXTURE = Path(__file__).parent / "fixtures" / "nhl_club_schedules_sample.json"


def load_fixture_games():
    docs_by_club = json.loads(FIXTURE.read_text())
    by_nhl_id, by_abbrev = load_team_maps()
    return parse_games(list(docs_by_club.values()), by_nhl_id, by_abbrev), docs_by_club


class TestTeamMaps(unittest.TestCase):
    def test_all_32_teams_mapped(self):
        by_nhl_id, by_abbrev = load_team_maps()
        self.assertEqual(len(by_nhl_id), 32)
        self.assertEqual(len(by_abbrev), 32)
        self.assertEqual(by_nhl_id[6], ("nhl-bos", "BOS"))
        self.assertEqual(by_abbrev["TOR"], "nhl-tor")

    def test_default_season_spans_years(self):
        self.assertEqual(default_season(date(2026, 7, 6)), "20252026")
        self.assertEqual(default_season(date(2026, 10, 20)), "20262027")
        self.assertEqual(default_season(date(2027, 2, 15)), "20262027")


class TestParse(unittest.TestCase):
    def setUp(self):
        self.games, self.docs = load_fixture_games()

    def test_dedupe_across_club_feeds(self):
        # 4 club feeds contain 23 game entries but only 12 distinct games;
        # the preseason game vs an unmapped prospect team is skipped -> 11.
        self.assertEqual(len(self.games), 11)
        ids = [g.game_id for g in self.games]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("2025010001", ids)

    def test_season_types(self):
        by_id = {g.game_id: g for g in self.games}
        self.assertEqual(by_id["2025020201"].season_type, "regular")
        self.assertEqual(by_id["2025030111"].season_type, "playoffs")

    def test_postponed_never_a_result(self):
        by_id = {g.game_id: g for g in self.games}
        ppd = by_id["2025020299"]
        self.assertEqual(ppd.status, "postponed")
        self.assertEqual((ppd.away_score, ppd.home_score), ("", ""))

    def test_ot_and_shootout_recorded(self):
        by_id = {g.game_id: g for g in self.games}
        self.assertEqual(by_id["2025020229"].ot, "OT")
        self.assertEqual(by_id["2025020243"].ot, "SO")
        self.assertEqual(by_id["2025020201"].ot, "REG")

    def test_game_date_preferred_over_utc(self):
        # startTimeUTC rolls to Apr 12 but the NHL gameDate says Apr 11.
        by_id = {g.game_id: g for g in self.games}
        self.assertEqual(by_id["2025021151"].date, "2026-04-11")

    def test_tricode_fallback_when_id_unknown(self):
        # A wrong/renumbered team id still maps via the tricode. Mutate the
        # BOS feed's copy — it's the first feed parsed, so dedupe can't
        # serve the game from an unmutated copy.
        docs = copy.deepcopy(self.docs)
        game = docs["BOS"]["games"][1]  # 2025020201, BOS @ MTL
        self.assertEqual(str(game["id"]), "2025020201")
        game["awayTeam"]["id"] = 9999  # BOS with a bogus id
        by_nhl_id, by_abbrev = load_team_maps()
        games = parse_games(list(docs.values()), by_nhl_id, by_abbrev)
        parsed = {g.game_id: g for g in games}["2025020201"]
        self.assertEqual(parsed.away_team_id, "nhl-bos")

    def test_unmapped_regular_season_team_is_fatal(self):
        docs = copy.deepcopy(self.docs)
        game = docs["BOS"]["games"][1]  # first parsed copy of 2025020201
        game["awayTeam"] = {"id": 99999, "abbrev": "XXX", "score": 4}
        by_nhl_id, by_abbrev = load_team_maps()
        with self.assertRaises(UnmappedTeamError):
            parse_games(list(docs.values()), by_nhl_id, by_abbrev)


class TestQueries(unittest.TestCase):
    def setUp(self):
        self.games, _ = load_fixture_games()

    def test_todays_games(self):
        today = todays_games(self.games, date(2026, 4, 11))
        self.assertEqual({g.game_id for g in today}, {"2025021150", "2025021151"})

    def test_completed_excludes_playoffs_postponed_scheduled(self):
        finals = completed_regular_season(self.games)
        self.assertEqual(len(finals), 7)
        ids = {g.game_id for g in finals}
        self.assertNotIn("2025030111", ids)  # playoff final
        self.assertNotIn("2025020299", ids)  # postponed
        self.assertNotIn("2025021150", ids)  # scheduled

    def test_team_log_ot_losses_marked_otl(self):
        log = team_log(self.games, "nhl-tor")
        self.assertEqual(len(log), 4)
        results = [r["result"] for r in log]
        # 11-05 OT loss at MTL, 11-09 W, 11-11 W, 11-13 regulation loss to BOS
        self.assertEqual(results, ["OTL", "W", "W", "L"])

    def test_common_opponents_math(self):
        # Hand-computed from the fixture:
        # BOS vs common opps {MTL x2, NYR x1}: wRF = (4+5+3)/3, wRA = (2+1+2)/3
        # TOR vs common opps {MTL x1, NYR x2}: wRF = (3+6+5)/3, wRA = (4+3+2)/3
        out = common_opponents(self.games, "nhl-bos", "nhl-tor")
        self.assertEqual(out["common_opponents"], ["nhl-mtl", "nhl-nyr"])

        bos = out["nhl-bos"]
        self.assertEqual(bos["games"], 3)
        self.assertAlmostEqual(bos["wRF"], 4.0)
        self.assertAlmostEqual(bos["wRA"], 1.6667)
        self.assertEqual(bos["per_opponent"]["nhl-mtl"], {"games": 2, "avg_scored": 4.5, "avg_allowed": 1.5})

        tor = out["nhl-tor"]
        self.assertEqual(tor["games"], 3)
        self.assertAlmostEqual(tor["wRF"], 4.6667)
        self.assertAlmostEqual(tor["wRA"], 3.0)
        self.assertEqual(tor["per_opponent"]["nhl-nyr"], {"games": 2, "avg_scored": 5.5, "avg_allowed": 2.5})

    def test_h2h_games_excluded_from_common_opponents(self):
        # BOS@TOR on 11-13 must not leak into either side's rows.
        out = common_opponents(self.games, "nhl-bos", "nhl-tor")
        self.assertNotIn("nhl-tor", out["nhl-bos"]["per_opponent"])
        self.assertNotIn("nhl-bos", out["nhl-tor"]["per_opponent"])


if __name__ == "__main__":
    unittest.main()
