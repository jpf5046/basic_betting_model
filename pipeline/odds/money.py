#!/usr/bin/env python3
"""Betting math on American odds — the one place payout/edge arithmetic lives.

American odds: +150 means a $100 stake wins $150; -150 means it takes a
$150 stake to win $100 (a $100 stake wins $66.67). Decimal odds are the
total-return multiplier (stake included): +150 -> 2.50, -150 -> 1.6667.

Medians across bookmakers are taken in DECIMAL space — American numbers
aren't continuous through the ±100 boundary (the median of +100 and -110
as raw numbers is a meaningless -5), decimal odds are.
"""

from __future__ import annotations

from statistics import median


def american_to_decimal(odds: float) -> float:
    if odds >= 100:
        return 1.0 + odds / 100.0
    if odds <= -100:
        return 1.0 + 100.0 / abs(odds)
    raise ValueError(f"invalid American odds {odds!r} (|odds| must be >= 100)")


def decimal_to_american(dec: float) -> float:
    if dec <= 1.0:
        raise ValueError(f"invalid decimal odds {dec!r}")
    if dec >= 2.0:
        return round((dec - 1.0) * 100.0, 1)
    return round(-100.0 / (dec - 1.0), 1)


def implied_prob(odds: float) -> float:
    """Bookmaker's implied win probability (vig included)."""
    if odds >= 100:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def payout_per_100(odds: float) -> float:
    """Profit on a winning $100 stake."""
    return round((american_to_decimal(odds) - 1.0) * 100.0, 2)


def ev_per_100(win_prob: float, odds: float) -> float:
    """Expected profit of a $100 stake given OUR win probability and the
    market's price. Positive EV = the model thinks there's an edge."""
    return round(win_prob * payout_per_100(odds) - (1.0 - win_prob) * 100.0, 2)


def median_price(american_prices: list[float]) -> float:
    """Consensus price across bookmakers: median in decimal space,
    reported back in American."""
    if not american_prices:
        raise ValueError("no prices to take a median of")
    return decimal_to_american(median(american_to_decimal(p) for p in american_prices))
