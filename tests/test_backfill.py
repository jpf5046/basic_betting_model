"""Offline tests for the season backfill (pipeline/backfill.py).

Runs against tests/fixtures/wnba_schedule_sample.json — no network.
From the repo root:  python3 -m unittest tests.test_backfill -v
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from pipeline import backfill
from pipeline.backfill import OUTCOME_COLS, backfill_rows, write_frame_csv
from pipeline.ingest.wnba import load_team_map, parse_games

FIXTURE = Path(__file__).parent / "fixtures" / "wnba_schedule_sample.json"


def fixture_games():
    return parse_games(json.loads(FIXTURE.read_text()), load_team_map())


class TestBackfillRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.games = fixture_games()
        cls.columns, cls.rows = backfill_rows("WNBA", cls.games, date(2026, 7, 6))
        cls.by_id = {r["game_id"]: r for r in cls.rows}

    def test_every_game_through_cutoff_present_and_sorted(self):
        self.assertEqual(len(self.rows), 11)  # all fixture games (allstar was dropped at ingest)
        self.assertEqual([r["date"] for r in self.rows], sorted(r["date"] for r in self.rows))

    def test_through_cutoff_excludes_later_dates(self):
        _, rows = backfill_rows("WNBA", self.games, date(2026, 7, 4))
        self.assertEqual(len(rows), 9)  # the two 07-05 scheduled games drop off

    def test_as_of_equals_game_date(self):
        for r in self.rows:
            self.assertEqual(r["as_of_date"], r["date"])

    def test_point_in_time_no_leakage(self):
        # Season opener: no completed regular-season games exist before it,
        # so evidence features must be zero/empty even though the CSV knows
        # the whole season's results.
        opener = self.by_id["1022600101"]
        self.assertEqual(opener["away_games_played"], 0)
        self.assertEqual(opener["home_games_played"], 0)
        # A late slate sees prior games.
        late = self.by_id["1022600201"]  # 2026-07-05
        self.assertGreater(late["away_games_played"], 0)

    def test_outcome_labels_on_finals_only(self):
        final = self.by_id["1022600101"]  # NYL 90 @ IND 80
        self.assertEqual(
            {c: final[c] for c in OUTCOME_COLS},
            {"final_away": 90, "final_home": 80, "actual_total": 170,
             "actual_margin": -10, "winner": "wnba-nyl"},
        )
        scheduled = self.by_id["1022600201"]
        self.assertEqual({final_col: scheduled[final_col] for final_col in OUTCOME_COLS},
                         {c: "" for c in OUTCOME_COLS})
        self.assertEqual(sum(1 for r in self.rows if r["winner"]), 9)

    def test_column_ordering_and_meta(self):
        self.assertEqual(self.columns[:6],
                         ["game_id", "sport", "season", "season_type", "date", "as_of_date"])
        self.assertEqual(self.columns[-6:], OUTCOME_COLS + ["feature_versions"])
        versions = json.loads(self.rows[0]["feature_versions"])
        self.assertIn("season_scoring", versions)

    def test_frame_csv_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(backfill, "FRAMES_DIR", Path(tmp)):
                path = write_frame_csv("WNBA", "2026", self.columns, self.rows)
            self.assertEqual(path.name, "wnba_2026.csv")
            header = path.read_text().splitlines()[0]
            self.assertTrue(header.startswith("game_id,sport,season"))


if __name__ == "__main__":
    unittest.main()
