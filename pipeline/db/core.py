#!/usr/bin/env python3
"""Storage layer — PLAN.md §1, first slice.

One connection front door with the promised swap behavior:

  * `DATABASE_URL` set  -> PostgreSQL via psycopg (`pip install "psycopg[binary]"`,
                           the repo's first and only optional dependency)
  * otherwise           -> a local SQLite file, data/model.db (stdlib, zero setup)

Both dialects run the identical SQL below: portable types (TEXT/REAL/
INTEGER), `INSERT … ON CONFLICT … DO UPDATE SET x = excluded.x` upserts,
and qmark placeholders that are rewritten to %s for psycopg. Flat CSVs
under data/ remain the source of truth (PLAN §1) — the database is a
queryable mirror loaded by `python3 -m pipeline.db load`, rebuildable
from the CSVs at any time.

Tables (the §1 subset the backfill needs; the rest land with the
backtester): `teams` (from data/teams.csv), `games` (per-sport games
CSVs), `frames` (the labeled canonical frame — identity + labels as
columns, the wide feature vector as a JSON `features` column so new
features never require a migration).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from pipeline.ingest.core import REPO_ROOT

DEFAULT_SQLITE_PATH = REPO_ROOT / "data" / "model.db"

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS teams (
        team_id      TEXT PRIMARY KEY,
        sport        TEXT NOT NULL,
        name         TEXT NOT NULL,
        abbrev       TEXT NOT NULL,
        conference   TEXT,
        division     TEXT,
        external_ids TEXT,
        venue_name   TEXT,
        venue_lat    REAL,
        venue_lon    REAL,
        timezone     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS games (
        sport          TEXT NOT NULL,
        game_id        TEXT NOT NULL,
        season         TEXT NOT NULL,
        season_type    TEXT NOT NULL,
        date           TEXT NOT NULL,
        start_time_utc TEXT,
        status         TEXT NOT NULL,
        away_team_id   TEXT NOT NULL,
        home_team_id   TEXT NOT NULL,
        away_abbrev    TEXT,
        home_abbrev    TEXT,
        away_score     INTEGER,
        home_score     INTEGER,
        venue          TEXT,
        ot             TEXT,
        doubleheader   TEXT,
        game_number    TEXT,
        PRIMARY KEY (sport, game_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS frames (
        sport            TEXT NOT NULL,
        game_id          TEXT NOT NULL,
        season           TEXT NOT NULL,
        season_type      TEXT,
        date             TEXT NOT NULL,
        as_of_date       TEXT NOT NULL,
        away_team_id     TEXT,
        home_team_id     TEXT,
        status           TEXT,
        features         TEXT NOT NULL,
        feature_versions TEXT NOT NULL,
        final_away       INTEGER,
        final_home       INTEGER,
        actual_total     INTEGER,
        actual_margin    INTEGER,
        winner           TEXT,
        PRIMARY KEY (sport, game_id)
    )
    """,
]


class Db:
    """Thin DB-API wrapper hiding the sqlite/postgres differences."""

    def __init__(self, conn, dialect: str):
        self.conn = conn
        self.dialect = dialect  # "sqlite" | "postgres"

    @classmethod
    def connect(cls, url: str | None = None, sqlite_path: Path | None = None) -> "Db":
        """DATABASE_URL (explicit arg wins) -> postgres; else local SQLite."""
        url = url if url is not None else os.environ.get("DATABASE_URL", "")
        if url:
            try:
                import psycopg
            except ImportError:
                sys.exit(
                    "DATABASE_URL is set but the driver is missing — "
                    "pip install \"psycopg[binary]\" (the only optional dependency)"
                )
            return cls(psycopg.connect(url), "postgres")
        path = sqlite_path or DEFAULT_SQLITE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(path), "sqlite")

    def _q(self, sql: str) -> str:
        # Our SQL never contains a literal '?', so a plain rewrite is safe.
        return sql if self.dialect == "sqlite" else sql.replace("?", "%s")

    def execute(self, sql: str, params: tuple = ()):
        cur = self.conn.cursor()
        cur.execute(self._q(sql), params)
        return cur

    def executemany(self, sql: str, rows: list[tuple]) -> int:
        if not rows:
            return 0
        cur = self.conn.cursor()
        cur.executemany(self._q(sql), rows)
        return len(rows)

    def init_schema(self) -> None:
        for stmt in SCHEMA:
            self.execute(stmt)
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("teams", "games", "frames"):
            out[table] = self.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Db":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def upsert_sql(table: str, columns: list[str], pk: list[str]) -> str:
    """Portable `INSERT … ON CONFLICT (pk) DO UPDATE` for both dialects."""
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in pk)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {updates}"
    )
