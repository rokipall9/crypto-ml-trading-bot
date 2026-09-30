"""Tests for backtest_v2.py — each asserts the behaviour really occurred."""
import unittest

import numpy as np
import pandas as pd

import backtest as B
import backtest_v2 as V


def _walk(n, seed, start="2015-01-01"):
    rng = np.random.default_rng(seed)
    return pd.Series(20000 * np.exp(np.cumsum(rng.normal(0, 0.03, n))),
                     index=pd.date_range(start, periods=n, freq="D"))


class SignedAccounting(unittest.TestCase):
    def test_short_gains_on_drop_and_receives_no_funding(self):
        expo = np.array([-1.0, -1.0])
        r = V.returns_signed(expo, np.array([-0.02, 0.0]), 0.001)
        self.assertAlmostEqual(r[0], 0.02 - B.COST_PER_SIDE)   # entry + gain
        self.assertAlmostEqual(r[1], 0.0)                      # no funding

    def test_long_pays_funding_like_v1(self):
        expo = np.array([1.0, 1.0])
        r = V.returns_signed(expo, np.array([0.0, 0.0]), 0.001)
        self.assertAlmostEqual(r[1], -0.001)

    def test_flip_long_to_short_costs_two_sides(self):
        r = V.returns_signed(np.array([1.0, -1.0]), np.zeros(2), 0.0)
        self.assertAlmostEqual(r[1], -2 * B.COST_PER_SIDE)


class Signals(unittest.TestCase):
    def test_multi_is_share_of_positive_lookbacks(self):
        close = pd.Series(np.arange(1, 301, dtype=float),
                          index=pd.date_range("2020-01-01", periods=300))
        s = V.sig_multi(close)
        self.assertEqual(s.iloc[-1], 1.0)          # rising: all positive
        self.assertAlmostEqual(s.iloc[20], 2 / 5)  # only 7 and 14 defined
        self.assertEqual(V.sig_multi_ls(-close + 400).iloc[-1], -1.0)

    def test_band_holds_small_changes_and_moves_on_large(self):
        close = _walk(600, 3)
        s = V.sig_multi_vt(close)
        changes = s.diff().abs()
        moved = changes[changes > 0]
        self.assertGreater(len(moved), 5)                  # it does trade
        self.assertTrue((moved > V.BAND - 1e-12).all())    # never a tiny step


class WalkForward(unittest.TestCase):
    def test_choice_uses_only_prior_years(self):
        close = _walk(365 * 6, 4)
        px = close.copy()
        px_ret = B.interval_returns(px)
        _, chosen = V.sig_walk_forward(close, px_ret, 0.0003)
        # scramble 2020 onward; choices for 2018 and 2019 must not change
        alt = close.copy()
        cut = alt.index >= pd.Timestamp("2020-01-01")
        alt[cut] = alt[cut] * np.random.default_rng(9).uniform(0.6, 1.4, cut.sum())
        _, chosen_alt = V.sig_walk_forward(alt, B.interval_returns(alt), 0.0003)
        for y in ("2018", "2019"):
            self.assertEqual(chosen[y], chosen_alt[y])
        self.assertNotEqual([chosen[y]["lookback"] for y in chosen],
                            [chosen_alt[y]["lookback"] for y in chosen_alt])


if __name__ == "__main__":
    unittest.main()
