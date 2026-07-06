#!/usr/bin/env python3
"""Edge report — the model's number vs the market's number.

The model predicts odds-blind (deliberately); this joins its predictions
to matched market odds and answers "is there an edge?":

  * Moneyline: model win probability minus the market's implied
    probability at the consensus (median) price, plus the EV of a $100
    stake at that price.
  * Totals: the model's predicted total vs the market's consensus line —
    the direction the model leans relative to the MARKET line (which is
    the actual bettable proposition), with the price on that side.
"""

from __future__ import annotations

from pipeline.odds.money import ev_per_100, implied_prob
from pipeline.odds.store import consensus_h2h, consensus_total

EDGE_FIELDS = [
    "game_id", "event_id", "sport", "date", "away_team_id", "home_team_id",
    "bet_type", "selection", "model_prob", "model_total",
    "market_price", "market_point", "implied_prob", "edge", "ev_per_100",
    "books",
]


def edge_rows(prediction: dict, event_id: str, snapshot_rows: list[dict]) -> list[dict]:
    """Edge rows for one prediction CSV row (status == ok) joined to its
    odds event. Returns 0-2 rows (ML and/or TOTAL, when the market has them)."""
    base = {
        "game_id": prediction["game_id"], "event_id": event_id,
        "sport": prediction["sport"], "date": prediction["date"],
        "away_team_id": prediction["away_team_id"],
        "home_team_id": prediction["home_team_id"],
    }
    out: list[dict] = []

    winner = prediction["winner_team_id"]
    win_prob = float(prediction["win_prob"])
    h2h = consensus_h2h(snapshot_rows, event_id, winner)
    if h2h:
        price, books = h2h
        imp = round(implied_prob(price), 4)
        out.append({
            **base, "bet_type": "ML", "selection": winner,
            "model_prob": win_prob, "model_total": "",
            "market_price": price, "market_point": "",
            "implied_prob": imp, "edge": round(win_prob - imp, 4),
            "ev_per_100": ev_per_100(win_prob, price), "books": books,
        })

    pred_total = float(prediction["pred_total"])
    # Direction vs the market's line (the bettable proposition).
    for side in ("OVER", "UNDER"):
        tot = consensus_total(snapshot_rows, event_id, side)
        if tot is None:
            continue
        point, price, books = tot
        leans_this_way = pred_total > point if side == "OVER" else pred_total < point
        if not leans_this_way:
            continue
        out.append({
            **base, "bet_type": "TOTAL", "selection": side,
            "model_prob": "", "model_total": pred_total,
            "market_price": price, "market_point": point,
            "implied_prob": round(implied_prob(price), 4),
            "edge": round(abs(pred_total - point), 4),  # units off the market line
            "ev_per_100": "", "books": books,
        })
        break
    return out
