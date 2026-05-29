#!/usr/bin/env python3
"""
ml_radical.py — Test radical alternatives to the weighted-heuristic Phase 0
filter. Each approach is backtested time-aware on the 89 historical closes.

Benchmark to beat (v2 @ MED gate 0.62):
  17 kept · 76.5% wr · +$2,909 PnL · 91% losses caught (39/43)

Approaches:
  R1 KNN lookup       — predict win rate from K most-similar past trades
  R2 Cooldown filter  — block N hours after any loss; require win to reset
  R3 Beta bandit      — per-strategy Bayesian win-rate estimate (Thompson)
  R4 Hybrid ensemble  — must pass R1 AND R2 AND v2 score gate
"""
import json
import sys
import math
from collections import defaultdict
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


# ─── R1: K-Nearest-Neighbors ─────────────────────────────────────────

def r1_score(trade, history, k=7):
    """Predict win-prob using K most-similar predecessor trades.

    Similarity metric: weighted match on (strategy, side, hour_bin, dow_bin,
    rr_bin, strat_wr_bin). Lower distance = more similar.
    """
    opened = parse_dt(trade["opened_at"])
    if not opened:
        return None
    opened_ms = int(opened.timestamp() * 1000)

    # Build trade's key features
    rr = abs(trade["tp"] - trade["entry"]) / abs(trade["entry"] - trade["sl"]) \
        if trade["sl"] != trade["entry"] else 0
    rr_bin = round(rr * 2) / 2   # 0.5R bins
    hour_bin = opened.hour // 4
    dow = opened.weekday()
    side = trade["side"]
    strat = trade["strategy"]

    # All eligible predecessors
    preds = []
    for h in history:
        h_dt = parse_dt(h["closed_at"])
        if not h_dt or int(h_dt.timestamp()*1000) >= opened_ms:
            continue
        h_opened = parse_dt(h["opened_at"])
        if not h_opened:
            continue
        h_rr = (abs(h["tp"] - h["entry"]) /
                abs(h["entry"] - h["sl"])) if h["sl"] != h["entry"] else 0
        h_rr_bin = round(h_rr * 2) / 2
        # Distance: weighted features
        dist = 0
        dist += 0  if h["strategy"] == strat else 3   # strategy dominates
        dist += 0  if h["side"] == side else 2
        dist += abs((h_opened.hour // 4) - hour_bin) * 0.5
        dist += abs(h_opened.weekday() - dow) * 0.3
        dist += abs(h_rr_bin - rr_bin) * 0.8
        preds.append((dist, h))

    if not preds:
        return None
    preds.sort(key=lambda x: x[0])
    nearest = [h for _, h in preds[:k]]
    if not nearest:
        return None
    wins = sum(1 for h in nearest if h["R"] > 0)
    wr = wins / len(nearest)
    # Laplace smoothing for confidence (avoid 0/1 on small samples)
    smooth_wr = (wins + 1) / (len(nearest) + 2)
    return {"score": round(smooth_wr, 4), "k_used": len(nearest),
            "raw_wr": round(wr, 3)}


# ─── R2: Cooldown / path-dependent ───────────────────────────────────

def r2_should_skip(trade, history, cooldown_hours=4,
                   consec_loss_block=2):
    """Block if:
       (a) ANY losing close in the last `cooldown_hours`
       (b) OR strategy has lost `consec_loss_block` in a row
    Returns (skip: bool, reason: str).
    """
    opened = parse_dt(trade["opened_at"])
    if not opened:
        return False, "no-time"
    cutoff = opened - timedelta(hours=cooldown_hours)

    # (a) Any recent loss
    recent_losses = []
    for h in history:
        h_close = parse_dt(h["closed_at"])
        if not h_close:
            continue
        if h_close >= opened:
            continue
        if h_close < cutoff:
            continue
        if h["R"] < 0:
            recent_losses.append(h)
    if recent_losses:
        return True, f"{len(recent_losses)} loss(es) in last {cooldown_hours}h"

    # (b) Per-strategy consecutive loss streak
    strat_history = [h for h in history
                     if h["strategy"] == trade["strategy"]
                     and parse_dt(h["closed_at"])
                     and parse_dt(h["closed_at"]) < opened]
    strat_history.sort(key=lambda h: h["closed_at"], reverse=True)
    streak = 0
    for h in strat_history:
        if h["R"] < 0:
            streak += 1
        elif h["R"] > 0:
            break
    if streak >= consec_loss_block:
        return True, f"strategy lost {streak} in a row"

    return False, "ok"


# ─── R3: Beta-Bernoulli bandit per strategy ──────────────────────────

def r3_strategy_posterior(trade, history, prior_a=2, prior_b=2):
    """Bayesian per-strategy win-rate posterior. Uses Beta(2,2) as
    weakly-informative prior (= 50% wr with 4 'pseudo-trades' of weight).
    Returns posterior mean as score. Naturally tightens as data grows.
    """
    opened = parse_dt(trade["opened_at"])
    if not opened:
        return None
    strat_history = [h for h in history
                     if h["strategy"] == trade["strategy"]
                     and parse_dt(h["closed_at"])
                     and parse_dt(h["closed_at"]) < opened]
    wins = sum(1 for h in strat_history if h["R"] > 0)
    losses = sum(1 for h in strat_history if h["R"] < 0)
    a = prior_a + wins
    b = prior_b + losses
    posterior_mean = a / (a + b)
    # Lower CI bound — conservative play (favors strategies we're sure about)
    var = (a * b) / ((a + b)**2 * (a + b + 1))
    sd = math.sqrt(var)
    lcb = max(0, posterior_mean - 1.0 * sd)
    return {"score": round(lcb, 4), "post_mean": round(posterior_mean, 3),
            "wins": wins, "losses": losses}


# ─── Backtest harness — evaluate each approach at multiple cutoffs ──

def backtest(rows, scorer_fn, name, cutoffs):
    print(f"\n{'='*100}\n{name}\n{'='*100}")
    print(f"{'cut':>6s} {'kept':>5s} {'wins':>5s} {'losses':>7s} "
          f"{'wr':>7s} {'kept_pnl':>10s} {'losses_caught':>14s}")
    print("-" * 70)
    all_scores = []
    for r in rows:
        s = scorer_fn(r, rows)
        if s is None:
            all_scores.append(None)
        else:
            all_scores.append(s["score"] if isinstance(s, dict) else s)

    n_losses_total = sum(1 for r in rows if r["R"] < 0)
    best = None
    for cut in cutoffs:
        keep = []
        skip = []
        for r, s in zip(rows, all_scores):
            if s is None:
                continue
            if s >= cut:
                keep.append(r)
            else:
                skip.append(r)
        if not keep:
            continue
        wins = sum(1 for r in keep if r["R"] > 0)
        losses = sum(1 for r in keep if r["R"] < 0)
        wr = wins / (wins + losses) if (wins + losses) else 0
        k_pnl = sum(r["pnl"] for r in keep)
        losses_skipped = sum(1 for r in skip if r["R"] < 0)
        loss_catch_pct = losses_skipped / n_losses_total * 100 if n_losses_total else 0
        print(f"{cut:>6.2f} {len(keep):>5d} {wins:>5d} {losses:>7d} "
              f"{wr*100:>6.1f}% {k_pnl:>+10.2f} "
              f"{losses_skipped:>4d}/{n_losses_total:<3d} ({loss_catch_pct:>5.1f}%)")
        # Track best by combined metric: prefer high WR + high loss catch
        score_combined = wr * 0.5 + (loss_catch_pct/100) * 0.5
        if best is None or score_combined > best[0]:
            best = (score_combined, cut, len(keep), wr, k_pnl, loss_catch_pct)
    print(f"  → best: cut={best[1]}, kept={best[2]}, wr={best[3]*100:.1f}%, "
          f"pnl=${best[4]:+.2f}, losses_caught={best[5]:.1f}%")
    return best


# ─── R2 binary filter — runs without scoring ─────────────────────────

def backtest_r2(rows):
    print(f"\n{'='*100}\nR2 — Cooldown (block 4h after any loss + 2-in-a-row strategy block)\n{'='*100}")
    keep, skip = [], []
    for r in rows:
        s, _reason = r2_should_skip(r, rows)
        if s:
            skip.append(r)
        else:
            keep.append(r)
    if not keep:
        print("blocked everything"); return None
    wins = sum(1 for r in keep if r["R"] > 0)
    losses = sum(1 for r in keep if r["R"] < 0)
    wr = wins / (wins + losses) if (wins + losses) else 0
    k_pnl = sum(r["pnl"] for r in keep)
    losses_skipped = sum(1 for r in skip if r["R"] < 0)
    n_loss = sum(1 for r in rows if r["R"] < 0)
    catch_pct = losses_skipped / n_loss * 100 if n_loss else 0
    print(f"  kept={len(keep)} wins={wins} losses={losses} wr={wr*100:.1f}% "
          f"pnl=${k_pnl:+.2f} losses_caught={losses_skipped}/{n_loss} ({catch_pct:.1f}%)")
    return (None, None, len(keep), wr, k_pnl, catch_pct)


# ─── R4: Ensemble — pass R2 cooldown AND R1 KNN AND R3 bandit ───────

def backtest_r4(rows, r1_min=0.55, r3_min=0.45):
    print(f"\n{'='*100}\nR4 — Ensemble (R2 cooldown ∧ R1 KNN≥{r1_min} ∧ R3 bandit≥{r3_min})\n{'='*100}")
    keep, skip = [], []
    for r in rows:
        s2_skip, _ = r2_should_skip(r, rows)
        if s2_skip:
            skip.append(r); continue
        s1 = r1_score(r, rows)
        if s1 is None or s1["score"] < r1_min:
            skip.append(r); continue
        s3 = r3_strategy_posterior(r, rows)
        if s3 is None or s3["score"] < r3_min:
            skip.append(r); continue
        keep.append(r)
    if not keep:
        print("blocked everything"); return None
    wins = sum(1 for r in keep if r["R"] > 0)
    losses = sum(1 for r in keep if r["R"] < 0)
    wr = wins / (wins + losses) if (wins + losses) else 0
    k_pnl = sum(r["pnl"] for r in keep)
    losses_skipped = sum(1 for r in skip if r["R"] < 0)
    n_loss = sum(1 for r in rows if r["R"] < 0)
    catch_pct = losses_skipped / n_loss * 100 if n_loss else 0
    print(f"  kept={len(keep)} wins={wins} losses={losses} wr={wr*100:.1f}% "
          f"pnl=${k_pnl:+.2f} losses_caught={losses_skipped}/{n_loss} ({catch_pct:.1f}%)")
    return (None, None, len(keep), wr, k_pnl, catch_pct)


# ─── Main: load, run, compare ────────────────────────────────────────

def main():
    rows = _load_all_closes()
    rows.sort(key=lambda r: r["closed_at"] or "")
    rows = [r for r in rows if r["entry"] and r["sl"] and r["tp"]]

    print(f"Universe: {len(rows)} closed trades")
    n_w = sum(1 for r in rows if r["R"] > 0)
    n_l = sum(1 for r in rows if r["R"] < 0)
    n_f = sum(1 for r in rows if r["R"] == 0)
    base_pnl = sum(r["pnl"] for r in rows)
    print(f"Baseline (no filter): {len(rows)} kept, {n_w/(n_w+n_l)*100:.1f}% wr, "
          f"${base_pnl:+.2f} pnl, 0/{n_l} losses caught (0%)")
    print(f"Benchmark v2 MED gate: 17 kept, 76.5% wr, +$2909, 39/{n_l} (91%)")

    # R1 KNN
    backtest(rows, r1_score, "R1 — KNN lookup (k=7)",
             [0.45, 0.50, 0.55, 0.60, 0.65, 0.70])

    # R2 cooldown
    backtest_r2(rows)

    # R3 Beta posterior
    backtest(rows, r3_strategy_posterior, "R3 — Beta-Bernoulli per-strategy",
             [0.30, 0.35, 0.40, 0.45, 0.50, 0.55])

    # R4 ensemble
    backtest_r4(rows, r1_min=0.55, r3_min=0.40)
    backtest_r4(rows, r1_min=0.60, r3_min=0.40)
    backtest_r4(rows, r1_min=0.55, r3_min=0.45)


if __name__ == "__main__":
    main()
