"""Odds package — The Odds API adapter, matching, edge, and payout math.

Flow: the model predicts odds-blind; this package joins market odds on
afterward. `fetch` pulls + caches odds, normalizes them to team_id-keyed
rows, and syncs the durable game_id <-> event_id map; `edge` compares the
model's numbers to the market's; grading uses the same join for real
payouts. Works for future events, live odds, and historical snapshots.
"""
from pipeline.odds.money import (  # noqa: F401
    american_to_decimal,
    decimal_to_american,
    ev_per_100,
    implied_prob,
    median_price,
    payout_per_100,
)
