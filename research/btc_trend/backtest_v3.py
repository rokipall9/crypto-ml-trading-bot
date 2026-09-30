"""BTC daily trend study v3 — implements SPEC_v3.md: the unseen 2014-2016
period for C3_tsmom28 and V1_tsmom_multi, with v1's gate and control."""
from __future__ import annotations

import hashlib
import json

import pandas as pd

import backtest as B
import backtest_v2 as V
import fetch_data as F

EARLY = ("2014-01-01", "2016-12-31")
DAILY_2013 = B.HERE / "data" / "btc_daily_2013.csv"
BONFERRONI_P = 0.05 / 2


def load() -> pd.DataFrame:
    if not DAILY_2013.exists():
        F.build_daily(pd.read_pickle(B.HERE / "data" / "btc_1m.pkl"),
                      start="2013-01-01").to_csv(DAILY_2013)
    return pd.read_csv(DAILY_2013, index_col="date", parse_dates=True)


def run() -> dict:
    daily = load()
    close, exec_px = daily["close"], daily["exec_px"]
    px_ret = B.interval_returns(exec_px)
    f = B.FUNDING_PER_DAY["primary"]
    sl = lambda s: s[(s.index >= pd.Timestamp(EARLY[0]))
                     & (s.index <= pd.Timestamp(EARLY[1]))]
    pr = sl(px_ret)
    bh = B.metrics(B.buy_hold(pr, f))
    res = {"spec_sha256": hashlib.sha256((B.HERE / "SPEC_v3.md").read_bytes())
           .hexdigest(), "period": EARLY, "daily_rows": len(daily),
           "buy_hold_perp": bh, "buy_hold_perp_yearly": B.yearly(B.buy_hold(pr, f)),
           "candidates": {}}
    sigs = {"C3_tsmom28": B.sig_tsmom28(close), "V1_tsmom_multi": V.sig_multi(close)}
    for name, sig in sigs.items():
        net, expo = B.strategy_returns(sig, px_ret, f)
        net, expo = sl(net), sl(expo)
        m = B.metrics(net, expo)
        m["yearly"] = B.yearly(net)
        m["control"] = B.control_p(expo.values, pr.values, m["sharpe"], f)
        checks = {"sharpe_gt_bh_perp": m["sharpe"] > bh["sharpe"],
                  "dd_le_0.7x_bh_perp": abs(m["max_dd"]) <= 0.7 * abs(bh["max_dd"]),
                  "control_p_lt_bonferroni": m["control"]["p"] < BONFERRONI_P,
                  "positive_total": m["total_return"] > 0}
        m["checks"], m["pass"] = checks, all(checks.values())
        res["candidates"][name] = m
    return res


if __name__ == "__main__":
    out = run()
    (B.HERE / "results_v3.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
