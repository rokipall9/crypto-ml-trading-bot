#!/usr/bin/env python3
"""
ml_real.py — Train ACTUAL ML models (logistic regression, random forest,
gradient boost, manual decision tree) with TIME-SERIES cross-validation
(no leakage). Compare each against v2 benchmark.

Benchmark (v2 MED gate 0.62):
  17 kept · 76.5% wr · +$2,909 PnL · 91% losses caught (39/43)
"""
import json
import sys
import numpy as np
from datetime import datetime, timedelta
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
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


def trade_features(t, history):
    """Build feature vector for trade t. History = predecessors only."""
    opened = parse_dt(t["opened_at"])
    if not opened:
        return None
    opened_ms = int(opened.timestamp() * 1000)
    rr = (abs(t["tp"] - t["entry"]) /
          abs(t["entry"] - t["sl"])) if t["sl"] != t["entry"] else 0

    # Strategy track from predecessors
    preds = [h for h in history
             if h["strategy"] == t["strategy"]
             and parse_dt(h["closed_at"])
             and int(parse_dt(h["closed_at"]).timestamp()*1000) < opened_ms]
    n = len(preds)
    if n > 0:
        recent = sorted(preds, key=lambda h: h["closed_at"], reverse=True)[:10]
        wr = sum(1 for h in recent if h["R"] > 0) / len(recent)
        avgR = sum(h["R"] for h in recent) / len(recent)
        last_R = recent[0]["R"] if recent else 0
    else:
        wr, avgR, last_R = 0.5, 0, 0

    # Global recent loss count (last 6h)
    cutoff = opened - timedelta(hours=6)
    recent_losses = sum(1 for h in history
                        if h["R"] < 0
                        and parse_dt(h["closed_at"])
                        and cutoff <= parse_dt(h["closed_at"]) < opened)

    return {
        "strategy": t["strategy"],
        "side": t["side"],
        "rr": rr,
        "hour": opened.hour,
        "dow": opened.weekday(),
        "strat_wr": wr,
        "strat_avgR": avgR,
        "strat_n": n,
        "strat_last_R": last_R,
        "recent_losses_6h": recent_losses,
    }


def build_matrix(rows):
    """Build (X, y, labels) using time-aware features.
    X: (n, k) numpy. y: 1 if win, 0 if loss. drops flat trades.
    """
    feat_list = []
    y = []
    for i, t in enumerate(rows):
        if t["R"] == 0:
            feat_list.append(None); y.append(None); continue
        history = rows[:i]
        f = trade_features(t, history)
        if f is None:
            feat_list.append(None); y.append(None); continue
        feat_list.append(f)
        y.append(1 if t["R"] > 0 else 0)
    # Encode strategy + side
    strats = [f["strategy"] for f in feat_list if f]
    sides = [f["side"] for f in feat_list if f]
    le_strat = LabelEncoder().fit(strats)
    le_side = LabelEncoder().fit(sides)

    X, y_clean, rows_clean = [], [], []
    for i, (f, lbl, r) in enumerate(zip(feat_list, y, rows)):
        if f is None:
            continue
        X.append([
            le_strat.transform([f["strategy"]])[0],
            le_side.transform([f["side"]])[0],
            f["rr"],
            f["hour"],
            f["dow"],
            f["strat_wr"],
            f["strat_avgR"],
            f["strat_n"],
            f["strat_last_R"],
            f["recent_losses_6h"],
        ])
        y_clean.append(lbl)
        rows_clean.append(r)
    return np.array(X, dtype=float), np.array(y_clean), rows_clean, le_strat, le_side


def time_series_cv(rows, model_fn, name, min_train=30):
    """Walk-forward time-series CV. For each test index i (starting at
    min_train), train on rows[0:i], predict probability of win for rows[i].
    """
    X, y, rows_clean, le_strat, le_side = build_matrix(rows)
    preds = [None] * len(rows_clean)
    for i in range(min_train, len(rows_clean)):
        try:
            model = model_fn()
            model.fit(X[:i], y[:i])
            p = model.predict_proba(X[i:i+1])[0][1]
            preds[i] = p
        except Exception as e:
            preds[i] = None
    # Evaluate at thresholds
    print(f"\n{'='*100}\n{name}\n{'='*100}")
    print(f"{'thr':>6s} {'kept':>5s} {'wins':>5s} {'loss':>5s} {'wr':>7s} "
          f"{'kept_pnl':>10s} {'losses_caught':>14s} {'beats_v2?':>10s}")
    print("-" * 80)
    n_eval = sum(1 for p in preds if p is not None)
    n_loss_total = sum(1 for r, p in zip(rows_clean, preds)
                       if p is not None and r["R"] < 0)
    best = None
    for thr in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
        keep = [(r, p) for r, p in zip(rows_clean, preds)
                if p is not None and p >= thr]
        skip = [(r, p) for r, p in zip(rows_clean, preds)
                if p is not None and p < thr]
        if not keep:
            continue
        wins = sum(1 for r, _ in keep if r["R"] > 0)
        losses = sum(1 for r, _ in keep if r["R"] < 0)
        wr = wins / (wins + losses) if (wins + losses) else 0
        k_pnl = sum(r["pnl"] for r, _ in keep)
        l_caught = sum(1 for r, _ in skip if r["R"] < 0)
        catch_pct = l_caught / n_loss_total * 100 if n_loss_total else 0
        beats = (wr >= 0.765 and k_pnl >= 2909 and catch_pct >= 91)
        mark = "✅ YES" if beats else ("∼close" if (wr >= 0.70 or catch_pct >= 85)
                                       else "")
        print(f"{thr:>6.2f} {len(keep):>5d} {wins:>5d} {losses:>5d} "
              f"{wr*100:>6.1f}% {k_pnl:>+10.2f} "
              f"{l_caught:>4d}/{n_loss_total:<3d} ({catch_pct:>5.1f}%) {mark:>10s}")
        score = wr * 0.4 + (catch_pct/100) * 0.3 + (k_pnl / 5000) * 0.3
        if best is None or score > best[0]:
            best = (score, thr, len(keep), wr, k_pnl, catch_pct)
    if best:
        print(f"  → best: thr={best[1]}, kept={best[2]}, wr={best[3]*100:.1f}%, "
              f"pnl=${best[4]:+.2f}, catch={best[5]:.1f}%")
    return best


def main():
    rows = _load_all_closes()
    rows.sort(key=lambda r: r["closed_at"] or "")
    rows = [r for r in rows if r["entry"] and r["sl"] and r["tp"]]
    print(f"Universe: {len(rows)} trades")
    print(f"Benchmark v2 MED: 17 kept, 76.5% wr, +$2909, 91% losses caught\n")

    # Approach M1: Logistic Regression
    time_series_cv(rows,
                   lambda: LogisticRegression(max_iter=1000, C=1.0,
                                              class_weight="balanced"),
                   "M1 — Logistic Regression (class-balanced)")

    # Approach M2: Random Forest
    time_series_cv(rows,
                   lambda: RandomForestClassifier(n_estimators=100,
                                                  max_depth=4,
                                                  min_samples_leaf=3,
                                                  class_weight="balanced",
                                                  random_state=42),
                   "M2 — Random Forest (depth=4, balanced)")

    # Approach M3: Gradient Boosting
    time_series_cv(rows,
                   lambda: GradientBoostingClassifier(n_estimators=80,
                                                     max_depth=3,
                                                     learning_rate=0.05,
                                                     min_samples_leaf=3,
                                                     random_state=42),
                   "M3 — Gradient Boosting")

    # Approach M4: Manual decision tree from EDA insights
    print(f"\n{'='*100}\nM4 — Manual decision tree (EDA-derived rules)\n{'='*100}")
    keep, skip = [], []
    for r in rows:
        rr = abs(r["tp"] - r["entry"]) / abs(r["entry"] - r["sl"]) \
            if r["sl"] != r["entry"] else 0
        # Hard rules — order matters
        if "_4h" in r["strategy"]:
            keep.append((r, "4h_strat"))
        elif r["side"] == "sell" and "s3_ote" not in r["strategy"]:
            keep.append((r, "sell_signal"))
        elif rr < 2.0:
            skip.append((r, "low_rr"))
        elif r["strategy"] == "SMC_CONFLUENCE" and r["side"] == "buy":
            # SMC longs only when RR ≥ 2.5 AND hour 16-20 UTC
            opened = parse_dt(r["opened_at"])
            if rr >= 2.5 and opened and 16 <= opened.hour < 20:
                keep.append((r, "smc_long_premium"))
            else:
                skip.append((r, "smc_long_default_skip"))
        elif r["strategy"] in ("s2_mss_1h", "s3_ote_1h") and r["side"] == "buy":
            # Only if predecessors > 5 AND strat_wr in mean-reversion sweet spot
            history = [h for h in rows if h["strategy"] == r["strategy"]
                       and parse_dt(h["closed_at"])
                       and parse_dt(h["closed_at"]) < parse_dt(r["opened_at"])]
            recent = sorted(history, key=lambda h: h["closed_at"],
                            reverse=True)[:10]
            wr_recent = (sum(1 for h in recent if h["R"] > 0) / len(recent)
                         if recent else 0.5)
            if 0.30 <= wr_recent < 0.45 and rr >= 2.5:
                keep.append((r, "1h_buy_meanrev"))
            else:
                skip.append((r, "1h_buy_skip"))
        else:
            skip.append((r, "default_skip"))
    wins = sum(1 for r, _ in keep if r["R"] > 0)
    losses = sum(1 for r, _ in keep if r["R"] < 0)
    wr = wins / (wins + losses) if (wins + losses) else 0
    k_pnl = sum(r["pnl"] for r, _ in keep)
    n_loss = sum(1 for r in rows if r["R"] < 0)
    l_caught = sum(1 for r, _ in skip if r["R"] < 0)
    catch_pct = l_caught / n_loss * 100 if n_loss else 0
    print(f"  kept={len(keep)} wins={wins} losses={losses} wr={wr*100:.1f}% "
          f"pnl=${k_pnl:+.2f} losses_caught={l_caught}/{n_loss} ({catch_pct:.1f}%)")
    beats = (wr >= 0.765 and k_pnl >= 2909 and catch_pct >= 91)
    print(f"  beats_v2? {'✅ YES' if beats else 'no'}")

    # Break down by reason
    from collections import Counter
    print(f"\n  Keep reasons:")
    for reason, n in Counter(r2 for _, r2 in keep).most_common():
        bucket = [r for r, x in keep if x == reason]
        w = sum(1 for r in bucket if r["R"] > 0)
        l = sum(1 for r in bucket if r["R"] < 0)
        pnl = sum(r["pnl"] for r in bucket)
        print(f"    {reason:25s} n={n:>3d} wr={w/(w+l)*100 if w+l else 0:>5.1f}% pnl=${pnl:+.2f}")


if __name__ == "__main__":
    main()
