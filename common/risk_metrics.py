"""
risk_metrics.py — Production-grade risk-adjusted metrics.

Computes per-trade (NOT annualized — explicit):
  - mean_r, median_r              (central tendency, robust)
  - std_r                         (sample stdev, n-1 denominator)
  - downside_std                  (Sortino denominator)
  - sharpe, sortino, calmar
  - sharpe_95ci_low / _high       (bootstrap, 1000 iterations)
  - max_dd                        (peak-to-trough)
  - expectancy, win_rate
  - avg_win, avg_loss
  - kelly_fraction                (fractional Kelly recommendation, capped 25%)

Cached: result invalidated when ledger byte-size changes.
"""
from __future__ import annotations

import math
import os
import random
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from ledger import closes, LEDGER


def _series() -> List[float]:
    return [float(r.get("r", 0)) for r in closes()]


def _stats(rs: List[float]) -> Dict:
    n = len(rs)
    if n == 0:
        return {"n": 0, "mean_r": 0, "median_r": 0, "std_r": 0,
                "downside_std": 0, "sharpe": None, "sortino": None,
                "calmar": None, "max_dd": 0, "total_r": 0,
                "expectancy": 0, "win_rate": 0,
                "avg_win": 0, "avg_loss": 0, "kelly_fraction": None}

    mean = sum(rs) / n
    sorted_rs = sorted(rs)
    median = (sorted_rs[n // 2] if n % 2 == 1
              else (sorted_rs[n // 2 - 1] + sorted_rs[n // 2]) / 2)

    # SAMPLE variance (n-1) — unbiased
    if n > 1:
        var = sum((x - mean) ** 2 for x in rs) / (n - 1)
        std = math.sqrt(var) if var > 0 else 0
    else:
        std = 0

    negs = [x - mean for x in rs if x < mean]
    if len(negs) > 1:
        d_var = sum(d ** 2 for d in negs) / (len(negs) - 1)
        d_std = math.sqrt(d_var) if d_var > 0 else 0
    else:
        d_std = 0

    sharpe  = (mean / std)   if std   > 0 else None
    sortino = (mean / d_std) if d_std > 0 else None

    eq = [0.0]
    for x in rs: eq.append(eq[-1] + x)
    peak = 0.0; max_dd = 0.0
    for v in eq:
        if v > peak: peak = v
        dd = peak - v
        if dd > max_dd: max_dd = dd
    total_r = eq[-1]
    calmar = (total_r / max_dd) if max_dd > 0 else None

    wins   = [x for x in rs if x > 0]
    losses = [x for x in rs if x < 0]
    win_rate  = len(wins) / n
    avg_win   = (sum(wins)   / len(wins))   if wins   else 0
    avg_loss  = (sum(losses) / len(losses)) if losses else 0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    if losses and avg_loss < 0 and wins:
        rr = avg_win / abs(avg_loss)
        kelly = win_rate - (1 - win_rate) / rr
        kelly = max(0.0, min(kelly, 0.25))   # cap at 25% bankroll
    else:
        kelly = None

    return {
        "n": n, "mean_r": mean, "median_r": median, "std_r": std,
        "downside_std": d_std, "sharpe": sharpe, "sortino": sortino,
        "calmar": calmar, "max_dd": max_dd, "total_r": total_r,
        "expectancy": expectancy, "win_rate": win_rate,
        "avg_win": avg_win, "avg_loss": avg_loss, "kelly_fraction": kelly,
    }


def _bootstrap_sharpe_ci(rs: List[float], n_iter: int = 1000,
                         alpha: float = 0.05
                         ) -> Tuple[Optional[float], Optional[float]]:
    """95% CI on Sharpe via non-parametric bootstrap."""
    if len(rs) < 5:
        return (None, None)
    rng = random.Random(42)
    sharpes: List[float] = []
    n = len(rs)
    for _ in range(n_iter):
        sample = [rs[rng.randrange(n)] for _ in range(n)]
        m = sum(sample) / n
        if n > 1:
            var = sum((x - m) ** 2 for x in sample) / (n - 1)
            sd = math.sqrt(var) if var > 0 else 0
        else:
            sd = 0
        if sd > 0:
            sharpes.append(m / sd)
    if not sharpes:
        return (None, None)
    sharpes.sort()
    lo = sharpes[max(0, int(len(sharpes) * (alpha / 2)) - 1)]
    hi = sharpes[min(len(sharpes) - 1, int(len(sharpes) * (1 - alpha / 2)))]
    return (lo, hi)


# Size-keyed cache
_CACHE: Dict = {"size": -1, "result": None}


def compute_metrics(use_cache: bool = True) -> Dict:
    cur_size = os.path.getsize(LEDGER) if os.path.exists(LEDGER) else 0
    if use_cache and _CACHE["size"] == cur_size and _CACHE["result"] is not None:
        return _CACHE["result"]

    rs = _series()
    s = _stats(rs)
    if s["n"] >= 5:
        lo, hi = _bootstrap_sharpe_ci(rs)
        s["sharpe_95ci_low"]  = lo
        s["sharpe_95ci_high"] = hi
    else:
        s["sharpe_95ci_low"]  = None
        s["sharpe_95ci_high"] = None

    _CACHE["size"]   = cur_size
    _CACHE["result"] = s
    return s


if __name__ == "__main__":
    import json
    print(json.dumps(compute_metrics(), indent=2, default=str))
