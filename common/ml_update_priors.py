"""
ml_update_priors.py — Weekly online updater for ml_filter's per-strategy
priors using Bayesian Beta-Bernoulli posteriors.

Why: backtest evidence is frozen at the date weights were set. Markets
shift. This script walks the ml_outcomes.jsonl log every week and
updates the `strategy_specific` priors so the filter ADAPTS without
needing a full retrain.

Algorithm (per strategy + side):
  prior     = Beta(α₀=2, β₀=2)              ← neutral 50% with N=4 weight
  posterior = Beta(α₀+wins, β₀+losses)
  weight    = posterior_mean × shrinkage    ← shrunk toward 0.5 if low n
  shrinkage = n / (n + κ)                    ← κ=10 → need ~10 samples
                                              to trust the data fully

Output: /home/ubuntu/bot/logs/ml_weights_live.json
   {
     "updated_at": "...",
     "n_samples_total": 47,
     "strategy_specific": {
       "SMC_CONFLUENCE|buy": 0.32,  ← updated from data
       ...
     },
     "lcb_lower":          { ... } ← lower 95% CI bounds
   }

ml_filter.py reads this file at module-load time and PREFERS its values
over the hard-coded EDA priors. If the file is missing or stale > 14
days, ml_filter falls back to hard-coded values.
"""
from __future__ import annotations

import os
import sys
import json
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta

DECISIONS_LOG = "/home/ubuntu/bot/logs/ml_decisions.jsonl"
OUTCOMES_LOG = "/home/ubuntu/bot/logs/ml_outcomes.jsonl"
WEIGHTS_OUT = "/home/ubuntu/bot/logs/ml_weights_live.json"

# Prior strength: Beta(2,2) = neutral 50% wr with 4 pseudo-samples
PRIOR_ALPHA = 2.0
PRIOR_BETA = 2.0
# Shrinkage: with κ=10, need ~10 real samples to reach 50% of posterior weight
SHRINKAGE_KAPPA = 10.0


def _load_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    except FileNotFoundError:
        pass
    return rows


def _beta_stats(alpha, beta):
    """Posterior mean and 95% credible interval lower bound."""
    mean = alpha / (alpha + beta)
    # Approximate normal LCB using variance
    var = (alpha * beta) / ((alpha + beta) ** 2 * (alpha + beta + 1))
    sd = math.sqrt(var)
    lcb = max(0, mean - 1.645 * sd)
    return mean, lcb, sd


def update_priors():
    outcomes = _load_jsonl(OUTCOMES_LOG)
    # Only keep outcomes with a real R value
    outcomes = [o for o in outcomes
                if o.get("realized_R") is not None
                and o.get("strategy")]

    # Group by (strategy, side)
    buckets = defaultdict(lambda: {"wins": 0, "losses": 0, "samples": []})
    for o in outcomes:
        strat = o.get("strategy", "")
        side = (o.get("side") or "").lower()
        if not strat:
            continue
        key = f"{strat}|{side}" if side else strat
        bucket = buckets[key]
        R = float(o.get("realized_R", 0))
        if R > 0:
            bucket["wins"] += 1
            bucket["samples"].append(1)
        elif R < 0:
            bucket["losses"] += 1
            bucket["samples"].append(0)

    # Also build strategy-only fallback (in case key|side is sparse)
    strat_only = defaultdict(lambda: {"wins": 0, "losses": 0})
    for key, b in buckets.items():
        strat = key.split("|")[0]
        strat_only[strat]["wins"] += b["wins"]
        strat_only[strat]["losses"] += b["losses"]

    # Compute updated weights
    strategy_specific = {}
    lcb_lower = {}
    posteriors = {}

    for key, b in buckets.items():
        n = b["wins"] + b["losses"]
        if n == 0:
            continue
        a = PRIOR_ALPHA + b["wins"]
        be = PRIOR_BETA + b["losses"]
        mean, lcb, sd = _beta_stats(a, be)
        # Shrinkage toward 0.5
        shrunk = (n * mean + SHRINKAGE_KAPPA * 0.5) / (n + SHRINKAGE_KAPPA)
        strategy_specific[key] = round(shrunk, 4)
        lcb_lower[key] = round(lcb, 4)
        posteriors[key] = {
            "wins": b["wins"], "losses": b["losses"], "n": n,
            "posterior_mean": round(mean, 4), "lcb_95": round(lcb, 4),
            "shrunk_weight": round(shrunk, 4), "sd": round(sd, 4),
        }

    # Strategy-only fallback (lower confidence)
    for strat, b in strat_only.items():
        n = b["wins"] + b["losses"]
        if n == 0 or strat in strategy_specific:
            continue
        a = PRIOR_ALPHA + b["wins"]
        be = PRIOR_BETA + b["losses"]
        mean, lcb, _ = _beta_stats(a, be)
        shrunk = (n * mean + SHRINKAGE_KAPPA * 0.5) / (n + SHRINKAGE_KAPPA)
        strategy_specific[strat] = round(shrunk, 4)
        lcb_lower[strat] = round(lcb, 4)

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "n_samples_total": sum(b["wins"] + b["losses"] for b in buckets.values()),
        "n_strategies_keyed": len(buckets),
        "prior": {"alpha": PRIOR_ALPHA, "beta": PRIOR_BETA},
        "shrinkage_kappa": SHRINKAGE_KAPPA,
        "strategy_specific": strategy_specific,
        "lcb_lower": lcb_lower,
        "posteriors": posteriors,
    }

    # Atomic write
    tmp = WEIGHTS_OUT + ".tmp"
    os.makedirs(os.path.dirname(WEIGHTS_OUT), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2, default=str)
    os.replace(tmp, WEIGHTS_OUT)

    return out


def print_summary(out):
    print(f"Updated at: {out['updated_at']}")
    print(f"Total samples: {out['n_samples_total']}")
    print(f"Strategies updated: {out['n_strategies_keyed']}")
    print()
    print(f"{'key':30s} {'n':>4s} {'W':>3s} {'L':>3s} {'mean':>6s} "
          f"{'shrunk':>7s} {'LCB95':>7s}")
    print("-" * 75)
    for key, p in sorted(out["posteriors"].items(),
                          key=lambda kv: -kv[1]["shrunk_weight"]):
        print(f"{key[:30]:30s} {p['n']:>4d} {p['wins']:>3d} {p['losses']:>3d} "
              f"{p['posterior_mean']:>6.3f} {p['shrunk_weight']:>7.3f} "
              f"{p['lcb_95']:>7.3f}")


if __name__ == "__main__":
    out = update_priors()
    print_summary(out)
    print(f"\nWrote {WEIGHTS_OUT}")
