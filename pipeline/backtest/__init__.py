"""Backtest package (PLAN.md §6): replay history through any model config."""
from pipeline.backtest.metrics import bucket, compute_metrics  # noqa: F401
from pipeline.backtest.runner import (  # noqa: F401
    BacktestResult,
    make_run_id,
    persist,
    run_backtest,
)
