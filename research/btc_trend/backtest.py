"""BTC daily trend study — implements SPEC.md v1 exactly.

Timing (no look-ahead):
  sig[D]      uses closes up to and including day D
  exposure    held over interval k (exec_px[k] -> exec_px[k+1]) is sig[k-1]
  exec_px[k]  close of the 00:04 UTC minute of day k (≈00:05 fill)

Run:  python3 fetch_data.py && python3 backtest.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
SPEC = HERE / "SPEC.md"
DAILY = HERE / "data" / "btc_daily.csv"

COST_PER_SIDE = 0.00055 + 0.00050            # taker fee + slippage/basis
FUNDING_PER_DAY = {"primary": 0.0003,        # 0.01 %/8h
                   "zero": 0.0,
                   "high": 0.0009}           # 0.03 %/8h
PERIODS = {"reference": ("2017-01-01", "2020-12-31"),
           "test": ("2021-01-01", None)}
LOT_BTC = 0.001
CONTROL_DRAWS = 1000
CONTROL_SEED = 20260928
BONFERRONI_P = 0.05 / 4


# ─── signals: each returns a 0..1 exposure per day, using data <= that day ───

def sig_sma200(close: pd.Series) -> pd.Series:
    sma = close.rolling(200, min_periods=200).mean()
    return (close > sma).astype(float).where(sma.notna(), 0.0)


def sig_donchian20_10(close: pd.Series) -> pd.Series:
    hi = close.shift(1).rolling(20, min_periods=20).max()
    lo = close.shift(1).rolling(10, min_periods=10).min()
    c, h, l = close.values, hi.values, lo.values
    out = np.zeros(len(c))
    pos = 0.0
    for i in range(len(c)):
        if pos == 0.0 and not np.isnan(h[i]) and c[i] > h[i]:
            pos = 1.0
        elif pos == 1.0 and not np.isnan(l[i]) and c[i] < l[i]:
            pos = 0.0
        out[i] = pos
    return pd.Series(out, index=close.index)


def sig_tsmom28(close: pd.Series) -> pd.Series:
    past = close.shift(28)
    return (close / past - 1 > 0).astype(float).where(past.notna(), 0.0)


def signals(close: pd.Series) -> dict[str, pd.Series]:
    s = {"C1_sma200": sig_sma200(close),
         "C2_donchian20_10": sig_donchian20_10(close),
         "C3_tsmom28": sig_tsmom28(close)}
    s["C4_ensemble"] = (s["C1_sma200"] + s["C2_donchian20_10"]
                        + s["C3_tsmom28"]) / 3.0
    return s


# ─── accounting ───────────────────────────────────────────────────────────

def interval_returns(exec_px: pd.Series) -> pd.Series:
    """Return of interval k = exec_px[k+1] / exec_px[k] - 1, indexed by k."""
    return (exec_px.shift(-1) / exec_px - 1).dropna()


def strategy_returns(sig: pd.Series, px_ret: pd.Series,
                     funding_per_day: float) -> tuple[pd.Series, pd.Series]:
    """Exposure on interval k is sig[k-1]; costs on changes, funding on
    exposure. Returns (net daily returns, exposure), both indexed like px_ret."""
    expo = sig.shift(1).reindex(px_ret.index).fillna(0.0)
    turn = expo.diff().abs()
    turn.iloc[0] = expo.iloc[0]
    net = expo * px_ret - turn * COST_PER_SIDE - expo * funding_per_day
    return net, expo


def returns_from_exposure(expo: np.ndarray, px_ret: np.ndarray,
                          funding_per_day: float) -> np.ndarray:
    turn = np.abs(np.diff(expo, prepend=0.0))
    return expo * px_ret - turn * COST_PER_SIDE - expo * funding_per_day


def buy_hold(px_ret: pd.Series, funding_per_day: float) -> pd.Series:
    r = px_ret - funding_per_day
    r.iloc[0] -= COST_PER_SIDE
    return r


# ─── metrics ──────────────────────────────────────────────────────────────

def sharpe(r: np.ndarray) -> float:
    sd = np.std(r, ddof=1)
    return float(np.mean(r) / sd * np.sqrt(365)) if sd > 0 else 0.0


def max_drawdown(r: np.ndarray) -> float:
    eq = np.cumprod(1 + r)
    return float((eq / np.maximum.accumulate(eq) - 1).min())


def round_trips(expo: pd.Series, r: pd.Series) -> list[float]:
    """Compound return of each in-market episode (exposure > 0), incl. costs
    and funding. The exit cost lands on the first flat day, so it is added."""
    trips, cur, inside = [], 1.0, False
    for e, x in zip(expo.values, r.values):
        if e > 0:
            cur *= 1 + x
            inside = True
        elif inside:
            cur *= 1 + x            # exit-day cost
            trips.append(cur - 1)
            cur, inside = 1.0, False
    if inside:
        trips.append(cur - 1)       # still open at period end
    return trips


def metrics(r: pd.Series, expo: pd.Series | None = None) -> dict:
    a = r.values
    n = len(a)
    total = float(np.prod(1 + a) - 1)
    out = {
        "days": n,
        "total_return": round(total, 4),
        "cagr": round(float((1 + total) ** (365 / n) - 1), 4),
        "vol": round(float(np.std(a, ddof=1) * np.sqrt(365)), 4),
        "sharpe": round(sharpe(a), 3),
        "max_dd": round(max_drawdown(a), 4),
    }
    if expo is not None:
        trips = round_trips(expo, r)
        out.update({
            "time_in_market": round(float((expo > 0).mean()), 3),
            "avg_exposure": round(float(expo.mean()), 3),
            "round_trips": len(trips),
            "trip_win_rate": round(float(np.mean([t > 0 for t in trips])), 3)
            if trips else None,
            "avg_win": round(float(np.mean([t for t in trips if t > 0])), 4)
            if any(t > 0 for t in trips) else None,
            "avg_loss": round(float(np.mean([t for t in trips if t <= 0])), 4)
            if any(t <= 0 for t in trips) else None,
        })
    return out


def yearly(r: pd.Series) -> dict:
    return {str(y): round(float(np.prod(1 + g.values) - 1), 4)
            for y, g in r.groupby(r.index.year)}


def control_p(expo: np.ndarray, px_ret: np.ndarray, actual: float,
              funding_per_day: float) -> dict:
    rng = np.random.default_rng(CONTROL_SEED)
    n = len(expo)
    offsets = rng.integers(30, n - 30, size=CONTROL_DRAWS)
    shifted = np.array([sharpe(returns_from_exposure(np.roll(expo, o),
                                                     px_ret, funding_per_day))
                        for o in offsets])
    return {"p": round(float((shifted >= actual).mean()), 4),
            "shift_sharpe_median": round(float(np.median(shifted)), 3),
            "shift_sharpe_p95": round(float(np.percentile(shifted, 95)), 3)}


# ─── small-account replay with Bybit lot rounding ────────────────────────

def small_account(sig: pd.Series, exec_px: pd.Series, start: str,
                  equity0: float, funding_per_day: float) -> dict:
    """Trade only when target exposure changes; qty floored to 0.001 BTC."""
    px = exec_px[exec_px.index >= pd.Timestamp(start)]
    expo_target = sig.shift(1).reindex(px.index).fillna(0.0)
    cash, qty, prev_e = equity0, 0.0, 0.0
    peak, max_dd, skipped, trades = equity0, 0.0, 0, 0
    for day, p in px.items():
        e = float(expo_target[day])
        if e != prev_e:
            equity = cash + qty * p
            want = np.floor(e * equity / p / LOT_BTC + 1e-9) * LOT_BTC
            if e > 0 and want < LOT_BTC:
                skipped += 1
            dq = want - qty
            if abs(dq) > 1e-12:
                cash -= dq * p + abs(dq) * p * COST_PER_SIDE
                qty = want
                trades += 1
            prev_e = e
        cash -= qty * p * funding_per_day
        equity = cash + qty * p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1)
    return {"start_equity": equity0, "end_equity": round(equity, 2),
            "return": round(equity / equity0 - 1, 4),
            "max_dd": round(max_dd, 4), "orders": trades,
            "entries_skipped_below_lot": skipped}


# ─── run ──────────────────────────────────────────────────────────────────

def period_slice(s: pd.Series, name: str) -> pd.Series:
    a, b = PERIODS[name]
    s = s[s.index >= pd.Timestamp(a)]
    return s[s.index <= pd.Timestamp(b)] if b else s


def run() -> dict:
    spec_hash = hashlib.sha256(SPEC.read_bytes()).hexdigest()
    daily = pd.read_csv(DAILY, index_col="date", parse_dates=True)
    close, exec_px = daily["close"], daily["exec_px"]
    px_ret = interval_returns(exec_px)
    sigs = signals(close)

    res = {"spec_sha256": spec_hash,
           "data_last_day": str(daily.index[-1].date()),
           "periods": {}, "sensitivity": {}, "small_account": {}}

    for pname in PERIODS:
        pr = period_slice(px_ret, pname)
        block = {"buy_hold_spot": metrics(buy_hold(pr, 0.0)),
                 "buy_hold_perp": metrics(buy_hold(pr, FUNDING_PER_DAY["primary"])),
                 "buy_hold_perp_yearly": yearly(buy_hold(pr, FUNDING_PER_DAY["primary"]))}
        for cname, sig in sigs.items():
            net, expo = strategy_returns(sig, px_ret, FUNDING_PER_DAY["primary"])
            net, expo = period_slice(net, pname), period_slice(expo, pname)
            m = metrics(net, expo)
            m["yearly"] = yearly(net)
            if pname == "test":
                m["control"] = control_p(expo.values, pr.values, m["sharpe"],
                                         FUNDING_PER_DAY["primary"])
            block[cname] = m
        res["periods"][pname] = block

    t = res["periods"]["test"]
    bh = t["buy_hold_perp"]
    verdict = {}
    for cname in sigs:
        m = t[cname]
        checks = {
            "sharpe_gt_bh_perp": m["sharpe"] > bh["sharpe"],
            "dd_le_0.7x_bh_perp": abs(m["max_dd"]) <= 0.7 * abs(bh["max_dd"]),
            "control_p_lt_bonferroni": m["control"]["p"] < BONFERRONI_P,
            "positive_total": m["total_return"] > 0,
        }
        verdict[cname] = {"checks": checks, "pass": all(checks.values())}
    res["verdict"] = verdict

    for fname, f in FUNDING_PER_DAY.items():
        pr = period_slice(px_ret, "test")
        row = {"buy_hold_perp": metrics(buy_hold(pr, f))["sharpe"]}
        for cname, sig in sigs.items():
            net, _ = strategy_returns(sig, px_ret, f)
            row[cname] = metrics(period_slice(net, "test"))["sharpe"]
        res["sensitivity"][f"test_sharpe_funding_{fname}"] = row

    for eq in (257.0, 2650.0):
        res["small_account"][f"${eq:,.0f}"] = {
            cname: small_account(sig, exec_px, PERIODS["test"][0], eq,
                                 FUNDING_PER_DAY["primary"])
            for cname, sig in sigs.items()}
    return res


if __name__ == "__main__":
    out = run()
    (HERE / "results_v1.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
