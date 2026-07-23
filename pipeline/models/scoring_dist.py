#!/usr/bin/env python3
"""Put a run distribution around a predicted mean score, instead of trusting
a single logistic curve to turn a tiny margin into a probability.

The v1 model predicts, say, away 4.6 vs home 4.5 and asks a logistic with a
hand-tuned `k` how confident that 0.1-run edge should make us. The backtest
showed the answer is *over*confident and the totals thresholds sit in a dead
zone. This module fixes both honestly: model each team's runs as a Negative
Binomial around its predicted mean, then read probabilities straight off the
implied outcome distribution.

Negative Binomial (not Poisson) because real baseball scoring is
over-dispersed — variance runs roughly twice the mean. NB variance is
`mu + mu^2 / r`; the dispersion parameter `r` is a tunable per-sport
constant (`nb_dispersion` in the params dict). As `r -> inf` this collapses
to Poisson.

Everything is pure-Python (math.lgamma) so there's no numpy/scipy dependency
and the model stays deterministic.
"""

from __future__ import annotations

import math

# Runs/points/goals are counted 0..KMAX; beyond this the PMF mass is
# negligible for any realistic mean (MLB ~4-5, NBA scaled far below KMAX
# because basketball is handled per-possession-free here — v2 is MLB/NHL
# first). KMAX is generous for blowouts.
KMAX = 30


def nb_pmf(mu: float, r: float, kmax: int = KMAX) -> list[float]:
    """Negative-Binomial PMF over 0..kmax for mean `mu`, dispersion `r`.

    Parameterized by mean: p = r / (r + mu), and
    P(k) = C(k+r-1, k) * p^r * (1-p)^k, computed in log space for stability.
    The tail beyond kmax is folded into the last bucket so the vector sums
    to 1 (keeps downstream probabilities exact)."""
    if mu <= 0:
        out = [0.0] * (kmax + 1)
        out[0] = 1.0
        return out
    p = r / (r + mu)
    log_p = math.log(p)
    log_1mp = math.log(1.0 - p)
    pmf = []
    for k in range(kmax + 1):
        log_pk = (math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                  + r * log_p + k * log_1mp)
        pmf.append(math.exp(log_pk))
    total = sum(pmf)
    pmf[-1] += max(0.0, 1.0 - total)  # fold the tail into the top bucket
    return pmf


def outcome_probabilities(mu_away: float, mu_home: float, r: float,
                          line: float | None = None,
                          kmax: int = KMAX) -> dict:
    """From two run means, the full set of betting probabilities.

    Returns p_away_win / p_home_win / p_tie (ties split out, not forced to a
    winner), the expected total, and — if `line` is given — p_over / p_under
    / p_push against that total line.
    """
    pa = nb_pmf(mu_away, r, kmax)
    ph = nb_pmf(mu_home, r, kmax)

    p_away = p_home = p_tie = 0.0
    for a in range(kmax + 1):
        pa_a = pa[a]
        if pa_a == 0.0:
            continue
        for h in range(kmax + 1):
            joint = pa_a * ph[h]
            if a > h:
                p_away += joint
            elif h > a:
                p_home += joint
            else:
                p_tie += joint

    result = {
        "p_away_win": p_away,
        "p_home_win": p_home,
        "p_tie": p_tie,
        "exp_total": mu_away + mu_home,
    }

    if line is not None:
        # Total distribution via convolution of the two PMFs.
        total_pmf = [0.0] * (2 * kmax + 1)
        for a in range(kmax + 1):
            pa_a = pa[a]
            if pa_a == 0.0:
                continue
            for h in range(kmax + 1):
                total_pmf[a + h] += pa_a * ph[h]
        p_over = sum(total_pmf[t] for t in range(2 * kmax + 1) if t > line)
        p_under = sum(total_pmf[t] for t in range(2 * kmax + 1) if t < line)
        result.update({
            "p_over": p_over,
            "p_under": p_under,
            "p_push": max(0.0, 1.0 - p_over - p_under),
        })

    return result
