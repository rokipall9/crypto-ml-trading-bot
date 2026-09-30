"""Tests for screen20_full.py — each asserts the behaviour really occurred."""
import unittest

import numpy as np

import screen20 as S
import screen20_full as F
from test_screen20 import _hourly


class Reproduction(unittest.TestCase):
    def test_defaults_reproduce_screen20_for_all_twenty(self):
        h = _hourly(n_days=420)
        ref = S.decisions(h)
        self.assertEqual(set(ref), set(F.DEFAULTS))
        active = 0
        for k, p in F.DEFAULTS.items():
            got = F.build(k, h, p)
            self.assertTrue(np.allclose(got.values, ref[k].values), k)
            active += int(got.abs().sum() > 0)
        self.assertEqual(active, 20)            # every strategy takes positions

    def test_neighbours_change_the_strategy(self):
        h = _hourly(n_days=420)
        changed = 0
        for k, nbs in F.NEIGHBORS.items():
            base = F.build(k, h, F.DEFAULTS[k])
            for nb in nbs:
                changed += int(not np.allclose(F.build(k, h, nb).values, base.values))
        self.assertEqual(changed, sum(len(v) for v in F.NEIGHBORS.values()))


class Stats(unittest.TestCase):
    def test_benjamini_hochberg(self):
        p = {"a": 0.001, "b": 0.02, "c": 0.03, "d": 0.5}
        sig = F.benjamini_hochberg(p, 0.10)
        # thresholds 0.025, 0.05, 0.075, 0.1 -> a, b, c pass; d fails
        self.assertEqual(sig, {"a": True, "b": True, "c": True, "d": False})
        self.assertEqual(F.benjamini_hochberg({"a": 0.2, "b": 0.3}, 0.10),
                         {"a": False, "b": False})

    def test_bootstrap_ci_brackets_sharpe_and_is_deterministic(self):
        rng = np.random.default_rng(3)
        daily = rng.normal(0.002, 0.03, 1500)
        lo, hi = F.block_bootstrap_ci(daily)
        self.assertLess(lo, hi)
        self.assertTrue(lo < F.B.sharpe(daily) < hi)
        self.assertEqual([lo, hi], F.block_bootstrap_ci(daily))

    def test_delay_shifts_exposure_one_more_hour(self):
        import pandas as pd
        idx = pd.date_range("2021-01-01", periods=6, freq="h")
        dec = pd.Series([1.0, 0, 0, 0, 0, 0], index=idx)
        r = pd.Series([0.0, 0.01, 0.02, 0, 0, 0], index=idx)
        m1 = F.evaluate(dec, r, ("2021-01-01", None), cost=0.0, funding_h=0.0)
        m2 = F.evaluate(dec, r, ("2021-01-01", None), cost=0.0, funding_h=0.0, delay=2)
        self.assertAlmostEqual(m1["total_return"], 0.01)
        self.assertAlmostEqual(m2["total_return"], 0.02)


if __name__ == "__main__":
    unittest.main()
