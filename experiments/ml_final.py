#!/usr/bin/env python3
"""
ml_final.py — Final round: ensembles + honest out-of-sample test +
BTC-regime-aware filter using historical 1d klines from Bybit.
"""
import json
import sys
import numpy as np
import urllib.request
from datetime import datetime, timedelta, timezone
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import LabelEncoder

sys.path.insert(0, "/home/ubuntu/common")
exec(open("/tmp/ml_backtest.py").read().split("def main()")[0])


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ─── Historical BTC 1d klines for regime detection ─────────────────

_DAILY_CACHE = {}


def _fetch_daily_klines(start_ms, end_ms):
    """Pull BTCUSDT 1d klines from Bybit."""
    key = (start_ms, end_ms)
    if key in _DAILY_CACHE:
        return _DAILY_CACHE[key]
    url = (f"https://api.bybit.com/v5/market/kline?"
           f"category=linear&symbol=BTCUSDT&interval=D"
           f"&start={start_ms}&end={end_ms}&limit=200")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            j = json.loads(r.read())
        bars = list(reversed(j["result"]["list"]))   # chrono order
        # Each bar: [openTime, open, high, low, close, volume, turnover]
        _DAILY_CACHE[key] = bars
        return bars
    except Exception as e:
        print(f"kline fetch fail: {e}")
        return []


def btc_regime_at(dt):
    """Return BULL/BEAR/CHOP based on BTC daily trend ending at dt.
    Uses last 20 daily closes: slope of regression line + ATR vs range.
    """
    end_ms = int(dt.timestamp() * 1000)
    start_ms = end_ms - 30 * 86400 * 1000
    bars = _fetch_daily_klines(start_ms, end_ms)
    if len(bars) < 10:
        return "UNKNOWN"
    closes = [float(b[4]) for b in bars[-20:]]
    if len(closes) < 10:
        return "UNKNOWN"
    # Linear regression slope (normalized)
    n = len(closes)
    xs = np.arange(n)
    slope, intercept = np.polyfit(xs, closes, 1)
    slope_pct = slope / np.mean(closes) * 100   # %/day
    # Volatility — range vs mean
    rng = (max(closes) - min(closes)) / np.mean(closes)
    if slope_pct > 0.5 and rng > 0.03:
        return "BULL"
    if slope_pct < -0.5 and rng > 0.03:
        return "BEAR"
    return "CHOP"


# ─── Build feature matrix with regime added ─────────────────────────

def trade_features_v2(t, history):
    opened = parse_dt(t["opened_at"])
    if not opened:
        return None
    opened_ms = int(opened.timestamp() * 1000)
    rr = (abs(t["tp"] - t["entry"]) /
          abs(t["entry"] - t["sl"])) if t["sl"] != t["entry"] else 0
    preds = [h for h in history
             if h["strategy"] == t["strategy"]
             and parse_dt(h["closed_at"])
             and int(parse_dt(h["closed_at"]).timestamp()*1000) < opened_ms]
    n = len(preds)
    recent = sorted(preds, key=lambda h: h["closed_at"], reverse=True)[:10]
    wr = sum(1 for h in recent if h["R"] > 0) / len(recent) if recent else 0.5
    avgR = sum(h["R"] for h in recent) / len(recent) if recent else 0
    last_R = recent[0]["R"] if recent else 0
    cutoff = opened - timedelta(hours=6)
    recent_losses = sum(1 for h in history
                        if h["R"] < 0
                        and parse_dt(h["closed_at"])
                        and cutoff <= parse_dt(h["closed_at"]) < opened)
    regime = btc_regime_at(opened)
    return {
        "strategy": t["strategy"], "side": t["side"], "rr": rr,
        "hour": opened.hour, "dow": opened.weekday(),
        "strat_wr": wr, "strat_avgR": avgR, "strat_n": n,
        "strat_last_R": last_R, "recent_losses_6h": recent_losses,
        "regime": regime,
    }


def build_X_y(rows):
    feats, ys, kept_rows = [], [], []
    for i, t in enumerate(rows):
        if t["R"] == 0:
            continue
        f = trade_features_v2(t, rows[:i])
        if f is None:
            continue
        feats.append(f); ys.append(1 if t["R"] > 0 else 0); kept_rows.append(t)
    strats = sorted(set(f["strategy"] for f in feats))
    sides = sorted(set(f["side"] for f in feats))
    regimes = sorted(set(f["regime"] for f in feats))
    s2i = {s: i for i, s in enumerate(strats)}
    sd2i = {s: i for i, s in enumerate(sides)}
    rg2i = {s: i for i, s in enumerate(regimes)}
    X = []
    for f in feats:
        X.append([
            s2i[f["strategy"]], sd2i[f["side"]], rg2i[f["regime"]],
            f["rr"], f["hour"], f["dow"],
            f["strat_wr"], f["strat_avgR"], f["strat_n"],
            f["strat_last_R"], f["recent_losses_6h"],
        ])
    return np.array(X, dtype=float), np.array(ys), kept_rows, feats


# ─── v2 scorer (reproduce for ensemble) ──────────────────────────────

import importlib.util
spec = importlib.util.spec_from_file_location("ml_filter",
                                              "/home/ubuntu/common/ml_filter.py")
mlf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mlf)


def v2_score_row(r, rows):
    """v2 score for a single row using time-aware history."""
    res = _backtest_score(r, rows)
    return res["score"] if res else None


# ─── Approach E1: v2 ∧ RF (must agree) ──────────────────────────────

def e1_ensemble_v2_rf(rows, v2_min=0.55, rf_min=0.50):
    """Both v2 score AND RF prediction must exceed thresholds.
    RF is trained walk-forward (no leakage)."""
    X, y, kept_rows, _ = build_X_y(rows)
    rf_preds = [None] * len(kept_rows)
    for i in range(30, len(kept_rows)):
        try:
            model = RandomForestClassifier(n_estimators=120,
                                           max_depth=4,
                                           min_samples_leaf=3,
                                           class_weight="balanced",
                                           random_state=42)
            model.fit(X[:i], y[:i])
            rf_preds[i] = model.predict_proba(X[i:i+1])[0][1]
        except Exception:
            rf_preds[i] = None

    keep, skip = [], []
    for r, rfp in zip(kept_rows, rf_preds):
        if rfp is None:
            skip.append(r); continue
        v2s = v2_score_row(r, rows)
        if v2s is None:
            skip.append(r); continue
        if v2s >= v2_min and rfp >= rf_min:
            keep.append(r)
        else:
            skip.append(r)
    wins = sum(1 for r in keep if r["R"] > 0)
    losses = sum(1 for r in keep if r["R"] < 0)
    wr = wins / (wins + losses) if (wins + losses) else 0
    k_pnl = sum(r["pnl"] for r in keep)
    n_loss = sum(1 for r in rows if r["R"] < 0)
    l_caught = sum(1 for r in skip if r["R"] < 0)
    catch_pct = l_caught / n_loss * 100 if n_loss else 0
    print(f"E1 (v2≥{v2_min} ∧ RF≥{rf_min}): kept={len(keep)} wr={wr*100:.1f}% "
          f"pnl=${k_pnl:+.2f} catch={catch_pct:.1f}%")
    return {"kept": len(keep), "wr": wr, "pnl": k_pnl, "catch": catch_pct}


# ─── Approach E2: regime-aware v2 ────────────────────────────────────

def e2_regime_v2(rows):
    """v2 score ≥ 0.55, but ONLY when BTC regime aligns with side."""
    keep, skip = [], []
    for r in rows:
        if r["R"] == 0:
            continue
        opened = parse_dt(r["opened_at"])
        if not opened:
            continue
        regime = btc_regime_at(opened)
        v2 = v2_score_row(r, rows)
        if v2 is None:
            skip.append(r); continue
        # Regime alignment
        is_long = r["side"] in ("buy", "long")
        aligned = ((regime == "BULL" and is_long) or
                   (regime == "BEAR" and not is_long))
        # Accept rule: v2≥0.62 OR (v2≥0.50 AND regime-aligned)
        if v2 >= 0.62 or (v2 >= 0.50 and aligned):
            keep.append(r)
        else:
            skip.append(r)
    wins = sum(1 for r in keep if r["R"] > 0)
    losses = sum(1 for r in keep if r["R"] < 0)
    wr = wins / (wins + losses) if (wins + losses) else 0
    k_pnl = sum(r["pnl"] for r in keep)
    n_loss = sum(1 for r in rows if r["R"] < 0)
    l_caught = sum(1 for r in skip if r["R"] < 0)
    catch_pct = l_caught / n_loss * 100 if n_loss else 0
    print(f"E2 (regime+v2): kept={len(keep)} wr={wr*100:.1f}% "
          f"pnl=${k_pnl:+.2f} catch={catch_pct:.1f}%")


# ─── Honest out-of-sample: train on first 66, test on last 23 ────────

def oos_test(rows, v2_min=0.62):
    """Test v2's HONEST forward performance on a held-out 25% of data."""
    split = int(len(rows) * 0.74)
    train, test = rows[:split], rows[split:]
    test = [r for r in test if r["R"] != 0]
    print(f"\nOOS test: train_n={len(train)} test_n={len(test)}")
    keep, skip = [], []
    for r in test:
        s = _backtest_score(r, rows)  # uses time-aware predecessors (full history up to r)
        if not s:
            continue
        if s["score"] >= v2_min:
            keep.append(r)
        else:
            skip.append(r)
    wins = sum(1 for r in keep if r["R"] > 0)
    losses = sum(1 for r in keep if r["R"] < 0)
    wr = wins / (wins + losses) if (wins + losses) else 0
    k_pnl = sum(r["pnl"] for r in keep)
    n_loss = sum(1 for r in test if r["R"] < 0)
    l_caught = sum(1 for r in skip if r["R"] < 0)
    catch_pct = l_caught / n_loss * 100 if n_loss else 0
    print(f"OOS v2 @ {v2_min}: kept={len(keep)} wr={wr*100:.1f}% "
          f"pnl=${k_pnl:+.2f} catch={catch_pct:.1f}% "
          f"({l_caught}/{n_loss} losses caught)")


def main():
    rows = _load_all_closes()
    rows.sort(key=lambda r: r["closed_at"] or "")
    rows = [r for r in rows if r["entry"] and r["sl"] and r["tp"]]
    print(f"Universe: {len(rows)} trades\n")
    print(f"Benchmark v2 MED: 17 kept, 76.5% wr, +$2909, 91% catch\n")

    print("=" * 100)
    print("E1 — Ensemble: v2 score AND RandomForest agreement (walk-forward)")
    print("=" * 100)
    for v2_min, rf_min in [(0.55, 0.50), (0.60, 0.50), (0.55, 0.45),
                            (0.50, 0.55), (0.58, 0.55)]:
        e1_ensemble_v2_rf(rows, v2_min, rf_min)

    print("\n" + "=" * 100)
    print("E2 — Regime-aware v2 (BTC trend gate)")
    print("=" * 100)
    e2_regime_v2(rows)

    print("\n" + "=" * 100)
    print("OOS — Honest out-of-sample test (last 25% held out)")
    print("=" * 100)
    oos_test(rows, 0.62)
    oos_test(rows, 0.55)
    oos_test(rows, 0.50)


if __name__ == "__main__":
    main()
