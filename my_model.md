# My Model — How the Picks Are Decided

"My Model" is the pipeline's in-house statistical predictor. Unlike the LLM
predictors (Claude, OpenAI, Google), it uses no language model and no betting
odds — it is a deterministic math model built entirely from team performance
statistics. Given the same inputs, it always produces the same pick.

This document is the full specification: every data point the model consumes,
every formula it applies, and every threshold it uses to turn a prediction
into a pick. Someone with this document and a source of team statistics could
recreate the model exactly.

The same model runs for **MLB, NBA, and NHL** — identical structure, with a
small set of sport-specific constants (listed in the tables below). The unit
of scoring is runs (MLB), points (NBA), or goals (NHL).

---

## 1. Data points required

The model needs the following inputs for **each of the two teams** in a game.
No odds, injuries, weather, or player-level data are used — teams only.

### Season summary (from league standings)

| Data point | Description |
|---|---|
| Wins / Losses / Games played | Current season record |
| Win percentage | Wins ÷ games played |
| Scoring per game | Runs / points / goals scored per game, season average |
| Scoring allowed per game | Runs / points / goals allowed per game, season average |
| Scoring differential | Total scored minus total allowed (context only) |
| Home record / Road record | Season splits (context only) |
| Last-10 record | Record over the team's last 10 games (NHL: W-L-OTL) |
| Current streak | Win/loss streak (context only) |
| League rank | Rank by win percentage across the league (context only) |

### Game-by-game season log (full schedule of completed games)

For **every completed game** each team has played this season:

| Data point | Description |
|---|---|
| Date | Game date |
| Opponent | Who they played |
| Home or away | Venue for that game |
| Score | Runs/points/goals scored and allowed |
| Result | Win or loss |

The game logs are what power the *common opponents* and *head-to-head*
methods. The reference implementation pulls these from the official league
stats APIs (MLB Stats API, NBA Stats API, NHL API); any equivalent source
works.

### The matchup itself

| Data point | Description |
|---|---|
| Away team, home team | Who is playing whom, and who hosts |

---

## 2. Predicting the score — five weighted methods

The core of the model is a predicted final score for each team. Five
independent methods each produce a (away score, home score) estimate, and the
estimates are blended with fixed weights:

| Method | Weight |
|---|---|
| 1. Season averages | 30% |
| 2. Common opponents | 35% |
| 3. Recent form (last 10) | 20% |
| 4. Home-field adjustment | 10% |
| 5. Head-to-head | 5% |

If a method has no data (no common opponents, or no head-to-head meetings
yet), its weight is dropped and the remaining weights are **renormalized to
sum to 1** — the other methods absorb its share proportionally.

Throughout, let:

- `RPG_A`, `RAPG_A` = away team's scoring per game and scoring allowed per game
- `RPG_H`, `RAPG_H` = same for the home team

### Method 1 — Season averages (weight 0.30)

Each team's expected score is the midpoint of its own offense and the
opponent's defense:

```
base_away = (RPG_A + RAPG_H) / 2
base_home = (RPG_H + RAPG_A) / 2
```

This `base` pair is also the fallback that methods 2 and 5 return when they
have no data, and the starting point that methods 3 and 4 adjust.

### Method 2 — Common opponents (weight 0.35)

The highest-weighted method. From the two game logs, find every opponent
**both** teams have played this season (excluding each other). For each
common opponent, compute each team's average scored and average allowed in
those games. Then combine across all common opponents, **weighting each
opponent by the number of games played against it**:

```
wRF_A = weighted avg of away team's scoring vs common opponents
wRA_A = weighted avg of away team's scoring allowed vs common opponents
wRF_H, wRA_H = same for home team

common_away = (wRF_A + wRA_H) / 2
common_home = (wRF_H + wRA_A) / 2
```

If there are no common opponents, this method returns `base_away, base_home`
and its weight is set to 0 (then renormalized away).

### Method 3 — Recent form (weight 0.20)

Convert each team's last-10 record into a form multiplier, then scale the
season baseline by the **ratio** of the two multipliers:

```
L10_pct = last-10 wins / 10          (0.5 if no last-10 data)
form    = form_base + L10_pct × form_range
form_away_score = base_away × form_A / form_H
form_home_score = base_home × form_H / form_A
```

| Sport | form_base | form_range | Multiplier range | L10_pct detail |
|---|---|---|---|---|
| MLB | 0.80 | 0.40 | 0.80 – 1.20 | wins / 10 |
| NBA | 0.85 | 0.30 | 0.85 – 1.15 | wins / 10 |
| NHL | 0.80 | 0.40 | 0.80 – 1.20 | points-based: (2×W + OTL) / 20 |

So a 10-0 team gets the max multiplier, a 0-10 team the min, and a 5-5 team
sits at 1.0. A hot team's score estimate is inflated relative to a cold
opponent's, and vice versa.

### Method 4 — Home-field adjustment (weight 0.10)

Add a fixed home advantage to the home team's baseline and subtract half of
it from the away team's:

```
home_adj_home = base_home + HOME_ADV
home_adj_away = base_away − HOME_ADV / 2
```

| Sport | HOME_ADV |
|---|---|
| MLB | 0.35 runs |
| NBA | 3.0 points |
| NHL | 0.25 goals |

### Method 5 — Head-to-head (weight 0.05)

If the two teams have already met this season, use the straight average of
the away team's scored and allowed across those meetings:

```
h2h_away = mean(away team's score in H2H games)
h2h_home = mean(away team's opponent score in H2H games)
```

If they haven't met, this method returns the baseline and its weight is set
to 0 (then renormalized away).

### Blending

```
pred_away = Σ (method_away × normalized_weight)
pred_home = Σ (method_home × normalized_weight)
```

These exact (decimal) predicted scores drive everything downstream:

```
pred_total  = pred_away + pred_home
pred_spread = pred_away − pred_home        (positive = away team favored)
```

For display, scores are rounded to whole numbers; if rounding produces a tie,
one unit is added to the side with the higher win probability (MLB and NHL —
NBA skips the tie-break since basketball ties are effectively impossible at
its scale).

---

## 3. Win probability

The predicted score margin is converted to a win probability with a logistic
(S-curve) function:

```
P(away wins) = 1 / (1 + e^(−(pred_away − pred_home) × k))
```

where `k` controls how quickly a margin translates into confidence — tuned
per sport to reflect how decisive one unit of scoring is:

| Sport | k | Example: 1-unit predicted margin ⇒ |
|---|---|---|
| MLB | 0.40 | ~60% win probability |
| NBA | 0.15 | ~54% win probability |
| NHL | 0.70 | ~67% win probability |

The predicted winner is whichever side has probability > 0.5, and the
reported `pred_win_prob` is that winner's probability (always ≥ 0.5).
Probabilities are rounded to two decimals.

---

## 4. Turning predictions into picks

My Model publishes up to **two picks per game**: a moneyline pick and a
totals (over/under) pick. It does not publish spread picks (the predicted
spread is produced, but is only consumed by the downstream ensemble model).

### Moneyline pick

Gated on the win probability from step 3:

| Win probability | Pick | Confidence |
|---|---|---|
| ≥ 0.60 | Predicted winner | STRONG |
| 0.55 – 0.599 | Predicted winner | LEAN |
| < 0.55 | TOSS-UP — **no pick published** | — |

### Totals pick

Gated on the predicted combined score against **fixed, sport-specific
thresholds** (not the sportsbook's line — the model never sees odds):

| Sport | OVER if pred_total ≥ | UNDER if pred_total ≤ | Otherwise |
|---|---|---|---|
| MLB | 9.2 | 7.8 | PASS — no pick |
| NBA | 225 | 215 | PASS — no pick |
| NHL | 6.3 | 5.7 | PASS — no pick |

The dead zone between the thresholds means the model only bets totals when
its prediction is meaningfully far from a typical game's scoring.

### What gets published

When the daily pick sheet is assembled, each game contributes:

- **Moneyline** row with the team pick and STRONG/LEAN confidence — only if
  the lean is not TOSS-UP.
- **Total** row with OVER/UNDER — only if the lean is not PASS (totals picks
  carry no confidence label).

TOSS-UP, PASS, and games where the model couldn't run (missing standings,
unmapped team, failed schedule fetch) produce no pick at all — sitting out
is a deliberate output of the model.

---

## 5. Prediction confidence label

Separately from the per-pick confidence above, each game gets an overall
data-quality confidence (`pred_confidence`) scored by how much evidence the
model had to work with:

| Evidence | Points |
|---|---|
| Many common opponents (MLB ≥15 / NBA ≥15 / NHL ≥10) | +3 |
| Moderate common opponents (MLB ≥8 / NBA ≥8 / NHL ≥5) | +2 |
| Few common opponents (MLB ≥3 / NBA ≥4 / NHL ≥2) | +1 |
| Any head-to-head games played | +1 |
| Deep into season (both teams: MLB ≥80 GP / NBA ≥50 / NHL ≥50) | +2 |
| Mid-season (both teams: MLB ≥40 GP / NBA ≥30 / NHL ≥30) | +1 |

(Only the highest matching common-opponent tier and the highest matching
games-played tier count.)

| Total score | Label |
|---|---|
| ≥ 5 | HIGH |
| 3 – 4 | MEDIUM |
| < 3 | LOW |

This label describes how trustworthy the underlying prediction is; the
moneyline pick's STRONG/LEAN label describes how decisive the prediction is.

---

## 6. Recreating the model — checklist

1. Gather the **season summary** and **full game log** for both teams
   (Section 1).
2. Compute the five method estimates (Section 2), dropping and renormalizing
   any method with no data.
3. Blend with weights 30 / 35 / 20 / 10 / 5 to get exact predicted scores,
   total, and spread.
4. Convert the margin to a win probability with the logistic curve and the
   sport's `k` (Section 3).
5. Apply the moneyline gate (0.60 / 0.55) and the sport's fixed totals
   thresholds (Section 4). Publish only non-TOSS-UP, non-PASS leans.
6. Score the evidence for the HIGH/MEDIUM/LOW confidence label (Section 5).

Reference implementations live in `mlb_insights/mlb_insights.py`,
`nba_insights/nba_insights.py`, and `nhl_insights/nhl_insights.py`; the picks
are collected under the predictor name "My Model" in
`review_picks/review_picks.py`.
