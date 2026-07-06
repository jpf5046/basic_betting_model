#!/usr/bin/env python3
"""Snapshot storage and closing-line lookups.

Every fetch writes one snapshots_<ts>.csv under data/odds/<sport>/ (long
format, one row per event/bookmaker/market/outcome). Lookups read all
snapshot files and, per event+market, use the LATEST snapshot taken at or
before the game's start — the closing line — falling back to the latest
available if nothing pre-game exists. Consensus price = median across
bookmakers in decimal space (money.median_price).
"""

from __future__ import annotations

import csv
from pathlib import Path

from pipeline.ingest.core import REPO_ROOT

from pipeline.odds.money import median_price
from pipeline.odds.normalize import ROW_FIELDS

ODDS_DIR = REPO_ROOT / "data" / "odds"


def write_snapshot(sport: str, rows: list[dict], snapshot_ts: str) -> Path:
    safe_ts = snapshot_ts.replace(":", "").replace("-", "")
    path = ODDS_DIR / sport.lower() / f"snapshots_{safe_ts}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ROW_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return path


def load_snapshot_rows(sport: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((ODDS_DIR / sport.lower()).glob("snapshots_*.csv")):
        with open(path, newline="") as f:
            rows.extend(csv.DictReader(f))
    return rows


def closing_rows(rows: list[dict], event_id: str, market: str) -> list[dict]:
    """The chosen snapshot's rows for one event+market: latest snapshot_ts
    at/before commence_time, else the latest snapshot there is."""
    mine = [r for r in rows if r["event_id"] == event_id and r["market"] == market
            and r["price_american"] not in ("", None)]
    if not mine:
        return []
    commence = mine[0]["commence_time"]
    pre = [r for r in mine if not commence or r["snapshot_ts"] <= commence]
    pool = pre or mine
    best_ts = max(r["snapshot_ts"] for r in pool)
    return [r for r in pool if r["snapshot_ts"] == best_ts]


def consensus_h2h(rows: list[dict], event_id: str, team_id: str
                  ) -> tuple[float, int] | None:
    """(median American price, #bookmakers) for a team's moneyline."""
    prices = [float(r["price_american"]) for r in closing_rows(rows, event_id, "h2h")
              if r["outcome"] == team_id]
    if not prices:
        return None
    return median_price(prices), len(prices)


def consensus_total(rows: list[dict], event_id: str, side: str
                    ) -> tuple[float, float, int] | None:
    """(median point, median American price, #bookmakers) for OVER/UNDER."""
    picked = [r for r in closing_rows(rows, event_id, "totals") if r["outcome"] == side
              and r["point"] not in ("", None)]
    if not picked:
        return None
    points = sorted(float(r["point"]) for r in picked)
    mid_point = points[len(points) // 2] if len(points) % 2 else (
        (points[len(points) // 2 - 1] + points[len(points) // 2]) / 2)
    price = median_price([float(r["price_american"]) for r in picked])
    return mid_point, price, len(picked)
