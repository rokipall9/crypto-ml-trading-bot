"""BTC daily trend study v2 — implements SPEC_v2.md (exploratory).

Reuses v1's data, timing, costs, metrics and control. New here: signed
exposure (shorts pay costs, receive no funding), a vol-target no-trade band,
and a walk-forward lookback chooser.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

import backtest as B

LADDER = (7, 14, 28, 56, 112)
WF_GRID = (7, 14, 21, 28, 42, 56, 84, 112, 168)
WF_YEARS_BACK = 3
TARGET_VOL = 0.50
VOL_WINDOW = 30
BAND = 0.10


# ─── signals ─────────────────────────────────────────────────────────────

def tsmom(close: pd.Series, lookback: int) -> pd.Series:
    """+1 / -1 / 0 (not enough history) per day, using closes <= that day."""
    past = close.shift(lookback)
    r = close / past - 1
    return np.sign(r).where(past.notna(), 0.0)


def sig_multi(close: pd.Series) -> pd.Series:
    return sum((tsmom(close, L) > 0).astype(float) for L in LADDER) / len(LADDER)


def sig_multi_ls(close: pd.Series) -> pd.Series:
    return sum(tsmom(close, L) for L in LADDER) / len(LADDER)


def sig_multi_vt(close: pd.Series) -> pd.Series:
    base = sig_multi(close)
    vol = close.pct_change().rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std() \
        * np.sqrt(365)
    target = (base * np.minimum(1.0, TARGET_VOL / vol)).fillna(0.0).values
    held, out = 0.0, np.zeros(len(target))
    for i, t in enumerate(target):
        if abs(t - held) > BAND:
            held = t
        out[i] = held
    return pd.Series(out, index=close.index)


# ─── accounting with signed exposure ─────────────────────────────────────

def returns_signed(expo: np.ndarray, px_ret: np.ndarray,
                   funding_per_day: float) -> np.ndarray:
    turn = np.abs(np.diff(expo, prepend=0.0))
    return (expo * px_ret - turn * B.COST_PER_SIDE
            - np.maximum(expo, 0.0) * funding_per_day)


def strategy_returns(sig: pd.Series, px_ret: pd.Series,
                     funding_per_day: float) -> tuple[pd.Series, pd.Series]:
    expo = sig.shift(1).reindex(px_ret.index).fillna(0.0)
    net = pd.Series(returns_signed(expo.values, px_ret.values,
                                   funding_per_day), index=px_ret.index)
    return net, expo


def control_p(expo: np.ndarray, px_ret: np.ndarray, actual: float,
              funding_per_day: float) -> dict:
    rng = np.random.default_rng(B.CONTROL_SEED)
    n = len(expo)
    offsets = rng.integers(30, n - 30, size=B.CONTROL_DRAWS)
    shifted = np.array([B.sharpe(returns_signed(np.roll(expo, o), px_ret,
                                                funding_per_day))
                        for o in offsets])
    return {"p": round(float((shifted >= actual).mean()), 4),
            "shift_sharpe_median": round(float(np.median(shifted)), 3),
            "shift_sharpe_p95": round(float(np.percentile(shifted, 95)), 3)}


# ─── walk-forward lookback ───────────────────────────────────────────────

def sig_walk_forward(close: pd.Series, px_ret: pd.Series,
                     f: float) -> tuple[pd.Series, dict]:
    sigs = {L: (tsmom(close, L) > 0).astype(float) for L in WF_GRID}
    nets = {L: strategy_returns(s, px_ret, f)[0] for L, s in sigs.items()}
    out = pd.Series(0.0, index=close.index)
    chosen = {}
    first = close.index[0].year + WF_YEARS_BACK
    for year in range(first, close.index[-1].year + 1):
        lo = pd.Timestamp(f"{year - WF_YEARS_BACK}-01-01")
        hi = pd.Timestamp(f"{year - 1}-12-31")
        scores = {L: B.sharpe(n[(n.index >= lo) & (n.index <= hi)].values)
                  for L, n in nets.items()}
        best = max(scores, key=scores.get)
        chosen[str(year)] = {"lookback": best,
                             "trailing_sharpe": round(scores[best], 3)}
        mask = close.index.year == year
        out[mask] = sigs[best][mask]
    return out, chosen


# ─── run ─────────────────────────────────────────────────────────────────

def run() -> dict:
    spec_hash = hashlib.sha256((B.HERE / "SPEC_v2.md").read_bytes()).hexdigest()
    daily = pd.read_csv(B.DAILY, index_col="date", parse_dates=True)
    close, exec_px = daily["close"], daily["exec_px"]
    px_ret = B.interval_returns(exec_px)
    f = B.FUNDING_PER_DAY["primary"]

    wf_sig, wf_chosen = sig_walk_forward(close, px_ret, f)
    sigs = {"V1_tsmom_multi": sig_multi(close),
            "V2_tsmom_multi_vt": sig_multi_vt(close),
            "V3_tsmom_wf": wf_sig,
            "V4_tsmom_multi_ls": sig_multi_ls(close)}

    res = {"spec_sha256": spec_hash, "data_last_day": str(daily.index[-1].date()),
           "wf_chosen": wf_chosen, "periods": {}, "verdict": {},
           "sensitivity": {}, "small_account": {}, "diagnostic_lookback": {}}

    for pname in B.PERIODS:
        pr = B.period_slice(px_ret, pname)
        block = {"buy_hold_perp": B.metrics(B.buy_hold(pr, f))}
        for cname, sig in sigs.items():
            if cname == "V3_tsmom_wf" and pname == "reference":
                # only defined from 2018; score 2018-2020 and say so
                pass
            net, expo = strategy_returns(sig, px_ret, f)
            net, expo = B.period_slice(net, pname), B.period_slice(expo, pname)
            if cname == "V3_tsmom_wf" and pname == "reference":
                keep = net.index >= pd.Timestamp("2018-01-01")
                net, expo = net[keep], expo[keep]
            m = B.metrics(net, expo.abs())
            m["avg_signed_exposure"] = round(float(expo.mean()), 3)
            m["yearly"] = B.yearly(net)
            if pname == "test":
                m["control"] = control_p(expo.values, pr.values, m["sharpe"], f)
            block[cname] = m
        res["periods"][pname] = block

    t = res["periods"]["test"]
    bh = t["buy_hold_perp"]
    for cname in sigs:
        m = t[cname]
        checks = {
            "sharpe_gt_bh_perp": m["sharpe"] > bh["sharpe"],
            "dd_le_0.7x_bh_perp": abs(m["max_dd"]) <= 0.7 * abs(bh["max_dd"]),
            "control_p_lt_bonferroni": m["control"]["p"] < B.BONFERRONI_P,
            "positive_total": m["total_return"] > 0,
        }
        res["verdict"][cname] = {"checks": checks, "pass": all(checks.values())}

    for fname, fv in B.FUNDING_PER_DAY.items():
        pr = B.period_slice(px_ret, "test")
        row = {"buy_hold_perp": B.metrics(B.buy_hold(pr, fv))["sharpe"]}
        for cname, sig in sigs.items():
            net, _ = strategy_returns(sig, px_ret, fv)
            row[cname] = B.metrics(B.period_slice(net, "test"))["sharpe"]
        res["sensitivity"][f"test_sharpe_funding_{fname}"] = row

    for eq in (257.0, 2650.0):
        res["small_account"][f"${eq:,.0f}"] = {
            c: B.small_account(s, exec_px, B.PERIODS["test"][0], eq, f)
            for c, s in sigs.items() if c != "V4_tsmom_multi_ls"}

    for L in range(7, 181, 7):
        net, _ = strategy_returns((tsmom(close, L) > 0).astype(float), px_ret, f)
        res["diagnostic_lookback"][L] = {
            p: B.metrics(B.period_slice(net, p))["sharpe"] for p in B.PERIODS}
    return res


if __name__ == "__main__":
    out = run()
    (B.HERE / "results_v2.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
