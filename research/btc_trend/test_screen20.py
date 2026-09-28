"""Tests for screen20.py. Each asserts the behaviour actually happened."""
import unittest

import numpy as np
import pandas as pd

import backtest as B
import screen20 as S


def _hourly(n_days=420, seed=5, start="2016-01-01"):
    rng = np.random.default_rng(seed)
    n = n_days * 24
    close = 20000 * np.exp(np.cumsum(rng.normal(0, 0.006, n)))
    openp = np.r_[close[0], close[:-1]]
    wig = np.abs(rng.normal(0, 0.003, n)) * close
    idx = pd.date_range(start, periods=n, freq="h")
    return pd.DataFrame({"open": openp, "high": np.maximum(openp, close) + wig,
                         "low": np.minimum(openp, close) - wig, "close": close,
                         "n": 60, "exec_px": close * (1 + rng.normal(0, 1e-4, n))},
                        index=idx)


class Mapping(unittest.TestCase):
    def test_daily_signal_reaches_hours_after_its_close(self):
        days = pd.date_range("2020-01-01", periods=3, freq="D")
        sig = pd.Series([0.0, 1.0, 0.0], index=days)
        hours = pd.date_range("2020-01-01", periods=72, freq="h")
        dec = S.daily_to_hourly(sig, hours)
        self.assertEqual(dec[pd.Timestamp("2020-01-01 22:00")], 0.0)
        self.assertEqual(dec[pd.Timestamp("2020-01-02 22:00")], 0.0)  # D1 not done
        self.assertEqual(dec[pd.Timestamp("2020-01-02 23:00")], 1.0)  # D1 closes
        self.assertEqual(dec[pd.Timestamp("2020-01-03 10:00")], 1.0)
        self.assertEqual(dec[pd.Timestamp("2020-01-03 23:00")], 0.0)


class NoLookAhead(unittest.TestCase):
    def test_future_bars_do_not_change_past_decisions(self):
        h = _hourly()
        base = S.decisions(h)
        cut = h.index[len(h) * 3 // 4]
        alt = h.copy()
        after = alt.index > cut
        k = np.random.default_rng(7).uniform(0.7, 1.3, after.sum())
        for col in ("open", "high", "low", "close", "exec_px"):
            alt.loc[after, col] = alt.loc[after, col] * k
        changed = S.decisions(alt)
        traded = 0
        for name in base:
            self.assertTrue(base[name][:cut].equals(changed[name][:cut]), name)
            traded += int(base[name][:cut].diff().abs().sum() > 0)
        self.assertGreaterEqual(traded, 15)   # most strategies really trade


class TimeOfDay(unittest.TestCase):
    def test_evening_holds_intervals_20_to_23(self):
        h = _hourly(n_days=260)
        dec = S.decisions(h)["S1_evening"]
        expo = dec.shift(1).fillna(0.0)
        day = expo[pd.Timestamp("2016-06-01"):pd.Timestamp("2016-06-01 23:00")]
        self.assertEqual(list(day[day > 0].index.hour), [20, 21, 22, 23])

    def test_vol_breakout_is_flat_after_the_day(self):
        h = _hourly()
        dec = S.decisions(h)["D6_vol_breakout"]
        self.assertGreater(dec.sum(), 10)                   # it does fire
        self.assertEqual(dec[dec.index.hour == 23].sum(), 0.0)


class Accounting(unittest.TestCase):
    def test_hourly_costs_and_funding(self):
        expo = np.array([0.0, 1.0, 1.0, 0.0])
        net = S.hourly_net(expo, np.zeros(4))
        self.assertAlmostEqual(net[1], -B.COST_PER_SIDE - S.FUNDING_PER_HOUR)
        self.assertAlmostEqual(net[2], -S.FUNDING_PER_HOUR)
        self.assertAlmostEqual(net[3], -B.COST_PER_SIDE)

    def test_daily_compounding(self):
        net = np.array([0.01, 0.01, -0.02, 0.0])
        d = S.to_daily(net, np.array([0, 2]))
        self.assertAlmostEqual(d[0], 1.01 * 1.01 - 1)
        self.assertAlmostEqual(d[1], -0.02)


if __name__ == "__main__":
    unittest.main()
