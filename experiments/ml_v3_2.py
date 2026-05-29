#!/usr/bin/env python3
"""
ml_v3_2.py — Regime-adaptive threshold.

Hypothesis: in strong-trending BTC regimes, even mid-quality setups work
because momentum carries them. In chop, only premium setups survive.

Test: gate adapts to |BTC 24h % change|.
  • Strong trend (|24h| ≥ 3%):  gate at 0.55
  • Mild trend (|24h| 1-3%):    gate at 0.62
  • Chop (|24h| < 1%):           gate at 0.66

Plus: hard veto RR < 2.0, kept from v3.1 (it captured 9 of 11 trades
in that bucket as losses).
"""
import json
import sys
import urllib.request
import numpy as np
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/home/ubuntu/common")
exec(open("/tmp/ml_backtest.py").read().split("def main()")[0])


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


_KLINES = None


def _fetch():
    global _KLINES
    if _KLINES is not None:
        return _KLINES
    url = ("https://api.bybit.com/v5/market/kline?"
           "category=linear&symbol=BTCUSDT&interval=240&limit=500")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            j = json.loads(r.read())
        bars = list(reversed(j["result"]["list"]))
        _KLINES = [{"ms": int(b[0]), "close": float(b[4]),
                    "high": float(b[2]), "low": float(b[3])}
                   for b in bars]
        return _KLINES
    except Exception as e:
        print(f"kline fail: {e}")
        return []


def btc_24h_pct(at_dt):
    """BTC % change over last 24h (6 bars of 4h klines)."""
    bars = _fetch()
    if not bars:
        return 0
    t_ms = int(at_dt.timestamp() * 1000)
    past = [b for b in bars if b["ms"] < t_ms]
    if len(past) < 7:
        return 0
    cur = past[-1]["close"]
    prev = past[-7]["close"]   # 6 bars back = 24h
    return (cur - prev) / prev * 100


def adaptive_threshold(btc_24h):
    """Higher gate in chop, lower in trend."""
    abs_mv = abs(btc_24h)
    if abs_mv >= 3.0:
        return 0.55       # strong trend — momentum helps
    if abs_mv >= 1.0:
        return 0.62
    return 0.66           # chop — be picky


def v3_2_decide(trade, history):
    rr = (abs(trade["tp"] - trade["entry"]) /
          abs(trade["entry"] - trade["sl"])) if trade["sl"] != trade["entry"] else 0
    if rr < 2.0:
        return {"pass": False, "reason": "rr<2.0", "score": 0,
                "thr": None, "btc24h": None}

    opened = parse_dt(trade["opened_at"])
    btc_mv = btc_24h_pct(opened) if opened else 0
    thr = adaptive_threshold(btc_mv)

    v2 = _backtest_score(trade, history)
    if not v2:
        return {"pass": False, "reason": "no_score", "score": 0,
                "thr": thr, "btc24h": btc_mv}
    if v2["score"] < thr:
        return {"pass": False,
                "reason": f"score {v2['score']:.3f} < adaptive thr {thr}",
                "score": v2["score"], "thr": thr, "btc24h": btc_mv}
    return {"pass": True, "reason": "all_pass", "score": v2["score"],
            "thr": thr, "btc24h": btc_mv}


def main():
    rows = _load_all_closes()
    rows.sort(key=lambda r: r["closed_at"] or "")
    rows = [r for r in rows if r["entry"] and r["sl"] and r["tp"]]
    print(f"Universe: {len(rows)} trades")
    print(f"Benchmark v2 MED: 17 kept, 76.5% wr, +$2909, 91% catch\n")

    keep, skip = [], []
    for t in rows:
        opened = parse_dt(t["opened_at"])
        history = [r for r in rows if parse_dt(r["closed_at"])
                   and parse_dt(r["closed_at"]) < opened]
        d = v3_2_decide(t, history)
        if d["pass"]:
            keep.append((t, d))
        else:
            skip.append((t, d))

    wins = sum(1 for t, _ in keep if t["R"] > 0)
    losses = sum(1 for t, _ in keep if t["R"] < 0)
    wr = wins / (wins + losses) if (wins + losses) else 0
    k_pnl = sum(t["pnl"] for t, _ in keep)
    n_loss = sum(1 for t in rows if t["R"] < 0)
    l_caught = sum(1 for t, _ in skip if t["R"] < 0)
    catch_pct = l_caught / n_loss * 100 if n_loss else 0

    print("=" * 100)
    print(f"v3.2 RESULT (regime-adaptive)")
    print("=" * 100)
    print(f"kept={len(keep)} wins={wins} losses={losses} wr={wr*100:.1f}%")
    print(f"P&L: ${k_pnl:+.2f}  catch={l_caught}/{n_loss} ({catch_pct:.1f}%)")

    # Compare
    print(f"\n{'metric':25s} {'v2':>10s} {'v3.2':>10s} {'delta':>10s}")
    metrics = [
        ("trades_kept", 17, len(keep)),
        ("win_rate_%", 76.5, round(wr*100, 1)),
        ("p&l_$", 2909, round(k_pnl)),
        ("loss_catch_%", 91.0, round(catch_pct, 1)),
    ]
    for name, v2v, v3v in metrics:
        d = v3v - v2v
        sym = "✅" if d > 0 else ("=" if d == 0 else "❌")
        print(f"{name:25s} {v2v:>10} {v3v:>10} {d:>+10.1f} {sym}")

    # Bucket breakdown by regime
    print("\nBucket performance by adaptive threshold zone:")
    buckets = {"strong (0.55)": [], "mild (0.62)": [], "chop (0.66)": []}
    for t, d in keep + skip:
        thr = d.get("thr")
        if thr is None:
            continue
        if thr == 0.55:
            buckets["strong (0.55)"].append((t, d))
        elif thr == 0.62:
            buckets["mild (0.62)"].append((t, d))
        else:
            buckets["chop (0.66)"].append((t, d))
    for name, ts in buckets.items():
        if not ts:
            continue
        kept_b = [(t, d) for t, d in ts if d["pass"]]
        w = sum(1 for t, _ in kept_b if t["R"] > 0)
        l = sum(1 for t, _ in kept_b if t["R"] < 0)
        pnl = sum(t["pnl"] for t, _ in kept_b)
        kp_wr = w/(w+l)*100 if (w+l) else 0
        print(f"  {name:25s} bucket={len(ts):3d} kept={len(kept_b):3d} "
              f"wr={kp_wr:>5.1f}% pnl=${pnl:+.2f}")


if __name__ == "__main__":
    main()
