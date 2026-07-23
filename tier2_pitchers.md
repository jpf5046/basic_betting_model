# Tier 2 Scope — Starting Pitchers (MLB)

Status: **scoped, not built.** Decisions locked (see "Decisions"). Foundation
in place: the backtest harness (`pipeline/backtest/`) and the distributional
model `my_model_v2` (Tier 1). This document is the implementation plan.

## Why this tier

Tier 1's backtest proved the moneyline is essentially a coin flip:

| Metric (MLB 2026, 1504 games) | v1 | v2 tuned | coin-flip baseline |
|---|---|---|---|
| ML accuracy | 51.3% | 51.3% | 50% |
| Brier | 0.2558 | 0.2536 | 0.25 |
| log-loss | 0.7074 | 0.7030 | 0.6931 |

Team-season averages carry **no** win signal — the model can't beat "always
say 50/50" on Brier. The starting pitcher is the single largest driver of a
game's run environment and is *not* reflected in a team's season averages on
any given day. This is the input most likely to create real edge.

**Tier success criterion:** the harness shows ML accuracy break above 51.3%
and Brier/log-loss drop below the coin-flip line (0.25 / 0.6931). If pitchers
don't move those numbers, this design isn't worth shipping — and the harness
will tell us, on identical games.

## Decisions

- **Integration:** starter adjustment is an **optional multiplier inside
  `my_model_v2`**, gated on a config weight `w_starter` and on data being
  present. No starter data (or `w_starter: 0`) => v2 output is byte-for-byte
  unchanged. No new model class; A/B is config-driven (`w_starter: 0` vs
  tuned), exactly like Tier 1.
- **Build order:** ingest + offline fixtures first (the data foundation),
  then context, feature, model, backtest, wire-up.

## Data model — two sidecar CSVs (the shared `Game` schema is untouched)

Pitchers stay out of `games_<season>.csv` to keep the cross-sport `Game`
record clean.

1. `data/mlb/game_starters_<season>.csv` — one row per game:
   `game_id, away_starter_id, home_starter_id`.
   Backfilled with *actual* starters for the backtest; written with
   *probable* starters by the daily fetch. Keyed by `game_id`, so
   doubleheaders (distinct `gamePk`) resolve automatically.

2. `data/mlb/pitcher_starts_<season>.csv` — one row per start:
   `pitcher_id, date, team_id, opponent_id, game_id, ip, r, er, k, bb, hr`.
   The pitcher analogue of the team game-log; what makes point-in-time
   starter strength computable without leakage.

Pitcher ids are statsapi `people` ids (a namespace separate from team ids) —
no change to `data/teams.csv`.

## Ingest (runs on a networked machine)

`statsapi.mlb.com` is blocked by the sandbox network policy (the existing
schedule fetch has the same constraint), so `fetch` runs where the other
ingest fetches run; everything downstream is developed and tested offline
against fixtures.

- **Probable / actual starters:** add `hydrate=probablePitcher` to the
  schedule request → `teams.away/home.probablePitcher.id` →
  `game_starters` CSV.
- **Pitcher lines:** for each starter id,
  `GET /api/v1/people/{id}/stats?stats=gameLog&group=pitching&season=YYYY`
  returns IP / ER / R / K / BB / HR per start **with dates** — ~150 calls
  (one per starter) vs. ~2,400 boxscore calls, and dated rows are exactly
  what point-in-time needs.
- Offline fixtures: `tests/fixtures/mlb_schedule_pp_sample.json`,
  `tests/fixtures/mlb_pitcher_gamelog_sample.json`, mirroring
  `tests/test_mlb_ingest.py`.

## Point-in-time context extension (leakage guardrail)

Add to `FeatureContext`:
- `starter_log(pitcher_id)` → that pitcher's starts **strictly before
  `as_of`** (the same one-line cutoff that already keeps `team_log` leak-free).
- `game_starters(game)` → `(away_starter_id, home_starter_id)` for the game
  being predicted.

Because the cutoff lives in one place, a pitcher's rate "as of date D" is
automatically built only from starts before D — no new leakage surface.

## Feature — `starter_strength` (robust to small samples)

Season ERA is too noisy early. Use Bayesian-shrunk runs-allowed-per-9,
pulled toward the league-average starter:

```
regressed_RA9 = (starter_R * 9 + PRIOR_IP * LG_RA9) / (starter_IP + PRIOR_IP)
starter_factor = clamp(regressed_RA9 / LG_RA9, LO, HI)   # ~1.0 avg, <1 ace
```

`PRIOR_IP` (~40), `LG_RA9`, and the clamp bounds are config params.
Shrinkage means call-ups, openers, and April degrade gracefully to ~1.0
instead of returning `None`.

Refinement path (not v1): FIP instead of RA9; opponent- and park-neutralize
the rate before forming the factor.

## Model integration — one more multiplier in v2

v2 already applies a park-factor multiplier after the blend. Starters slot in
the same way, but team `sapg` already contains that pitcher's past starts, so
apply a **partial** adjustment (avoids double-counting), weight-controlled:

```
mu_away *= (1 - w_starter) + w_starter * home_starter_factor  # home SP suppresses away
mu_home *= (1 - w_starter) + w_starter * away_starter_factor
```

`w_starter` starts ~0.5, tuned on the backtest. Data absent => factor 1.0 =>
v2 unchanged.

## Config additions (`configs/mlb_v2.json`, data not code)

`w_starter`, `prior_ip`, `lg_ra9`, `starter_factor_bounds`. All tunable
against the harness.

## Success criteria & tuning

A/B `w_starter: 0` vs tuned across the season. Track ML accuracy, Brier,
log-loss, calibration (do high-confidence bins finally hit their stated rate
once ace-vs-scrub games are distinguishable?), and totals hit-rate. Tune
`w_starter` / `prior_ip` to minimize log-loss.

## Risks & subtleties

- **Backtest optimism:** backtest uses *actual* starters; live uses
  *probables* that occasionally get scratched. Standard; small effect; noted
  so we don't over-read the backtest.
- **Openers / bullpen games:** a 1-IP "starter" distorts RA9 — shrinkage plus
  a min-IP floor mitigates.
- **Bullpen not modeled:** starters cover ~5 IP; relief is a later tier.
- **Double-counting** with team defense: handled by `w_starter < 1`.

## Phased tasks

1. **Ingest + fixtures** — `game_starters` (probable + actual) and
   `pitcher_starts` logs; offline tests. *(first)*
2. **Context** — `starter_log` / `game_starters`, point-in-time.
3. **Feature** — `starter_strength` shrinkage; unit tests.
4. **Model** — starter multiplier in v2 behind `w_starter`; config params.
5. **Backtest** — A/B vs v2; tune; report against the coin-flip baseline.
6. **Wire-up** — daily fetch writes probables; dashboard shows the pitching
   matchup + factors.

---

# Next steps once this PR is merged

Concrete actions, in order. Items marked **[you]** need a networked machine
(the sandbox blocks `statsapi.mlb.com`); the rest I can do here.

## 1. Start getting value from v2 now (optional, low effort)
The Tier 1 work is usable immediately — v2 fixes the OVER bias and makes the
totals marginally profitable. To run the daily pipeline with it:

```
python3 -m pipeline.models predict --sport MLB \
    --model my_model_v2 --config configs/mlb_v2.json
```

Decide whether to switch the scheduled daily run over to `my_model_v2`
(`.github/workflows/daily.yml` / `run_daily.py`) or keep running v1 alongside
it for a live head-to-head. My recommendation: run both for a couple of weeks
and compare graded results before cutting over.

## 2. Capture two real API samples **[you]** — unblocks Phase 1
This is the one thing that makes the pitcher ingest correct on the first pass
instead of the second. On a networked machine, save these two responses and
commit them as fixtures:

```
# one day of the schedule with probable pitchers
curl -s "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-06-01&hydrate=probablePitcher" \
    > tests/fixtures/mlb_schedule_pp_sample.json

# one pitcher's game log (pick any starter's people id, e.g. 592789)
curl -s "https://statsapi.mlb.com/api/v1/people/592789/stats?stats=gameLog&group=pitching&season=2026" \
    > tests/fixtures/mlb_pitcher_gamelog_sample.json
```

Commit them to a new branch (or hand them to me) and I'll build Phase 1
(ingest + parsers + offline tests) against the real shapes.

## 3. Backfill the season's starter data **[you]** — enables the Tier 2 backtest
Once the ingest exists, run it on a networked machine to produce
`data/mlb/game_starters_2026.csv` and `data/mlb/pitcher_starts_2026.csv` for
the full season. Without this the Tier 2 A/B can't run (the harness needs
historical starters to replay).

## 4. Then Phases 2–5 (I build, here)
Context extension → `starter_strength` feature → the `w_starter` multiplier in
v2 → harness A/B. The gate to ship: **ML accuracy above 51.3% and Brier/
log-loss below the coin-flip line (0.25 / 0.6931).** If pitchers don't clear
that bar, we stop and reconsider rather than ship noise.

## 5. Tune and re-tune **[you or me]**
`configs/mlb_v2.json` (and the new `w_starter` / `prior_ip`) are data, not
code — rerun `python3 -m pipeline.backtest` as the season progresses and
adjust. The park factors in particular are approximate first-pass values and
should be fit against results.

## Not in scope here (future tiers, noted so they aren't forgotten)
- **Bullpen** strength / recent usage — starters cover only ~5 IP.
- **Weather** (wind, temperature) and **lineup/injury** data.
- **FIP** instead of RA9, and park/opponent-neutralizing the starter rate.
- Replacing the hand-weighted blend with a fitted model (Elo / Poisson
  regression / gradient boosting) once there are enough real features.
