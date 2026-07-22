"""The dashboard's read-only data layer.

Everything the pages need, assembled from artifacts under ``data/``. It
leans on the existing ``pipeline.api`` service/parsers layer for anything
that already exists there (performance, predictions, runs) and only adds
what that layer does not expose: a plain reader over the per-sport games
CSVs, and a straight-up "did the predicted winner win" accuracy join.

Nothing here writes; nothing here touches ``pipeline/api``'s own modules
beyond importing and calling them.
"""

from __future__ import annotations

import csv
import glob
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from pipeline.api import service
from pipeline.api.paths import SPORTS, default_data_dir
from pipeline.api.teams import TeamRegistry
from pipeline.ingest.core import EASTERN, REPO_ROOT
from flask_dashboard import schema_meta

# Sports that actually have a games feed on disk right now, in a friendly order.
_GAME_SPORTS = ("MLB", "NHL", "NBA", "WNBA")


def _num(value, cast=int):
    if value is None or value == "":
        return None
    try:
        return cast(value)
    except (ValueError, TypeError):
        return None


class DataAccess:
    """One instance per app; cheap CSV reads are cached per process."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or default_data_dir()
        self.repo_root = REPO_ROOT

    # ---------------------------------------------------------------- basics

    @property
    def teams(self) -> TeamRegistry:
        return TeamRegistry.load(self.data_dir)

    def today(self) -> str:
        return datetime.now(EASTERN).date().isoformat()

    @property
    def sports(self) -> tuple[str, ...]:
        return SPORTS

    # ----------------------------------------------------------------- games

    @lru_cache(maxsize=None)
    def _games(self, sport: str) -> list[dict]:
        """Every game row for a sport across all season files, typed and
        team-name enriched, sorted by date then start time."""
        teams = self.teams
        rows: list[dict] = []
        pattern = str(self.data_dir / sport.lower() / "games_*.csv")
        for path in glob.glob(pattern):
            with open(path, newline="") as f:
                for r in csv.DictReader(f):
                    away = teams.get(r.get("away_team_id", ""))
                    home = teams.get(r.get("home_team_id", ""))
                    rows.append(
                        {
                            "sport": sport,
                            "game_id": r.get("game_id", ""),
                            "date": r.get("date", ""),
                            "season": r.get("season", ""),
                            "season_type": r.get("season_type", ""),
                            "start_time_utc": r.get("start_time_utc", ""),
                            "status": r.get("status", ""),
                            "away_team_id": r.get("away_team_id", ""),
                            "home_team_id": r.get("home_team_id", ""),
                            "away_abbrev": r.get("away_abbrev") or away["abbrev"],
                            "home_abbrev": r.get("home_abbrev") or home["abbrev"],
                            "away_name": away["name"],
                            "home_name": home["name"],
                            "away_score": _num(r.get("away_score")),
                            "home_score": _num(r.get("home_score")),
                            "venue": r.get("venue", ""),
                            "ot": r.get("ot", ""),
                            "doubleheader": r.get("doubleheader", ""),
                        }
                    )
        rows.sort(key=lambda g: (g["date"], g["start_time_utc"], g["game_id"]))
        return rows

    def _games_present(self) -> list[str]:
        """Sports that actually have at least one games file on disk."""
        present = []
        for s in _GAME_SPORTS:
            if glob.glob(str(self.data_dir / s.lower() / "games_*.csv")):
                present.append(s)
        return present

    @staticmethod
    def _winner(g: dict) -> str | None:
        if g["status"] != "final" or g["away_score"] is None or g["home_score"] is None:
            return None
        if g["home_score"] > g["away_score"]:
            return g["home_team_id"]
        if g["away_score"] > g["home_score"]:
            return g["away_team_id"]
        return None  # tie (shouldn't happen in these sports, but be safe)

    def game_counts(self) -> list[dict]:
        """Per-sport totals: how many games exist, past (final), future
        (scheduled), and on today's slate."""
        today = self.today()
        out = []
        for sport in self._games_present():
            games = self._games(sport)
            out.append(
                {
                    "sport": sport,
                    "total": len(games),
                    "final": sum(1 for g in games if g["status"] == "final"),
                    "scheduled": sum(1 for g in games if g["status"] == "scheduled"),
                    "postponed": sum(1 for g in games if g["status"] == "postponed"),
                    "today": sum(1 for g in games if g["date"] == today),
                    "future": sum(1 for g in games if g["date"] > today),
                    "past": sum(1 for g in games if g["date"] < today),
                    "first_date": games[0]["date"] if games else "",
                    "last_date": games[-1]["date"] if games else "",
                }
            )
        return out

    def games_view(self, sport: str, when: str = "today",
                   limit: int = 200) -> dict:
        """Filtered games list for the browser page.

        ``when`` ∈ {today, past, future, all}. Past is returned most-recent
        first; future/all oldest first (natural reading order for a slate)."""
        sport = sport.upper()
        today = self.today()
        games = self._games(sport)

        if when == "today":
            rows = [g for g in games if g["date"] == today]
        elif when == "past":
            rows = [g for g in games if g["date"] < today][::-1]
        elif when == "future":
            rows = [g for g in games if g["date"] > today]
        else:  # all — most recent first
            rows = games[::-1]

        total = len(rows)
        return {
            "sport": sport,
            "when": when,
            "today": today,
            "total": total,
            "rows": rows[:limit],
            "truncated": total > limit,
            "limit": limit,
        }

    def todays_games(self) -> list[dict]:
        """Today's slate for every sport that has one, each game annotated
        with the model's prediction/pick where the pipeline produced one."""
        today = self.today()
        teams = self.teams
        out = []
        for sport in self._games_present():
            games = [g for g in self._games(sport) if g["date"] == today]
            if not games:
                continue
            preds = self._prediction_map(sport, today, teams)
            annotated = [{**g, "prediction": preds.get(g["game_id"])} for g in games]
            out.append({"sport": sport, "games": annotated})
        return out

    def _prediction_map(self, sport: str, date: str,
                        teams: TeamRegistry) -> dict[str, dict]:
        """game_id -> a compact prediction summary for a sport+date."""
        try:
            data = service.read_predictions(self.data_dir, sport, date, teams)
        except Exception:
            return {}
        out = {}
        for p in data.get("predictions", []):
            out[p["game_id"]] = {
                "status": p["status"],
                "ml_lean": p["ml_lean"],
                "total_lean": p["total_lean"],
                "win_prob": p["win_prob"],
                "winner_abbrev": p.get("winner_abbrev", ""),
                "disp_away": p.get("disp_away"),
                "disp_home": p.get("disp_home"),
                "confidence": p.get("pred_confidence", ""),
                "published_ml": p.get("published_ml", False),
                "published_total": p.get("published_total", False),
            }
        return out

    # ---------------------------------------------------- predictions / dates

    def prediction_dates(self, sport: str) -> list[str]:
        return service.available_dates(self.data_dir, sport.upper())

    def prediction_dates_all(self) -> dict[str, list[str]]:
        return {s: service.available_dates(self.data_dir, s) for s in SPORTS}

    def predictions(self, sport: str, date: str) -> dict:
        return service.read_predictions(self.data_dir, sport.upper(), date,
                                        self.teams)

    # ----------------------------------------------------------- performance

    def performance(self) -> dict:
        return service.performance(self.data_dir)

    def winner_accuracy(self) -> dict:
        """How often the predicted winner actually won — a straight-up
        accuracy that complements the betting ROI.

        Joins every available predictions CSV to final game rows. Only
        finished games with a non-toss-up predicted winner count."""
        overall = {"correct": 0, "total": 0}
        by_sport: dict[str, dict] = {}
        by_date: dict[str, dict] = {}

        for sport in SPORTS:
            finals = {g["game_id"]: g for g in self._games(sport)
                      if g["status"] == "final"} if sport in _GAME_SPORTS else {}
            for date in service.available_dates(self.data_dir, sport):
                try:
                    data = self.predictions(sport, date)
                except Exception:
                    continue
                for p in data["predictions"]:
                    if p["status"] != "ok" or not p["winner_team_id"]:
                        continue
                    game = finals.get(p["game_id"])
                    if not game:
                        continue
                    actual = self._winner(game)
                    if actual is None:
                        continue
                    correct = int(p["winner_team_id"] == actual)
                    overall["correct"] += correct
                    overall["total"] += 1
                    bs = by_sport.setdefault(sport, {"correct": 0, "total": 0})
                    bs["correct"] += correct
                    bs["total"] += 1
                    bd = by_date.setdefault(date, {"correct": 0, "total": 0,
                                                   "sport": sport})
                    bd["correct"] += correct
                    bd["total"] += 1

        def pct(d: dict) -> float | None:
            return round(100.0 * d["correct"] / d["total"], 1) if d["total"] else None

        overall["pct"] = pct(overall)
        for d in by_sport.values():
            d["pct"] = pct(d)
        for d in by_date.values():
            d["pct"] = pct(d)
        return {
            "overall": overall,
            "by_sport": {k: v for k, v in sorted(by_sport.items())},
            "by_date": {k: by_date[k] for k in sorted(by_date, reverse=True)},
        }

    # ------------------------------------------------------------------ runs

    def runs(self) -> list[dict]:
        return service.list_runs(self.data_dir)

    def run_detail(self, on: str) -> dict | None:
        return service.read_report(self.data_dir, on)

    # -------------------------------------------------------------- database

    def db_status(self) -> dict:
        """Live row counts from the SQLite/Postgres mirror, if it exists.

        The mirror is optional (built by ``python3 -m pipeline.db load``),
        so a missing/empty DB is reported plainly rather than raised."""
        import os

        from pipeline.db.core import DEFAULT_SQLITE_PATH

        # Stay read-only: don't create the SQLite file just to report on it.
        if not os.environ.get("DATABASE_URL") and not DEFAULT_SQLITE_PATH.exists():
            return {"ok": False,
                    "error": "no mirror on disk (run `python3 -m pipeline.db load`)"}
        try:
            from pipeline.db.core import Db
            with Db.connect() as db:
                db.init_schema()
                target = ("PostgreSQL (DATABASE_URL)" if db.dialect == "postgres"
                          else "SQLite — data/model.db")
                return {"ok": True, "target": target, "counts": db.counts()}
        except SystemExit as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def db_tables(self) -> list[dict]:
        return schema_meta.db_tables()

    def csv_datasets(self) -> list[dict]:
        return schema_meta.csv_datasets(self.repo_root)

    def relationships(self) -> list[dict]:
        return schema_meta.RELATIONSHIPS


# ------------------------------------------------------------- svg sparkline

def sparkline(points: list[float], width: int = 640, height: int = 120,
              pad: int = 8) -> dict:
    """A no-JS inline-SVG line of a cumulative series.

    Returns the path/zero-line/geometry the template renders — keeping the
    (tiny) bit of math out of Jinja."""
    if not points:
        return {"empty": True}
    lo, hi = min(points), max(points)
    lo = min(lo, 0.0)
    hi = max(hi, 0.0)
    span = (hi - lo) or 1.0
    n = len(points)
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad

    def x(i: int) -> float:
        return pad + (inner_w * (i / (n - 1) if n > 1 else 0.5))

    def y(v: float) -> float:
        return pad + inner_h * (1 - (v - lo) / span)

    coords = [(round(x(i), 1), round(y(v), 1)) for i, v in enumerate(points)]
    path = " ".join(
        ("M" if i == 0 else "L") + f"{px},{py}" for i, (px, py) in enumerate(coords)
    )
    return {
        "empty": False,
        "width": width,
        "height": height,
        "path": path,
        "zero_y": round(y(0.0), 1),
        "last": coords[-1],
        "up": points[-1] >= 0,
    }
