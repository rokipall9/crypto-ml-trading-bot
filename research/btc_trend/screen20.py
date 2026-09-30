"""Twenty-strategy screen — implements SPEC_20.md.

Every strategy is an hourly decision series `dec` in [-1, 1]: dec[h] is
decided at the close of hour h using data up to that close, and is held over
interval k = h + 1 (exec_px[k] -> exec_px[k+1], exec_px = close of the :04
minute). Daily rules decide at the close of hour 23.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

import backtest as B
import backtest_v2 as V

SPEC = B.HERE / "SPEC_20.md"
HOURLY = B.HERE / "data" / "btc_hourly.csv"
WARMUP_FROM = "2016-01-01"
PERIODS = {"selection": ("2017-01-01", "2020-12-31"),
           "holdout": ("2021-01-01", None)}
FUNDING_PER_HOUR = B.FUNDING_PER_DAY["primary"] / 24
TOP_N = 5
GATE_P = 0.05 / TOP_N
CONTROL_MIN_OFFSET_H = 720


# ─── data ────────────────────────────────────────────────────────────────

def build_hourly() -> pd.DataFrame:
    if HOURLY.exists():
        return pd.read_csv(HOURLY, index_col="hour", parse_dates=True)
    m = pd.read_pickle(B.HERE / "data" / "btc_1m.pkl")
    m = m[m["timestamp"] >= pd.Timestamp(WARMUP_FROM).timestamp()]
    t = pd.to_datetime(m["timestamp"], unit="s")
    m = m.assign(hour=t.dt.floor("h"), minute=t.dt.minute)
    g = m.groupby("hour")
    h = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                      "low": g["low"].min(), "close": g["close"].last(),
                      "n": g.size()})
    h["exec_px"] = m[m["minute"] == 4].set_index("hour")["close"]
    h = h[h["n"] == 60].dropna()
    h.to_csv(HOURLY)
    return h


def daily_from_hourly(h: pd.DataFrame) -> pd.DataFrame:
    g = h.groupby(h.index.floor("D"))
    d = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                      "low": g["low"].min(), "close": g["close"].last(),
                      "n": g.size()})
    return d[d["n"] == 24]


def daily_to_hourly(sig: pd.Series, hours: pd.DatetimeIndex) -> pd.Series:
    """Decision at hour h = daily signal of the last day completed by h's
    close: hour 23 of D -> sig[D]; hours 0..22 of D+1 -> sig[D]."""
    done = (hours + pd.Timedelta(hours=1)).floor("D") - pd.Timedelta(days=1)
    return pd.Series(sig.reindex(done).values, index=hours).ffill().fillna(0.0)


# ─── helpers ─────────────────────────────────────────────────────────────

def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def enter_exit(enter: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    pos, out = 0.0, np.zeros(len(enter))
    for i in range(len(enter)):
        if pos == 0.0 and enter[i]:
            pos = 1.0
        elif pos == 1.0 and exit_[i]:
            pos = 0.0
        out[i] = pos
    return out


def hold_n(trigger: np.ndarray, n: int) -> np.ndarray:
    cnt, out = 0, np.zeros(len(trigger))
    for i, t in enumerate(trigger):
        if t:
            cnt = n
        out[i] = 1.0 if cnt > 0 else 0.0
        cnt = max(cnt - 1, 0)
    return out


def bars_4h(h: pd.DataFrame) -> pd.Series:
    """4-hour closes, stamped at the hour whose close ends the bar."""
    c = h["close"]
    return c[c.index.hour % 4 == 3]


def from_4h(dec4: pd.Series, hours: pd.DatetimeIndex) -> pd.Series:
    return dec4.reindex(hours).ffill().fillna(0.0)


# ─── the twenty ──────────────────────────────────────────────────────────

def decisions(h: pd.DataFrame) -> dict[str, pd.Series]:
    idx = h.index
    d = daily_from_hourly(h)
    dc = d["close"]
    sma = lambda s, n: s.rolling(n, min_periods=n).mean()
    D = {}

    # daily trend
    D["T1_tsmom28"] = B.sig_tsmom28(dc)
    D["T2_tsmom_multi"] = V.sig_multi(dc)
    D["T3_sma200"] = B.sig_sma200(dc)
    D["T4_donchian20_10"] = B.sig_donchian20_10(dc)
    s50, s200 = sma(dc, 50), sma(dc, 200)
    D["T5_golden_cross"] = ((s50 > s200) & s200.notna()).astype(float)
    D["T6_tsmom_multi_vt"] = V.sig_multi_vt(dc)

    # daily mean reversion / calendar
    r2 = rsi(dc, 2)
    D["D1_rsi2"] = pd.Series(enter_exit(((r2 < 10) & (dc > s200)).values,
                                        (dc > sma(dc, 5)).values), index=dc.index)
    D["D2_panic_dip"] = pd.Series(hold_n((dc.pct_change() <= -0.05).values, 3),
                                  index=dc.index)
    down3 = (dc < dc.shift(1)) & (dc.shift(1) < dc.shift(2)) & (dc.shift(2) < dc.shift(3))
    D["D3_three_down"] = pd.Series(hold_n((down3 & (dc > s200)).values, 5),
                                   index=dc.index)
    D["D4_weekdays"] = pd.Series(((dc.index + pd.Timedelta(days=1)).dayofweek < 5)
                                 .astype(float), index=dc.index)
    dom = dc.index.day
    mend = dc.index.days_in_month
    D["D5_turn_of_month"] = pd.Series(((dom >= mend - 3) | (dom <= 2))
                                      .astype(float), index=dc.index)

    out = {k: daily_to_hourly(v, idx) for k, v in D.items()}

    # volatility breakout (decided hourly within the day)
    day = idx.floor("D")
    level = (d["open"] + 0.5 * (d["high"] - d["low"]).shift(1)).reindex(day).values
    above = h["close"].values > level
    trend_ok = (dc.shift(1) > sma(dc, 20).shift(1)).reindex(day) \
        .fillna(False).astype(bool).values
    last_hour = idx.hour == 23
    vb, vbt = np.zeros(len(idx)), np.zeros(len(idx))
    on = on_t = False
    for i in range(len(idx)):
        if idx.hour[i] == 0:
            on = on_t = False
        if above[i]:
            on = True
            on_t = on_t or trend_ok[i]
        vb[i] = 0.0 if last_hour[i] else float(on)
        vbt[i] = 0.0 if last_hour[i] else float(on_t)
    out["D6_vol_breakout"] = pd.Series(vb, index=idx)
    out["D7_vol_breakout_trend"] = pd.Series(vbt, index=idx)

    # intraday
    hc = h["close"]
    out["H1_hourly_trend"] = (((hc / hc.shift(24) - 1) > 0)
                              & ((hc / hc.shift(168) - 1) > 0)).astype(float)
    c4 = bars_4h(h)
    out["H2_4h_donchian"] = from_4h(B.sig_donchian20_10(c4), idx)
    m20, sd20, m200 = sma(c4, 20), c4.rolling(20, min_periods=20).std(), sma(c4, 200)
    boll = enter_exit(((c4 < m20 - 2 * sd20) & (c4 > m200)).values,
                      (c4 >= m20).values)
    out["H3_4h_bollinger"] = from_4h(pd.Series(boll, index=c4.index), idx)
    r14, h200 = rsi(hc, 14), sma(hc, 200)
    out["H4_hourly_rsi"] = pd.Series(enter_exit(((r14 < 25) & (hc > h200)).values,
                                                (r14 > 50).values), index=idx)

    # time of day
    hr = idx.hour
    out["S1_evening"] = pd.Series(((hr >= 19) & (hr <= 22)).astype(float), index=idx)
    day_open = d["open"].reindex(day).values
    so_far = hc.values / day_open - 1
    out["S2_last_hour_momentum"] = pd.Series(((hr == 22) & (so_far > 0))
                                             .astype(float), index=idx)
    s3 = np.zeros(len(idx))
    cur = 0.0
    for i in range(len(idx)):
        if hr[i] == 13:
            cur = 1.0 if so_far[i] > 0 else 0.0
        elif hr[i] == 19:
            cur = 0.0
        s3[i] = cur if 13 <= hr[i] <= 18 else 0.0
    out["S3_us_open_momentum"] = pd.Series(s3, index=idx)
    return {k: v.fillna(0.0) for k, v in out.items()}


# ─── accounting and metrics ──────────────────────────────────────────────

def hourly_net(expo: np.ndarray, r: np.ndarray) -> np.ndarray:
    turn = np.abs(np.diff(expo, prepend=0.0))
    return expo * r - turn * B.COST_PER_SIDE - np.maximum(expo, 0) * FUNDING_PER_HOUR


def to_daily(net: np.ndarray, day_starts: np.ndarray) -> np.ndarray:
    return np.expm1(np.add.reduceat(np.log1p(net), day_starts))


def evaluate(dec: pd.Series, r: pd.Series, period: tuple) -> tuple[dict, np.ndarray, np.ndarray, np.ndarray]:
    a, b = period
    expo_all = dec.shift(1).reindex(r.index).fillna(0.0)
    mask = (r.index >= pd.Timestamp(a)) & \
        ((r.index <= pd.Timestamp(b) + pd.Timedelta(hours=23)) if b else True)
    rr, ee = r.values[mask], expo_all.values[mask]
    days = r.index[mask].floor("D")
    starts = np.flatnonzero(np.r_[True, days[1:] != days[:-1]])
    net = hourly_net(ee, rr)
    daily = pd.Series(to_daily(net, starts), index=days[starts])
    m = B.metrics(daily)
    entries = int(((ee > 0) & (np.r_[0.0, ee[:-1]] <= 0)).sum())
    m.update({"time_in_market": round(float((ee != 0).mean()), 3),
              "entries": entries,
              "entries_per_year": round(entries / (len(ee) / 8766), 1),
              "yearly": B.yearly(daily)})
    return m, ee, rr, starts


def control(ee: np.ndarray, rr: np.ndarray, starts: np.ndarray, actual: float) -> dict:
    rng = np.random.default_rng(B.CONTROL_SEED)
    n = len(ee)
    offs = rng.integers(CONTROL_MIN_OFFSET_H, n - CONTROL_MIN_OFFSET_H,
                        size=B.CONTROL_DRAWS)
    sh = np.array([B.sharpe(to_daily(hourly_net(np.roll(ee, o), rr), starts))
                   for o in offs])
    return {"p": round(float((sh >= actual).mean()), 4),
            "shift_sharpe_median": round(float(np.median(sh)), 3),
            "shift_sharpe_p95": round(float(np.percentile(sh, 95)), 3)}


def run() -> dict:
    h = build_hourly()
    r = (h["exec_px"].shift(-1) / h["exec_px"] - 1).dropna()
    decs = decisions(h)
    ones = pd.Series(1.0, index=h.index)
    res = {"spec_sha256": hashlib.sha256(SPEC.read_bytes()).hexdigest(),
           "hours": len(h), "last_hour": str(h.index[-1]),
           "buy_hold_perp": {}, "selection": {}, "holdout": {}}
    for p, per in PERIODS.items():
        res["buy_hold_perp"][p] = evaluate(ones, r, per)[0]
        for k, dec in decs.items():
            res[p][k] = evaluate(dec, r, per)[0]

    rank = sorted(decs, key=lambda k: res["selection"][k]["sharpe"], reverse=True)
    res["selection_rank"] = rank
    top = rank[:TOP_N]
    bh = res["buy_hold_perp"]["holdout"]
    res["top5_holdout"] = {}
    for k in top:
        m, ee, rr, starts = evaluate(decs[k], r, PERIODS["holdout"])
        m["control"] = control(ee, rr, starts, m["sharpe"])
        checks = {"sharpe_gt_bh_perp": m["sharpe"] > bh["sharpe"],
                  "dd_le_0.7x_bh_perp": abs(m["max_dd"]) <= 0.7 * abs(bh["max_dd"]),
                  "control_p_lt_0.01": m["control"]["p"] < GATE_P,
                  "positive_total": m["total_return"] > 0}
        m["checks"], m["pass"] = checks, all(checks.values())
        res["top5_holdout"][k] = m
    sel = pd.Series({k: res["selection"][k]["sharpe"] for k in decs})
    hol = pd.Series({k: res["holdout"][k]["sharpe"] for k in decs})
    res["spearman_selection_vs_holdout"] = round(float(sel.rank().corr(hol.rank())), 3)
    return res


if __name__ == "__main__":
    out = run()
    (B.HERE / "results_20.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: out[k] for k in ("selection_rank", "spearman_selection_vs_holdout")}, indent=2))
