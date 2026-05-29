"""
walkforward.py — Walk-forward validation harness.

Reads forward_results.jsonl, splits into rolling train/test windows,
computes per-fold stats. Catches overfit strategies (high in-sample
PF, low out-of-sample PF) — the same failure mode that exposed
vip_signal in the round-6 audit.

Default config:
  - 90-day train + 30-day test windows
  - Step forward 30 days per fold
  - Minimum 5 trades per window for valid stats

Output: per-fold stats + overall train/test PF ratio. A ratio < 0.7
suggests overfitting.

Usage:
  python3 walkforward.py BREAKOUT_4H        # one strategy
  python3 walkforward.py --all              # all strategies
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from ledger import closes

TRAIN_DAYS = 90
TEST_DAYS = 30
STEP_DAYS = 30
MIN_TRADES = 5


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _strategy_trades(strategy: str) -> List[Tuple[datetime, str, float]]:
    out = []
    for r in closes():
        if r.get("system") != strategy:
            continue
        ts = _parse_ts(r.get("exit_time") or r.get("opened_at") or "")
        if ts is None:
            continue
        out.append((ts, r.get("status", "?"), float(r.get("r", 0))))
    out.sort()
    return out


def _window_stats(trades: List[Tuple[datetime, str, float]]) -> Dict:
    if len(trades) < MIN_TRADES:
        return {"n": len(trades), "valid": False}
    rs = [r for _, _, r in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    total_r = sum(rs)
    wr = len(wins) / len(rs) * 100
    pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")
    return {
        "n": len(rs), "valid": True,
        "wr_pct": round(wr, 1),
        "total_r": round(total_r, 2),
        "pf": round(pf, 2) if pf != float("inf") else "inf",
    }


def walkforward(strategy: str) -> Dict:
    trades = _strategy_trades(strategy)
    if len(trades) < MIN_TRADES * 2:
        return {"strategy": strategy, "valid": False,
                "reason": f"need ≥{MIN_TRADES*2} trades, have {len(trades)}"}

    start = trades[0][0]
    end = trades[-1][0]

    folds: List[Dict] = []
    window_start = start
    while True:
        train_start = window_start
        train_end = window_start + timedelta(days=TRAIN_DAYS)
        test_start = train_end
        test_end = test_start + timedelta(days=TEST_DAYS)
        if test_end > end + timedelta(days=1):
            break

        train_trades = [t for t in trades
                        if train_start <= t[0] < train_end]
        test_trades = [t for t in trades
                       if test_start <= t[0] < test_end]

        train_stats = _window_stats(train_trades)
        test_stats = _window_stats(test_trades)
        folds.append({
            "train_start": train_start.date().isoformat(),
            "train_end": train_end.date().isoformat(),
            "test_start": test_start.date().isoformat(),
            "test_end": test_end.date().isoformat(),
            "train": train_stats, "test": test_stats,
        })
        window_start += timedelta(days=STEP_DAYS)

    if not folds:
        return {"strategy": strategy, "valid": False,
                "reason": "ledger spans less than train+test window",
                "trade_span_days": (end - start).days}

    # Aggregate
    valid_folds = [f for f in folds
                   if f["train"]["valid"] and f["test"]["valid"]]
    overall = {
        "strategy": strategy, "valid": True,
        "fold_count": len(folds),
        "valid_folds": len(valid_folds),
        "folds": folds,
    }

    if valid_folds:
        avg_train_pf = sum(
            f["train"]["pf"] for f in valid_folds
            if isinstance(f["train"]["pf"], (int, float))
        ) / max(1, len(valid_folds))
        avg_test_pf = sum(
            f["test"]["pf"] for f in valid_folds
            if isinstance(f["test"]["pf"], (int, float))
        ) / max(1, len(valid_folds))
        overall["avg_train_pf"] = round(avg_train_pf, 2)
        overall["avg_test_pf"] = round(avg_test_pf, 2)
        if avg_train_pf > 0:
            ratio = avg_test_pf / avg_train_pf
            overall["test_train_ratio"] = round(ratio, 2)
            if ratio < 0.7:
                overall["verdict"] = (f"⚠️  Likely overfit "
                                      f"(test/train PF ratio = {ratio:.2f} < 0.7)")
            else:
                overall["verdict"] = (f"✅ Robust "
                                      f"(test/train PF ratio = {ratio:.2f})")

    return overall


def all_strategies() -> List[str]:
    seen = set()
    for r in closes():
        s = r.get("system")
        if s: seen.add(s)
    return sorted(seen)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        out = {s: walkforward(s) for s in all_strategies()}
        print(json.dumps(out, indent=2, default=str))
    elif len(sys.argv) >= 2:
        print(json.dumps(walkforward(sys.argv[1]), indent=2, default=str))
    else:
        print("Usage:")
        print("  walkforward.py <strategy>")
        print("  walkforward.py --all")
