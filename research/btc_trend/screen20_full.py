"""Full evaluation of all twenty screened strategies — implements
SPEC_20_FULL.md. Parameterised re-implementation of screen20.decisions
(defaults must reproduce it exactly), then gate x 3 periods, BH adjustment,
robustness re-runs, block-bootstrap CIs and fixed verdicts."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

import backtest as B
import screen20 as S

SPEC = B.HERE / "SPEC_20_FULL.md"
HOURLY_2013 = B.HERE / "data" / "btc_hourly_2013.csv"
EARLY = ("2014-01-01", "2016-12-31")
PERIODS = {"selection": S.PERIODS["selection"], "holdout": S.PERIODS["holdout"]}
BH_Q = 0.10
BOOT_N, BOOT_BLOCK = 2000, 30

DEFAULTS = {
    "T1_tsmom28": {"L": 28},
    "T2_tsmom_multi": {"ladder": (7, 14, 28, 56, 112)},
    "T3_sma200": {"n": 200},
    "T4_donchian20_10": {"hi": 20, "lo": 10},
    "T5_golden_cross": {"fast": 50, "slow": 200},
    "T6_tsmom_multi_vt": {"tv": 0.50},
    "D1_rsi2": {"thr": 10},
    "D2_panic_dip": {"drop": -0.05},
    "D3_three_down": {"hold": 5},
    "D4_weekdays": {},
    "D5_turn_of_month": {"w": 3},
    "D6_vol_breakout": {"k": 0.5},
    "D7_vol_breakout_trend": {"k": 0.5},
    "H1_hourly_trend": {"a": 24, "b": 168},
    "H2_4h_donchian": {"hi": 20, "lo": 10},
    "H3_4h_bollinger": {"z": 2.0},
    "H4_hourly_rsi": {"thr": 25},
    "S1_evening": {"start": 20},
    "S2_last_hour_momentum": {"dec": 22},
    "S3_us_open_momentum": {"dec": 13},
}
NEIGHBORS = {
    "T1_tsmom28": [{"L": 21}, {"L": 35}],
    "T2_tsmom_multi": [{"ladder": (5, 10, 21, 42, 84)}, {"ladder": (9, 18, 35, 70, 140)}],
    "T3_sma200": [{"n": 150}, {"n": 250}],
    "T4_donchian20_10": [{"hi": 15, "lo": 8}, {"hi": 25, "lo": 13}],
    "T5_golden_cross": [{"fast": 40, "slow": 150}, {"fast": 60, "slow": 250}],
    "T6_tsmom_multi_vt": [{"tv": 0.40}, {"tv": 0.60}],
    "D1_rsi2": [{"thr": 5}, {"thr": 15}],
    "D2_panic_dip": [{"drop": -0.04}, {"drop": -0.06}],
    "D3_three_down": [{"hold": 4}, {"hold": 6}],
    "D4_weekdays": [],
    "D5_turn_of_month": [{"w": 2}, {"w": 4}],
    "D6_vol_breakout": [{"k": 0.4}, {"k": 0.6}],
    "D7_vol_breakout_trend": [{"k": 0.4}, {"k": 0.6}],
    "H1_hourly_trend": [{"a": 18, "b": 126}, {"a": 30, "b": 210}],
    "H2_4h_donchian": [{"hi": 15, "lo": 8}, {"hi": 25, "lo": 13}],
    "H3_4h_bollinger": [{"z": 1.5}, {"z": 2.5}],
    "H4_hourly_rsi": [{"thr": 20}, {"thr": 30}],
    "S1_evening": [{"start": 19}, {"start": 21}],
    "S2_last_hour_momentum": [{"dec": 21}, {"dec": 20}],
    "S3_us_open_momentum": [{"dec": 12}, {"dec": 14}],
}
DAILY_DECIDED = [k for k in DEFAULTS if k[0] == "T" or k[:2] in ("D1", "D2", "D3", "D4", "D5")]


# ─── parameterised strategies ────────────────────────────────────────────

def _sma(s, n):
    return s.rolling(n, min_periods=n).mean()


def _donchian(close, hi_n, lo_n):
    hi = close.shift(1).rolling(hi_n, min_periods=hi_n).max().values
    lo = close.shift(1).rolling(lo_n, min_periods=lo_n).min().values
    c, out, pos = close.values, np.zeros(len(close)), 0.0
    for i in range(len(c)):
        if pos == 0.0 and not np.isnan(hi[i]) and c[i] > hi[i]:
            pos = 1.0
        elif pos == 1.0 and not np.isnan(lo[i]) and c[i] < lo[i]:
            pos = 0.0
        out[i] = pos
    return pd.Series(out, index=close.index)


def _tsmom_pos(close, L):
    past = close.shift(L)
    return (close / past - 1 > 0).astype(float).where(past.notna(), 0.0)


def _multi(close, ladder):
    return sum((_tsmom_pos(close, L) > 0).astype(float) for L in ladder) / len(ladder)


def _multi_vt(close, tv):
    base = _multi(close, (7, 14, 28, 56, 112))
    vol = close.pct_change().rolling(30, min_periods=30).std() * np.sqrt(365)
    target = (base * np.minimum(1.0, tv / vol)).fillna(0.0).values
    held, out = 0.0, np.zeros(len(target))
    for i, t in enumerate(target):
        if abs(t - held) > 0.10:
            held = t
        out[i] = held
    return pd.Series(out, index=close.index)


def build(name: str, h: pd.DataFrame, p: dict) -> pd.Series:
    idx = h.index
    d = S.daily_from_hourly(h)
    dc = d["close"]
    s200 = _sma(dc, 200)
    daily = None
    if name == "T1_tsmom28":
        daily = _tsmom_pos(dc, p["L"])
    elif name == "T2_tsmom_multi":
        daily = _multi(dc, p["ladder"])
    elif name == "T3_sma200":
        m = _sma(dc, p["n"])
        daily = (dc > m).astype(float).where(m.notna(), 0.0)
    elif name == "T4_donchian20_10":
        daily = _donchian(dc, p["hi"], p["lo"])
    elif name == "T5_golden_cross":
        f, s = _sma(dc, p["fast"]), _sma(dc, p["slow"])
        daily = ((f > s) & s.notna()).astype(float)
    elif name == "T6_tsmom_multi_vt":
        daily = _multi_vt(dc, p["tv"])
    elif name == "D1_rsi2":
        r2 = S.rsi(dc, 2)
        daily = pd.Series(S.enter_exit(((r2 < p["thr"]) & (dc > s200)).values,
                                       (dc > _sma(dc, 5)).values), index=dc.index)
    elif name == "D2_panic_dip":
        daily = pd.Series(S.hold_n((dc.pct_change() <= p["drop"]).values, 3), index=dc.index)
    elif name == "D3_three_down":
        down3 = (dc < dc.shift(1)) & (dc.shift(1) < dc.shift(2)) & (dc.shift(2) < dc.shift(3))
        daily = pd.Series(S.hold_n((down3 & (dc > s200)).values, p["hold"]), index=dc.index)
    elif name == "D4_weekdays":
        daily = pd.Series(((dc.index + pd.Timedelta(days=1)).dayofweek < 5)
                          .astype(float), index=dc.index)
    elif name == "D5_turn_of_month":
        dom, mend = dc.index.day, dc.index.days_in_month
        daily = pd.Series(((dom >= mend - p["w"]) | (dom <= p["w"] - 1))
                          .astype(float), index=dc.index)
    if daily is not None:
        return S.daily_to_hourly(daily, idx).fillna(0.0)

    hc, hr, day = h["close"], idx.hour, idx.floor("D")
    if name in ("D6_vol_breakout", "D7_vol_breakout_trend"):
        level = (d["open"] + p["k"] * (d["high"] - d["low"]).shift(1)).reindex(day).values
        above = hc.values > level
        trend_ok = (dc.shift(1) > _sma(dc, 20).shift(1)).reindex(day) \
            .fillna(False).astype(bool).values
        use_trend = name == "D7_vol_breakout_trend"
        out, on = np.zeros(len(idx)), False
        for i in range(len(idx)):
            if hr[i] == 0:
                on = False
            if above[i] and (not use_trend or trend_ok[i]):
                on = True
            out[i] = 0.0 if hr[i] == 23 else float(on)
        return pd.Series(out, index=idx)
    if name == "H1_hourly_trend":
        return (((hc / hc.shift(p["a"]) - 1) > 0)
                & ((hc / hc.shift(p["b"]) - 1) > 0)).astype(float)
    if name == "H2_4h_donchian":
        return S.from_4h(_donchian(S.bars_4h(h), p["hi"], p["lo"]), idx)
    if name == "H3_4h_bollinger":
        c4 = S.bars_4h(h)
        m20, sd, m200 = _sma(c4, 20), c4.rolling(20, min_periods=20).std(), _sma(c4, 200)
        boll = S.enter_exit(((c4 < m20 - p["z"] * sd) & (c4 > m200)).values,
                            (c4 >= m20).values)
        return S.from_4h(pd.Series(boll, index=c4.index), idx)
    if name == "H4_hourly_rsi":
        r14, h200 = S.rsi(hc, 14), _sma(hc, 200)
        return pd.Series(S.enter_exit(((r14 < p["thr"]) & (hc > h200)).values,
                                      (r14 > 50).values), index=idx)
    if name == "S1_evening":
        dec_hours = [(p["start"] - 1 + j) % 24 for j in range(4)]
        return pd.Series(np.isin(hr, dec_hours).astype(float), index=idx)
    day_open = d["open"].reindex(day).values
    so_far = hc.values / day_open - 1
    if name in ("S2_last_hour_momentum", "S3_us_open_momentum"):
        last = 22 if name.startswith("S2") else 18
        out, cur = np.zeros(len(idx)), 0.0
        for i in range(len(idx)):
            if hr[i] == p["dec"]:
                cur = 1.0 if so_far[i] > 0 else 0.0
            out[i] = cur if p["dec"] <= hr[i] <= last else 0.0
        return pd.Series(out, index=idx)
    raise KeyError(name)


# ─── evaluation ──────────────────────────────────────────────────────────

def net_hourly(expo, r, cost, funding_h):
    turn = np.abs(np.diff(expo, prepend=0.0))
    return expo * r - turn * cost - np.maximum(expo, 0) * funding_h


def evaluate(dec, r, period, cost=B.COST_PER_SIDE, funding_h=S.FUNDING_PER_HOUR,
             delay=1, with_control=False, with_boot=False):
    a, b = period
    expo_all = dec.shift(delay).reindex(r.index).fillna(0.0)
    mask = (r.index >= pd.Timestamp(a))
    if b:
        mask &= r.index <= pd.Timestamp(b) + pd.Timedelta(hours=23)
    rr, ee = r.values[mask], expo_all.values[mask]
    days = r.index[mask].floor("D")
    starts = np.flatnonzero(np.r_[True, days[1:] != days[:-1]])
    daily = S.to_daily(net_hourly(ee, rr, cost, funding_h), starts)
    m = B.metrics(pd.Series(daily, index=days[starts]))
    m["time_in_market"] = round(float((ee != 0).mean()), 3)
    m["entries_per_year"] = round(float(((ee > 0) & (np.r_[0.0, ee[:-1]] <= 0)).sum())
                                  / (len(ee) / 8766), 1)
    if with_control:
        rng = np.random.default_rng(B.CONTROL_SEED)
        offs = rng.integers(S.CONTROL_MIN_OFFSET_H, len(ee) - S.CONTROL_MIN_OFFSET_H,
                            size=B.CONTROL_DRAWS)
        sh = np.array([B.sharpe(S.to_daily(net_hourly(np.roll(ee, o), rr, cost, funding_h),
                                           starts)) for o in offs])
        m["control_p"] = round(float((sh >= m["sharpe"]).mean()), 4)
    if with_boot:
        m["sharpe_ci90"] = block_bootstrap_ci(daily)
    return m


def block_bootstrap_ci(daily: np.ndarray) -> list:
    rng = np.random.default_rng(B.CONTROL_SEED)
    n = len(daily)
    k = int(np.ceil(n / BOOT_BLOCK))
    sh = np.empty(BOOT_N)
    for i in range(BOOT_N):
        st = rng.integers(0, n - BOOT_BLOCK + 1, size=k)
        sample = np.concatenate([daily[s:s + BOOT_BLOCK] for s in st])[:n]
        sh[i] = B.sharpe(sample)
    return [round(float(np.percentile(sh, 5)), 3), round(float(np.percentile(sh, 95)), 3)]


def benjamini_hochberg(pvals: dict, q: float) -> dict:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    cutoff = 0
    for i, (_, p) in enumerate(items, 1):
        if p <= i / m * q:
            cutoff = i
    return {k: (i <= cutoff) for i, (k, _) in enumerate(items, 1)}


def gate(m, bh):
    return {"sharpe_gt_bh_perp": m["sharpe"] > bh["sharpe"],
            "dd_le_0.7x_bh_perp": abs(m["max_dd"]) <= 0.7 * abs(bh["max_dd"]),
            "positive_total": m["total_return"] > 0}


def build_hourly_2013() -> pd.DataFrame:
    if HOURLY_2013.exists():
        return pd.read_csv(HOURLY_2013, index_col="hour", parse_dates=True)
    old = (S.HOURLY, S.WARMUP_FROM)
    S.HOURLY, S.WARMUP_FROM = HOURLY_2013, "2013-01-01"
    try:
        return S.build_hourly()
    finally:
        S.HOURLY, S.WARMUP_FROM = old


def run() -> dict:
    h = S.build_hourly()
    r = (h["exec_px"].shift(-1) / h["exec_px"] - 1).dropna()
    ones = pd.Series(1.0, index=h.index)
    res = {"spec_sha256": hashlib.sha256(SPEC.read_bytes()).hexdigest(),
           "buy_hold_perp": {p: evaluate(ones, r, per) for p, per in PERIODS.items()},
           "strategies": {}}
    decs = {k: build(k, h, DEFAULTS[k]) for k in DEFAULTS}
    ref = S.decisions(h)
    mismatched = [k for k in DEFAULTS if not np.allclose(decs[k].values, ref[k].values)]
    if mismatched:
        raise AssertionError(f"defaults do not reproduce screen20: {mismatched}")
    res["reproduces_screen20"] = True

    for k, dec in decs.items():
        row = {}
        for p, per in PERIODS.items():
            m = evaluate(dec, r, per, with_control=True, with_boot=(p == "holdout"))
            m["gate"] = gate(m, res["buy_hold_perp"][p])
            row[p] = m
        hold = PERIODS["holdout"]
        rob = {"costs_x2": evaluate(dec, r, hold, cost=2 * B.COST_PER_SIDE)["sharpe"],
               "costs_x0.5": evaluate(dec, r, hold, cost=0.5 * B.COST_PER_SIDE)["sharpe"],
               "delay_+1h": evaluate(dec, r, hold, delay=2)["sharpe"],
               "funding_high": evaluate(dec, r, hold,
                                        funding_h=B.FUNDING_PER_DAY["high"] / 24)["sharpe"]}
        for j, nb in enumerate(NEIGHBORS[k]):
            rob[f"neighbour_{'low' if j == 0 else 'high'}"] = \
                evaluate(build(k, h, nb), r, hold)["sharpe"]
        row["robustness_holdout_sharpe"] = rob
        res["strategies"][k] = row

    # early period, daily-decided only
    h13 = build_hourly_2013()
    r13 = (h13["exec_px"].shift(-1) / h13["exec_px"] - 1).dropna()
    bh_early = evaluate(pd.Series(1.0, index=h13.index), r13, EARLY)
    res["buy_hold_perp"]["early"] = bh_early
    for k in DAILY_DECIDED:
        m = evaluate(build(k, h13, DEFAULTS[k]), r13, EARLY, with_control=True)
        m["gate"] = gate(m, bh_early)
        res["strategies"][k]["early"] = m

    # multiple testing + verdicts
    pv = {k: v["holdout"]["control_p"] for k, v in res["strategies"].items()}
    bh_sig = benjamini_hochberg(pv, BH_Q)
    bh_hold = res["buy_hold_perp"]["holdout"]
    for k, v in res["strategies"].items():
        ho = v["holdout"]
        robust = all(x > 0 for x in v["robustness_holdout_sharpe"].values()) and ho["sharpe"] > 0
        early_ok = ("early" not in v) or v["early"]["sharpe"] > bh_early["sharpe"]
        passed = (all(ho["gate"].values()) and bh_sig[k] and robust
                  and ho["sharpe_ci90"][0] > 0 and early_ok)
        promising = (ho["sharpe"] > bh_hold["sharpe"] and ho["total_return"] > 0 and robust)
        v["bh_significant"] = bh_sig[k]
        v["bonferroni_significant"] = pv[k] < 0.05 / len(pv)
        v["verdict"] = "PASS" if passed else ("PROMISING" if promising else "FAIL")
    return res


if __name__ == "__main__":
    out = run()
    (B.HERE / "results_20_full.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v["verdict"] for k, v in out["strategies"].items()}, indent=2))
