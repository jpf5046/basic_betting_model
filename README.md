# basic_betting_model

A daily, automated sports-betting pipeline: it ingests schedules and scores for
**WNBA, MLB, NBA, and NHL**, computes point-in-time features, predicts the winner
and final score of every game on today's slate, publishes a pick sheet, compares
the model to live market odds, and grades yesterday's picks once the finals are in.

```bash
python3 run_daily.py          # the whole morning loop, one command
```

## Requirements

- **Python 3.11+, standard library only** — there is nothing to `pip install`.
- No database, no services: all storage is flat CSVs under `data/` (see
  [Storage](#storage--databases) below).
- Internet access for the `fetch` stages (the leagues' public, key-less APIs).
- Optional: `export ODDS_API_KEY=...` ([The Odds API](https://the-odds-api.com))
  for the odds-fetch and edge-report stages — without it those stages skip
  themselves and the pipeline runs odds-blind.

## Quickstart

```bash
python3 -m unittest discover -s tests      # 129 offline tests, no network needed
python3 run_daily.py --sports WNBA,MLB     # daily run for the in-season sports
python3 run_daily.py --date 2026-07-03     # rebuild a missed day
```

Each run writes a human-readable report — stage log, yesterday's grade card,
today's pick sheet, model-vs-market edge — to `data/reports/daily_<date>.md`.

The GitHub Actions cron version of the daily run exists at
`.github/workflows/daily.yml` but is **intentionally disabled** (fully commented
out); its header explains how to enable it.

## What's built

| Piece | Where | What it does |
|---|---|---|
| Sport adapters | `pipeline/ingest/` | Schedule/score feeds → one shared games CSV schema, with raw-response caching and retry |
| Feature registry | `pipeline/features/` | Point-in-time features (scoring, common opponents, form, H2H, travel, …) — one file per feature, leak-proof by construction |
| Model + pick policy | `pipeline/models/` | `my_model` v1 (the [`my_model.md`](my_model.md) spec) → predictions, win prob, ML/TOTAL picks with confidence gates |
| Odds adapter | `pipeline/odds/` | The Odds API v4: consensus prices, game↔event matching, model-vs-market edge report |
| Grader | `pipeline/grading/` | WIN/LOSS/PUSH/VOID/PENDING at market prices, doubleheader- and OT-aware |
| Daily orchestrator | `pipeline/orchestrator.py` | Chains all of the above per sport; `run_daily.py` / `python3 -m pipeline daily` |

## Docs

| File | Read it for |
|---|---|
| [`HOWTO.md`](HOWTO.md) | Running everything that exists today, command by command, plus troubleshooting |
| [`PLAN.md`](PLAN.md) | The backend architecture, build order, and what's next (backfill, backtester, API, UI) |
| [`my_model.md`](my_model.md) | The full spec of the baseline statistical model |
| [`CLAUDE_CODE_PROMPT.md`](CLAUDE_CODE_PROMPT.md) / `researcher-console-design.html` | The future Researcher Console UI this backend will serve |

## Storage & databases

**Nothing in the pipeline connects to a database today, and none is needed to
run it locally.** Every artifact is a flat CSV under `data/` (the games CSVs,
feature frames, predictions, picks, grades, odds snapshots) — deliberately, per
[`PLAN.md`](PLAN.md) §1: files are reviewable, diffable, and enough until the
backtester needs real queries.

If you have a PostgreSQL instance and a `DATABASE_URL` environment variable set:
setting it changes nothing right now — no code reads it. The recorded plan
(PLAN §1) is that when the storage layer lands, it will **honor `DATABASE_URL`
when set (PostgreSQL) and fall back to a local SQLite file otherwise**, keeping
the zero-dependency local path working. The schema in PLAN §1 is already written
to make that a drop-in swap.
