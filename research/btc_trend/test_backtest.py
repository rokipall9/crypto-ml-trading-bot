"""Tests for backtest.py. Every test asserts that the behaviour it checks
actually happened (a flip occurred, a position was held), so none can pass
vacuously."""
import unittest

import numpy as np
import pandas as pd

import backtest as B


def _series(vals, start="2020-01-01"):
    return pd.Series(np.asarray(vals, dtype=float),
                     index=pd.date_range(start, periods=len(vals), freq="D"))


class NoLookAhead(unittest.TestCase):
    def test_future_prices_do_not_change_past_exposure(self):
        rng = np.random.default_rng(1)
        close = _series(20000 * np.exp(np.cumsum(rng.normal(0, 0.03, 400))))
        cut = 300
        altered = close.copy()
        altered.iloc[cut + 1:] *= rng.uniform(0.5, 1.5, len(close) - cut - 1)
        base, alt = B.signals(close), B.signals(altered)
        for name in base:
            # exposure on interval k is sig[k-1]: intervals <= cut+1 fixed
            self.assertTrue(base[name].iloc[:cut + 1]
                            .equals(alt[name].iloc[:cut + 1]), name)
            # and the alteration really did change later signals somewhere
        self.assertFalse(all(base[n].iloc[cut + 1:].equals(alt[n].iloc[cut + 1:])
                             for n in base))
        # the series must actually trade, or the check above is empty
        self.assertGreater(base["C2_donchian20_10"].diff().abs().sum(), 2)

    def test_exposure_lags_signal_by_one_interval(self):
        sig = _series([0, 1, 1, 0, 0])
        px_ret = _series([0.0, 0.0, 0.0, 0.0, 0.0])
        _, expo = B.strategy_returns(sig, px_ret, 0.0)
        self.assertEqual(expo.tolist(), [0, 0, 1, 1, 0])


class Accounting(unittest.TestCase):
    def test_flip_costs_exactly_one_side(self):
        sig = _series([1, 1, 1])
        px_ret = _series([0.0, 0.0, 0.0])
        net, expo = B.strategy_returns(sig, px_ret, 0.0)
        self.assertEqual(expo.iloc[1], 1.0)                 # really entered
        self.assertAlmostEqual(net.iloc[1], -B.COST_PER_SIDE)
        self.assertAlmostEqual(net.iloc[2], 0.0)

    def test_funding_charged_only_while_exposed(self):
        sig = _series([0, 1, 1, 0])
        px_ret = _series([0.0] * 4)
        net, expo = B.strategy_returns(sig, px_ret, 0.001)
        self.assertEqual(expo.sum(), 2.0)
        self.assertAlmostEqual(net.iloc[0], 0.0)
        self.assertAlmostEqual(net.iloc[1], 0.0)             # flat, no funding
        self.assertAlmostEqual(net.iloc[2], -B.COST_PER_SIDE - 0.001)  # entry
        self.assertAlmostEqual(net.iloc[3], -0.001)          # funding only

    def test_gain_scaled_by_exposure(self):
        sig = _series([1 / 3, 1 / 3, 1 / 3])
        px_ret = _series([0.0, 0.03, 0.03])
        net, _ = B.strategy_returns(sig, px_ret, 0.0)
        self.assertAlmostEqual(net.iloc[2], 0.01)


class Donchian(unittest.TestCase):
    def test_enters_on_breakout_and_exits_on_breakdown(self):
        vals = [100] * 25 + [101] + [101.5] * 3 + [99]
        s = B.sig_donchian20_10(_series(vals))
        self.assertEqual(s.iloc[24], 0.0)
        self.assertEqual(s.iloc[25], 1.0)       # close > prior-20 max
        self.assertEqual(s.iloc[28], 1.0)
        self.assertEqual(s.iloc[29], 0.0)       # close < prior-10 min


class Control(unittest.TestCase):
    def test_circular_shift_preserves_exposure(self):
        rng = np.random.default_rng(2)
        expo = (rng.random(500) > 0.6).astype(float)
        for o in (31, 200, 450):
            sh = np.roll(expo, o)
            self.assertEqual(sh.sum(), expo.sum())
            self.assertLessEqual(abs(np.abs(np.diff(sh)).sum()
                                     - np.abs(np.diff(expo)).sum()), 2)
            self.assertFalse(np.array_equal(sh, expo))


class SmallAccount(unittest.TestCase):
    def test_floors_to_lot_and_skips_below_lot(self):
        idx = pd.date_range("2021-01-01", periods=4, freq="D")
        sig = pd.Series([1.0, 1.0, 1.0, 1.0], index=idx)
        px = pd.Series([50000.0] * 4, index=idx)
        r = B.small_account(sig, px, "2021-01-01", 257.0, 0.0)
        self.assertEqual(r["orders"], 1)                 # 0.005 BTC bought
        self.assertAlmostEqual(r["end_equity"],
                               257 - 0.005 * 50000 * B.COST_PER_SIDE, 2)
        r2 = B.small_account(sig, px * 10, "2021-01-01", 257.0, 0.0)
        self.assertEqual(r2["orders"], 0)
        self.assertEqual(r2["entries_skipped_below_lot"], 1)


if __name__ == "__main__":
    unittest.main()
