"""Offline tests for the storage layer (pipeline/db/).

Run against a temp SQLite file — the DATABASE_URL/psycopg path shares the
identical SQL and is exercised by TestPostgres only when TEST_DATABASE_URL
is set (it needs a reachable PostgreSQL and `pip install "psycopg[binary]"`).
From the repo root:  python3 -m unittest tests.test_db -v
"""
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from pipeline import backfill
from pipeline.backfill import backfill_rows, write_frame_csv
from pipeline.db.core import Db, upsert_sql
from pipeline.db.loader import load_all
from pipeline.ingest import core, wnba
from pipeline.ingest.wnba import load_team_map, parse_games

FIXTURE = Path(__file__).parent / "fixtures" / "wnba_schedule_sample.json"


class TestUpsertSql(unittest.TestCase):
    def test_shape(self):
        sql = upsert_sql("t", ["a", "b", "c"], ["a"])
        self.assertEqual(
            sql,
            "INSERT INTO t (a, b, c) VALUES (?, ?, ?) "
            "ON CONFLICT (a) DO UPDATE SET b = excluded.b, c = excluded.c",
        )

    def test_postgres_placeholder_rewrite(self):
        db = Db.__new__(Db)
        db.dialect = "postgres"
        self.assertEqual(db._q("VALUES (?, ?)"), "VALUES (%s, %s)")


class TestSqliteRoundtrip(unittest.TestCase):
    """Full load_all against a temp SQLite db built from the WNBA fixture."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmpdir.name)
        games = parse_games(json.loads(FIXTURE.read_text()), load_team_map())

        cls.patchers = [
            mock.patch.object(wnba, "OUT_DIR", tmp / "wnba"),
            mock.patch.object(backfill, "FRAMES_DIR", tmp / "frames"),
        ]
        for p_ in cls.patchers:
            p_.start()

        core.write_games_csv(games, wnba.games_csv_path("2026"))
        columns, rows = backfill_rows("WNBA", games, date(2026, 7, 6))
        write_frame_csv("WNBA", "2026", columns, rows)

        cls.db = Db.connect(url="", sqlite_path=tmp / "model.db")
        cls.report = load_all(cls.db, ["WNBA"], "2026", date(2026, 7, 6))

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        for p_ in cls.patchers:
            p_.stop()
        cls.tmpdir.cleanup()

    def test_counts(self):
        self.assertEqual(self.report["teams"]["all"], 275)
        self.assertEqual(self.report["WNBA"], {"games": 11, "frames": 11})
        self.assertEqual(self.db.counts(), {"teams": 275, "games": 11, "frames": 11})

    def test_reload_is_idempotent(self):
        load_all(self.db, ["WNBA"], "2026", date(2026, 7, 6))
        self.assertEqual(self.db.counts(), {"teams": 275, "games": 11, "frames": 11})

    def test_scores_stored_as_integers(self):
        away, home = self.db.execute(
            "SELECT away_score, home_score FROM games WHERE game_id = ?",
            ("1022600101",),
        ).fetchone()
        self.assertEqual((away, home), (90, 80))
        pending = self.db.execute(
            "SELECT away_score FROM games WHERE game_id = ?", ("1022600201",)
        ).fetchone()[0]
        self.assertIsNone(pending)

    def test_frame_row_labels_and_features_json(self):
        winner, total, feats = self.db.execute(
            "SELECT winner, actual_total, features FROM frames WHERE game_id = ?",
            ("1022600101",),
        ).fetchone()
        self.assertEqual((winner, total), ("wnba-nyl", 170))
        features = json.loads(feats)
        self.assertEqual(features["away_games_played"], 0)   # numbers stay numbers
        self.assertIsNone(features["away_last10_form_wins"])  # '' -> null

    def test_unlabeled_scheduled_game(self):
        winner, final_away = self.db.execute(
            "SELECT winner, final_away FROM frames WHERE game_id = ?",
            ("1022600201",),
        ).fetchone()
        self.assertEqual(winner, "")
        self.assertIsNone(final_away)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "set TEST_DATABASE_URL to run against a real PostgreSQL")
class TestPostgres(unittest.TestCase):
    """Same SQL, real Postgres — opt-in (needs a server + psycopg)."""

    def test_schema_and_upsert_roundtrip(self):
        with Db.connect(url=os.environ["TEST_DATABASE_URL"]) as db:
            self.assertEqual(db.dialect, "postgres")
            db.init_schema()
            sql = upsert_sql("teams", ["team_id", "sport", "name", "abbrev"], ["team_id"])
            db.executemany(sql, [("t-test", "WNBA", "Test", "TST")] * 2)
            db.commit()
            name = db.execute("SELECT name FROM teams WHERE team_id = ?",
                              ("t-test",)).fetchone()[0]
            self.assertEqual(name, "Test")
            db.execute("DELETE FROM teams WHERE team_id = ?", ("t-test",))
            db.commit()


if __name__ == "__main__":
    unittest.main()
