"""Backtest harness — replay a model over historical finals and score it.

The whole point of this package is *honest measurement*: before changing
any formula, we want a baseline for how the current model actually does,
and after each change we want the same numbers computed the same way so
improvements are provable, not vibes.

Leak-free by construction: every game is predicted from a FeatureContext
built as-of that game's own date, which (see pipeline/features/context.py)
serves only completed regular-season games strictly *before* that date.
Re-running June 1 in a backtest sees exactly what the live June 1 run saw.
"""
