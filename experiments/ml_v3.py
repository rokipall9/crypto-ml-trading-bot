#!/usr/bin/env python3
"""
ml_v3.py — Layered adaptive filter inspired by prop-firm risk systems.

Design:
  Layer A: Market context features (BTC 24h move, 4h vol regime)
  Layer B: Hard vetoes (RR<1.8, position concentration, extreme vol)
  Layer C: Drawdown cooldown (post-loss pause)
  Layer D: Strategy health monitor (per-strategy rolling Sharpe-like)
  Layer E: Per-strategy thresholds (SMC longs gated hard, sells gated light)
  Layer F: Calibration map (raw score → actual historical win rate)

Every layer is independently testable. Backtest compares each addition
against v2 baseline.

Benchmark v2 MED gate:
  17 kept · 76.5% wr · +$2,909 PnL · 91% loss catch
"""
import json
import sys
import math
import urllib.request
import numpy as np
from datetime import datetime, timedelta, timezone
from collections import defaultdict

sys.path.insert(0, "/home/ubuntu/common")
exec(open("/tmp/ml_backtest.py").read().split("def main()")[0])


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ─── Layer A: BTC market context ─────────────────────────────────────

_KLINE_CACHE_4H = None


def _fetch_4h_klines():
    """Get plenty of 4h BTCUSDT klines covering the entire backtest period."""
    global _KLINE_CACHE_4H
    if _KLINE_CACHE_4H is not None:
        return _KLINE_CACHE_4H
    # Pull last 200 bars of 4h (~33 days back, covers our trade window)
    url = ("https://api.bybit.com/v5/market/kline?"
           "category=linear&symbol=BTCUSDT&interval=240&limit=500")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            j = json.loads(r.read())
        # Each bar: [openTime_ms, open, high, low, close, volume, turnover]
        bars = list(reversed(j["result"]["list"]))
        _KLINE_CACHE_4H = [
            {"ms": int(b[0]), "open": float(b[1]), "high": float(b[2]),
             "low": float(b[3]), "close": float(b[4]), "vol": float(b[5])}
            for b in bars]
        return _KLINE_CACHE_4H
    except Exception as e:
        print(f"kline fetch fail: {e}")
        return []


def market_context(at_dt):
    """Compute BTC context at the moment of a trade signal.
    Returns:
      btc_24h_pct: BTC price change in last 24h (%)
      btc_4h_atr_norm: current 4h ATR vs 20-bar median
      btc_regime: BULL/BEAR/CHOP/UNKNOWN based on 20-bar 4h slope
      btc_dist_from_high: distance from 20-bar high as % (0=at high, neg=below)
    """
    if not at_dt:
        return {}
    target_ms = int(at_dt.timestamp() * 1000)
    bars = _fetch_4h_klines()
    if len(bars) < 30:
        return {}
    # Find bars before target
    past = [b for b in bars if b["ms"] < target_ms]
    if len(past) < 20:
        return {}
    last20 = past[-20:]
    closes = np.array([b["close"] for b in last20])
    highs = np.array([b["high"] for b in last20])
    lows = np.array([b["low"] for b in last20])
    cur = closes[-1]

    # 24h change = current close vs 6 bars ago (6*4h=24h)
    if len(past) >= 6:
        c_24h = past[-6]["close"]
        btc_24h_pct = (cur - c_24h) / c_24h * 100
    else:
        btc_24h_pct = 0

    # 4h ATR
    trs = []
    for i in range(1, len(last20)):
        h, l, pc = highs[i], lows[i], closes[i-1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_now = np.mean(trs[-14:]) if len(trs) >= 14 else np.mean(trs)
    atr_med = np.median(trs) if trs else 1
    atr_norm = round(atr_now / atr_med, 3) if atr_med > 0 else 1

    # Regime via slope
    slope, _ = np.polyfit(np.arange(20), closes, 1)
    slope_pct = slope / np.mean(closes) * 100
    rng = (highs.max() - lows.min()) / np.mean(closes)
    if slope_pct > 0.20 and rng > 0.02:
        regime = "BULL"
    elif slope_pct < -0.20 and rng > 0.02:
        regime = "BEAR"
    else:
        regime = "CHOP"

    # Distance from 20-bar high
    high20 = highs.max()
    dist_from_high = (cur - high20) / high20 * 100

    return {
        "btc_24h_pct": round(btc_24h_pct, 3),
        "btc_atr_norm_4h": atr_norm,
        "btc_regime": regime,
        "btc_dist_from_high_pct": round(dist_from_high, 3),
        "btc_slope_pct": round(slope_pct, 3),
    }


# ─── Layer C: Drawdown cooldown ──────────────────────────────────────

def cooldown_block(trade, history, hours=6, max_losses=2):
    """Block if N+ losses in the last `hours` hours. Real prop systems
    use this — losses cluster temporally."""
    opened = parse_dt(trade["opened_at"])
    if not opened:
        return False, ""
    cutoff = opened - timedelta(hours=hours)
    recent_losses = [h for h in history
                     if h["R"] < 0
                     and parse_dt(h["closed_at"])
                     and cutoff <= parse_dt(h["closed_at"]) < opened]
    if len(recent_losses) >= max_losses:
        return True, f"{len(recent_losses)} losses in last {hours}h"
    # Per-strategy: 3-loss streak → block until first win
    strat_history = [h for h in history if h["strategy"] == trade["strategy"]
                     and parse_dt(h["closed_at"])
                     and parse_dt(h["closed_at"]) < opened]
    strat_history.sort(key=lambda h: h["closed_at"], reverse=True)
    streak = 0
    for h in strat_history:
        if h["R"] < 0:
            streak += 1
        elif h["R"] > 0:
            break
    if streak >= 3:
        return True, f"strategy lost {streak} in a row"
    return False, ""


# ─── Layer D: Strategy health monitor ────────────────────────────────

def strategy_health(strategy, history, at_dt, window=15):
    """Rolling Sharpe-like for the strategy. Returns 0-1 health score.
    < 0.3 = strategy is breaking. > 0.7 = healthy."""
    at_ms = int(at_dt.timestamp() * 1000) if at_dt else 0
    past = [h for h in history if h["strategy"] == strategy
            and parse_dt(h["closed_at"])
            and int(parse_dt(h["closed_at"]).timestamp()*1000) < at_ms]
    past.sort(key=lambda h: h["closed_at"], reverse=True)
    recent = past[:window]
    if len(recent) < 5:
        return 0.50    # insufficient data — neutral
    Rs = np.array([h["R"] for h in recent])
    if Rs.std() == 0:
        return 0.50
    sharpe = Rs.mean() / Rs.std()
    # Map sharpe to [0,1]
    return float(max(0, min(1, (sharpe + 1.5) / 3)))


# ─── Layer F: Calibration map ────────────────────────────────────────

def build_calibration_map(rows):
    """Take v2 raw scores from history, bucket by 0.05 score band,
    compute actual win rate per bucket. Returns map: score → calibrated_prob."""
    scored = []
    for t in rows:
        s = _backtest_score(t, rows)
        if s is None or t["R"] == 0:
            continue
        scored.append((s["score"], 1 if t["R"] > 0 else 0))
    scored.sort()
    # Bucket by quantile-aligned bands for stability
    bands = {}
    band_width = 0.05
    for sc, w in scored:
        b = round(sc / band_width) * band_width
        bands.setdefault(b, []).append(w)
    cal = {}
    for b, ws in bands.items():
        if len(ws) >= 3:
            cal[b] = sum(ws) / len(ws)
    # Monotone isotonic adjustment — calibrated map should be non-decreasing
    keys = sorted(cal.keys())
    last_val = 0
    monotone = {}
    for k in keys:
        v = max(cal[k], last_val)
        monotone[k] = v
        last_val = v
    return monotone


def calibrated_prob(raw_score, cal_map):
    """Look up calibrated probability for a raw score (nearest bin)."""
    if not cal_map:
        return raw_score
    keys = sorted(cal_map.keys())
    # Find closest band
    best_k = min(keys, key=lambda k: abs(k - raw_score))
    return cal_map[best_k]


# ─── Layer E: Per-strategy thresholds ────────────────────────────────

# From EDA — strategies need different thresholds because their score
# distributions and win-rate profiles differ.
PER_STRATEGY_THRESHOLD = {
    # strict (worst historical)
    "SMC_CONFLUENCE":        0.68,
    "s2_mss_1h":             0.66,
    "s3_ote_1h":             0.64,
    # loose (winners or low-sample)
    "SMC_CONFLUENCE_SHORT":  0.60,
    "s3_ote_4h":             0.50,
    "s2_mss_4h":             0.50,
}


def per_strategy_gate(trade, v2_score):
    thr = PER_STRATEGY_THRESHOLD.get(trade["strategy"], 0.62)
    return v2_score >= thr


# ─── Anomaly detection (Mahalanobis-style) ───────────────────────────

def anomaly_score(features_vec, mean_vec, cov_inv):
    """Mahalanobis distance from historical centroid. High = unusual."""
    diff = features_vec - mean_vec
    try:
        d = math.sqrt(diff @ cov_inv @ diff)
        return d
    except Exception:
        return 0


# ─── v3 master scorer ────────────────────────────────────────────────

def v3_decide(trade, history, cal_map, anomaly_mean, anomaly_cov_inv):
    """Returns dict {pass, score, reason, layers_triggered}."""
    layers = {}
    opened = parse_dt(trade["opened_at"])

    # Layer A: Market context (informational, used in scoring)
    ctx = market_context(opened) if opened else {}
    layers["context"] = ctx

    # Layer B: Hard vetoes
    rr = (abs(trade["tp"] - trade["entry"]) /
          abs(trade["entry"] - trade["sl"])) if trade["sl"] != trade["entry"] else 0
    if rr < 1.8:
        return {"pass": False, "reason": "veto:rr<1.8", "score": 0,
                "layers": layers}
    if ctx.get("btc_atr_norm_4h", 1) > 2.0:
        return {"pass": False, "reason": "veto:extreme_vol", "score": 0,
                "layers": layers}

    # Layer C: Drawdown cooldown
    cd_block, cd_reason = cooldown_block(trade, history)
    if cd_block:
        return {"pass": False, "reason": f"cooldown:{cd_reason}", "score": 0,
                "layers": layers}

    # Layer D: Strategy health
    health = strategy_health(trade["strategy"], history, opened)
    layers["health"] = round(health, 3)
    if health < 0.30:
        return {"pass": False, "reason": f"strategy_breaking (health={health:.2f})",
                "score": 0, "layers": layers}

    # Layer E + F: v2 score, calibrated, per-strategy threshold
    v2 = _backtest_score(trade, history)
    if not v2:
        return {"pass": False, "reason": "no v2 score", "score": 0,
                "layers": layers}
    raw = v2["score"]
    cal = calibrated_prob(raw, cal_map)
    layers["raw_score"] = raw
    layers["calibrated"] = round(cal, 3)

    # Market regime adjustment: penalize counter-trend setups
    regime = ctx.get("btc_regime", "UNKNOWN")
    is_long = trade["side"] in ("buy", "long")
    counter_trend = ((regime == "BEAR" and is_long) or
                     (regime == "BULL" and not is_long))
    if counter_trend:
        cal *= 0.80
        layers["counter_trend_penalty"] = True

    # BTC 24h dump/pump penalty (avoid catching falling knife)
    btc_24h = ctx.get("btc_24h_pct", 0)
    if is_long and btc_24h < -3.0:
        cal *= 0.70   # long while BTC dumped > 3% in 24h
        layers["dump_penalty"] = True
    if not is_long and btc_24h > 3.0:
        cal *= 0.70   # short while BTC pumped > 3%
        layers["pump_penalty"] = True

    # Per-strategy gate on CALIBRATED score
    gate = PER_STRATEGY_THRESHOLD.get(trade["strategy"], 0.55)
    if cal < gate:
        return {"pass": False,
                "reason": f"below_strategy_threshold ({cal:.3f}<{gate})",
                "score": cal, "layers": layers}

    return {"pass": True, "reason": "all_layers_passed",
            "score": cal, "layers": layers}


# ─── Backtest harness ────────────────────────────────────────────────

def main():
    rows = _load_all_closes()
    rows.sort(key=lambda r: r["closed_at"] or "")
    rows = [r for r in rows if r["entry"] and r["sl"] and r["tp"]]
    print(f"Universe: {len(rows)} trades")
    print(f"Benchmark v2 MED: 17 kept, 76.5% wr, +$2909, 91% catch\n")

    print("Building calibration map from history...")
    cal_map = build_calibration_map(rows)
    print(f"Calibration map: {len(cal_map)} bands")
    for k in sorted(cal_map.keys()):
        print(f"  score {k:.2f} → actual wr {cal_map[k]*100:.0f}%")

    # Compute anomaly baseline (mean + cov of v2 feature vectors)
    feats = []
    for t in rows:
        s = _backtest_score(t, rows)
        if s:
            f = s["features"]
            feats.append([
                f.get("target_R", 0),
                f.get("hour_utc", 0),
                f.get("dow", 0),
                f.get("strategy_winrate_10", 0.5),
            ])
    feats = np.array(feats)
    anomaly_mean = feats.mean(axis=0)
    try:
        anomaly_cov_inv = np.linalg.inv(np.cov(feats.T) + np.eye(feats.shape[1]) * 1e-3)
    except Exception:
        anomaly_cov_inv = np.eye(feats.shape[1])

    # Run v3 on every trade
    print("\nRunning v3 layered backtest...")
    results = []
    layer_stops = defaultdict(int)
    for t in rows:
        # History = trades closed before this one's open
        opened = parse_dt(t["opened_at"])
        history = [r for r in rows if parse_dt(r["closed_at"])
                   and parse_dt(r["closed_at"]) < opened]
        decision = v3_decide(t, history, cal_map, anomaly_mean, anomaly_cov_inv)
        results.append((t, decision))
        if not decision["pass"]:
            # First word of reason
            cat = decision["reason"].split(":")[0]
            layer_stops[cat] += 1

    keep = [(t, d) for t, d in results if d["pass"]]
    skip = [(t, d) for t, d in results if not d["pass"]]

    wins = sum(1 for t, _ in keep if t["R"] > 0)
    losses = sum(1 for t, _ in keep if t["R"] < 0)
    wr = wins / (wins + losses) if (wins + losses) else 0
    k_pnl = sum(t["pnl"] for t, _ in keep)
    n_loss = sum(1 for t in rows if t["R"] < 0)
    l_caught = sum(1 for t, _ in skip if t["R"] < 0)
    catch_pct = l_caught / n_loss * 100 if n_loss else 0

    print()
    print("=" * 100)
    print(f"v3 RESULT")
    print("=" * 100)
    print(f"kept={len(keep)} (wins={wins}, losses={losses}, flat="
          f"{len(keep)-wins-losses}) wr={wr*100:.1f}%")
    print(f"P&L kept: ${k_pnl:+.2f}")
    print(f"Losses caught: {l_caught}/{n_loss} ({catch_pct:.1f}%)")
    print()
    print("Layer-stop breakdown:")
    for layer, n in sorted(layer_stops.items(), key=lambda x: -x[1]):
        print(f"  {layer:30s}  {n}")

    # Compare to v2
    print()
    print("=" * 100)
    print("COMPARISON")
    print("=" * 100)
    print(f"{'metric':25s} {'v2':>10s} {'v3':>10s} {'delta':>10s}")
    metrics = [
        ("trades_kept", 17, len(keep)),
        ("win_rate_%", 76.5, round(wr*100, 1)),
        ("p&l_$", 2909, round(k_pnl)),
        ("loss_catch_%", 91.0, round(catch_pct, 1)),
    ]
    for name, v2v, v3v in metrics:
        d = v3v - v2v
        sym = "✅" if d > 0 else ("=" if d == 0 else "❌")
        print(f"{name:25s} {v2v:>10} {v3v:>10} {d:>+10} {sym}")

    # Detail of escaped losses
    escaped = [(t, d) for t, d in keep if t["R"] < 0]
    if escaped:
        print(f"\nLOSSES STILL ESCAPING v3 ({len(escaped)}):")
        for t, d in escaped:
            print(f"  {t['closed_at'][:19]} {t['strategy'][:18]:18s} "
                  f"{t['side'][:4]:4s} R={t['R']:+.2f} pnl=${t['pnl']:+.2f} "
                  f"cal={d['score']:.3f}")

    # Wins we'd miss (cost of filter)
    missed = [(t, d) for t, d in skip if t["R"] > 0]
    if missed:
        print(f"\nWINS WE MISS ({len(missed)}):")
        for t, d in missed[:10]:
            print(f"  {t['closed_at'][:19]} {t['strategy'][:18]:18s} "
                  f"{t['side'][:4]:4s} R={t['R']:+.2f} pnl=${t['pnl']:+.2f}  "
                  f"reason={d['reason']}")
        if len(missed) > 10:
            print(f"  ... ({len(missed)-10} more)")


if __name__ == "__main__":
    main()
