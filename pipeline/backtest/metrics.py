#!/usr/bin/env python3
"""Backtest/live metrics — one module so 'live vs backtest expectation'
is always an apples-to-apples comparison (PLAN.md §6).

Conventions:
  * flat $100 stake per pick; WIN pays the market payout when a price is
    attached, else +100 (see grading)
  * ROI = P&L / total staked; PUSHes stake and return, VOID/PENDING never
    stake at all
  * pass rate = predicted games that produced zero picks / predicted
    games — sitting out is an output, so it gets measured
  * confidence tiers: ML STRONG, ML LEAN, TOTAL
"""

from __future__ import annotations

from pipeline.grading.grader import Grade

STAKE = 100


def bucket(grades: list[Grade]) -> dict:
    wins = sum(1 for g in grades if g.result == "WIN")
    losses = sum(1 for g in grades if g.result == "LOSS")
    pushes = sum(1 for g in grades if g.result == "PUSH")
    staked = STAKE * (wins + losses + pushes)
    pnl = round(sum(g.pnl for g in grades), 2)
    return {
        "record": f"{wins}-{losses}-{pushes}",
        "wins": wins, "losses": losses, "pushes": pushes,
        "voids": sum(1 for g in grades if g.result == "VOID"),
        "pending": sum(1 for g in grades if g.result == "PENDING"),
        "picks": wins + losses + pushes,
        "staked": staked,
        "pnl": pnl,
        "roi": round(pnl / staked, 4) if staked else None,
    }


def tier_of(grade: Grade) -> str:
    return grade.confidence if grade.bet_type == "ML" else "TOTAL"


def compute_metrics(grades: list[Grade], games_seen: int, games_predicted: int,
                    games_no_pick: int) -> dict:
    """The full metrics block for one run (candidate or baseline)."""
    out = {
        "games": games_seen,
        "predicted": games_predicted,
        "no_prediction": games_seen - games_predicted,
        "pass_rate": round(games_no_pick / games_predicted, 4) if games_predicted else None,
        "overall": bucket(grades),
        "by_bet_type": {bt: bucket([g for g in grades if g.bet_type == bt])
                        for bt in sorted({g.bet_type for g in grades})},
        "by_tier": {t: bucket([g for g in grades if tier_of(g) == t])
                    for t in sorted({tier_of(g) for g in grades})},
        "cumulative_pnl": [],
    }
    running = 0.0
    for day in sorted({g.date for g in grades}):
        running += sum(g.pnl for g in grades if g.date == day)
        out["cumulative_pnl"].append([day, round(running, 2)])
    return out
