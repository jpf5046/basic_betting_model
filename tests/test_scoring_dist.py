"""Offline tests for the v2 distributional core (pipeline/models/scoring_dist.py).

Pure math, no data or network.
From the repo root:  python3 -m unittest tests.test_scoring_dist -v
"""
import unittest

from pipeline.models.scoring_dist import nb_pmf, outcome_probabilities


class TestNbPmf(unittest.TestCase):
    def test_pmf_sums_to_one(self):
        for mu in (0.5, 3.0, 4.5, 9.0):
            for r in (1.0, 4.0, 50.0):
                self.assertAlmostEqual(sum(nb_pmf(mu, r)), 1.0, places=9)

    def test_mean_recovers_mu(self):
        # E[X] under the returned PMF should be ~mu (tail folding aside).
        pmf = nb_pmf(4.5, 4.0)
        mean = sum(k * p for k, p in enumerate(pmf))
        self.assertAlmostEqual(mean, 4.5, delta=0.05)

    def test_lower_r_more_variance(self):
        def var(mu, r):
            pmf = nb_pmf(mu, r)
            m = sum(k * p for k, p in enumerate(pmf))
            return sum((k - m) ** 2 * p for k, p in enumerate(pmf))
        # Smaller dispersion r => fatter tails => larger variance.
        self.assertGreater(var(4.5, 1.0), var(4.5, 10.0))

    def test_zero_mu_is_a_point_mass_at_zero(self):
        pmf = nb_pmf(0.0, 4.0)
        self.assertEqual(pmf[0], 1.0)
        self.assertEqual(sum(pmf[1:]), 0.0)


class TestOutcomeProbabilities(unittest.TestCase):
    def test_win_probs_and_tie_sum_to_one(self):
        p = outcome_probabilities(4.6, 4.5, 4.0)
        self.assertAlmostEqual(p["p_away_win"] + p["p_home_win"] + p["p_tie"], 1.0, places=6)

    def test_higher_mean_team_favored(self):
        p = outcome_probabilities(6.0, 4.0, 4.0)
        self.assertGreater(p["p_away_win"], p["p_home_win"])

    def test_equal_means_are_symmetric(self):
        p = outcome_probabilities(4.5, 4.5, 4.0)
        self.assertAlmostEqual(p["p_away_win"], p["p_home_win"], places=6)

    def test_tiny_edge_stays_near_coinflip(self):
        # The whole point of v2: a 0.1-run edge must NOT become 60% like the
        # v1 logistic did — decided win prob should sit just above 0.5.
        p = outcome_probabilities(4.55, 4.45, 2.0)
        decided = p["p_away_win"] / (p["p_away_win"] + p["p_home_win"])
        self.assertGreater(decided, 0.50)
        self.assertLess(decided, 0.53)

    def test_over_under_push_sum_to_one(self):
        p = outcome_probabilities(4.6, 4.5, 4.0, line=9.0)
        self.assertAlmostEqual(p["p_over"] + p["p_under"] + p["p_push"], 1.0, places=6)

    def test_line_far_below_total_is_almost_certain_over(self):
        p = outcome_probabilities(5.0, 5.0, 4.0, line=2.0)
        self.assertGreater(p["p_over"], 0.95)


if __name__ == "__main__":
    unittest.main()
