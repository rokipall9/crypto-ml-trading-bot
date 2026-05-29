#!/usr/bin/env python3
"""
ml_v3_1.py — Surgical refinement of v2 using ONLY the insights that
the data actually supports. Each layer must have backtest evidence.

Insights kept from v3 audit:
  1. Calibration map shows 0.62-0.65 is the natural wr cliff (43% → 80%)
  2. Loss clusters exist (SMC_S sell May 20-22) — narrow same-strategy cooldown
  3. Same-side aggregate concentration causes cascading SL slippage (May 13)

Insights discarded from v3:
  ❌ Broad cooldown — blocked 18 winning recovery trades
  ❌ Per-strategy thresholds — locked out early-period winners
  ❌ Counter-trend penalty — too noisy at 89 samples
  ❌ Strategy health monitor — degenerated to neutral 0.5 too often

The v3.1 stack:
  Layer 1: Hard veto RR < 2.0
  Layer 2: Same-strategy 24h cooldown after 2 consec losses
  Layer 3: Aggregate concentration veto
  Layer 4: v2 score >= 0.62
"""
import json
import sys
import numpy as np
from datetime import datetime, timedelta

sys.path.insert(0, "/home/ubuntu/common")
exec(open("/tmp/ml_backtest.py").read().split("def main()")[0])


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def same_strategy_losing_streak(trade, history, lookback_h=48,
                                streak_n=2):
    """Block trade ONLY if its OWN strategy has lost `streak_n` of its
    last trades within `lookback_h` hours. Narrow filter — doesn't punish
    other strategies."""
    opened = parse_dt(trade["opened_at"])
    if not opened:
        return False, ""
    cutoff = opened - timedelta(hours=lookback_h)
    strat_history = [h for h in history
                     if h["strategy"] == trade["strategy"]
                     and parse_dt(h["closed_at"])
                     and cutoff <= parse_dt(h["closed_at"]) < opened]
    if not strat_history:
        return False, ""
    strat_history.sort(key=lambda h: h["closed_at"], reverse=True)
    # Are the last N (within window) ALL losses?
    recent = strat_history[:streak_n]
    if len(recent) >= streak_n and all(h["R"] < 0 for h in recent):
        return True, f"{streak_n} {trade['strategy']} losses in {lookback_h}h"
    return False, ""


def concentration_veto(trade, history):
    """Block if a SAME-STRATEGY same-side trade closed in the last 30 min
    (proxy for 'we just got out of similar exposure'). Backtest-evidence:
    May 13 12:38 cluster had this exact pattern."""
    opened = parse_dt(trade["opened_at"])
    if not opened:
        return False, ""
    cutoff = opened - timedelta(minutes=30)
    similar = [h for h in history
               if h["strategy"] == trade["strategy"]
               and h["side"] == trade["side"]
               and parse_dt(h["closed_at"])
               and cutoff <= parse_dt(h["closed_at"]) < opened
               and h["R"] < 0]
    if similar:
        return True, "30-min after same-strat same-side loss"
    return False, ""


def v3_1_decide(trade, history):
    """Layered gatekeeping. Returns dict."""
    # Layer 1: RR veto
    rr = (abs(trade["tp"] - trade["entry"]) /
          abs(trade["entry"] - trade["sl"])) if trade["sl"] != trade["entry"] else 0
    if rr < 2.0:
        return {"pass": False, "reason": "rr<2.0", "score": 0}

    # Layer 2: same-strategy losing streak
    block, why = same_strategy_losing_streak(trade, history)
    if block:
        return {"pass": False, "reason": why, "score": 0}

    # Layer 3: concentration after recent same-side loss
    block, why = concentration_veto(trade, history)
    if block:
        return {"pass": False, "reason": why, "score": 0}

    # Layer 4: v2 score gate
    v2 = _backtest_score(trade, history)
    if not v2:
        return {"pass": False, "reason": "no_score", "score": 0}
    if v2["score"] < 0.62:
        return {"pass": False, "reason": f"score<0.62 ({v2['score']:.3f})",
                "score": v2["score"]}
    return {"pass": True, "reason": "all_pass", "score": v2["score"]}


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
        d = v3_1_decide(t, history)
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
    print(f"v3.1 RESULT")
    print("=" * 100)
    print(f"kept={len(keep)} wins={wins} losses={losses} wr={wr*100:.1f}%")
    print(f"P&L: ${k_pnl:+.2f}")
    print(f"Loss catch: {l_caught}/{n_loss} ({catch_pct:.1f}%)")

    # Compare
    print()
    print("=" * 100)
    print("COMPARISON")
    print("=" * 100)
    print(f"{'metric':25s} {'v2':>10s} {'v3.1':>10s} {'delta':>10s}")
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

    # Detail
    from collections import Counter
    print(f"\nSkip-reason histogram:")
    reasons = Counter(d["reason"].split(":")[0].split(" ")[0] for _, d in skip)
    for r, n in reasons.most_common():
        print(f"  {r:30s} {n}")

    # Escaped losses
    escaped = [(t, d) for t, d in keep if t["R"] < 0]
    if escaped:
        print(f"\nLOSSES ESCAPING v3.1 ({len(escaped)}):")
        for t, d in escaped:
            print(f"  {t['closed_at'][:19]} {t['strategy'][:18]:18s} "
                  f"{t['side'][:4]:4s} R={t['R']:+.2f} ${t['pnl']:+.2f} score={d['score']:.3f}")


if __name__ == "__main__":
    main()
