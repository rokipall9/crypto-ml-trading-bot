"""
ml_retrain.py — Manual retrain pipeline. Run on demand.

Pairs every ml_decisions.jsonl entry with its outcome (via trade_id),
builds feature matrix + label vector, runs proper time-series CV with
multiple model families, reports candidates ranked by Brier score
(calibration quality) and AUC.

Does NOT auto-deploy. Outputs a candidate report:
  /home/ubuntu/bot/logs/ml_retrain_report_<ts>.json

You inspect, then manually swap weights into ml_filter.py if a model wins.

Usage:
  python3 /home/ubuntu/common/ml_retrain.py             # full retrain
  python3 /home/ubuntu/common/ml_retrain.py --min 30    # require 30+ samples
"""
from __future__ import annotations

import os
import sys
import json
import argparse
import math
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import LabelEncoder, StandardScaler
    from sklearn.metrics import brier_score_loss, roc_auc_score
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

DECISIONS_LOG = "/home/ubuntu/bot/logs/ml_decisions.jsonl"
OUTCOMES_LOG = "/home/ubuntu/bot/logs/ml_outcomes.jsonl"
REPORT_DIR = "/home/ubuntu/bot/logs/"


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


def _pair_data():
    """Return list of {features, label, decided_at, trade_id, score}
    for every outcome that has a matching decision."""
    decisions = _load_jsonl(DECISIONS_LOG)
    outcomes = _load_jsonl(OUTCOMES_LOG)

    paired = []
    for o in outcomes:
        if o.get("realized_R") is None:
            continue
        R = float(o["realized_R"])
        if R == 0:
            continue
        # Try to find the matching decision. Match: same strategy, side,
        # entry, and decision's decided_at is just before outcome opened_at.
        strat = o.get("strategy")
        side = (o.get("side") or "").lower()
        entry = o.get("entry_price")
        opened_at = o.get("opened_at", "")

        # Best match: most-recent decision for this strategy before opened
        best = None
        for d in decisions:
            f = d.get("features", {}) or {}
            if f.get("strategy") != strat:
                continue
            if (f.get("side") or "").lower() != side:
                continue
            if entry is not None and f.get("entry") is not None:
                if abs(float(f["entry"]) - float(entry)) > 1:
                    continue
            d_at = d.get("decided_at", "")
            if d_at > opened_at:
                continue
            if best is None or d_at > best.get("decided_at", ""):
                best = d
        if best is None:
            continue
        paired.append({
            "features": best.get("features", {}),
            "label": 1 if R > 0 else 0,
            "R": R,
            "decided_at": best.get("decided_at", ""),
            "ml_score": best.get("score"),
            "tier": best.get("tier"),
            "strategy": strat,
            "side": side,
        })
    paired.sort(key=lambda r: r["decided_at"])
    return paired


def _build_matrix(paired):
    """Encode features → numpy matrix. Returns X, y, feature_names."""
    if not paired:
        return None, None, None
    strats = sorted({p["features"].get("strategy", "?") for p in paired})
    sides = sorted({p["features"].get("side", "?") for p in paired})
    regimes = sorted({p["features"].get("regime", "?") for p in paired})

    s2i = {s: i for i, s in enumerate(strats)}
    sd2i = {s: i for i, s in enumerate(sides)}
    rg2i = {s: i for i, s in enumerate(regimes)}

    X = []
    for p in paired:
        f = p["features"]
        X.append([
            s2i.get(f.get("strategy", "?"), 0),
            sd2i.get(f.get("side", "?"), 0),
            rg2i.get(f.get("regime", "?"), 0),
            float(f.get("target_R", 0) or 0),
            float(f.get("hour_utc", 12) or 12),
            float(f.get("dow", 0) or 0),
            float(f.get("strategy_winrate_10", 0.5) or 0.5),
            float(f.get("strategy_avg_R_10", 0) or 0),
            float(f.get("strategy_n_samples", 0) or 0),
            float(f.get("atr_norm", 1) or 1),
            float(f.get("funding_rate", 0) or 0),
            1.0 if f.get("bybit_has_position") else 0.0,
        ])
    feature_names = ["strategy", "side", "regime", "target_R", "hour_utc",
                     "dow", "strat_wr10", "strat_avgR10", "strat_n",
                     "atr_norm", "funding", "has_position"]
    y = np.array([p["label"] for p in paired])
    return np.array(X, dtype=float), y, feature_names


def _walk_forward_cv(X, y, model_fn, min_train=10):
    """Standard time-series CV. Predict each point using only past data."""
    preds = []
    for i in range(min_train, len(X)):
        try:
            m = model_fn()
            m.fit(X[:i], y[:i])
            p = m.predict_proba(X[i:i+1])[0][1]
        except Exception:
            p = 0.5
        preds.append(p)
    return np.array(preds), y[min_train:]


def _calibration_table(preds, labels, bins=5):
    """Reliability diagram data."""
    out = []
    qs = np.quantile(preds, np.linspace(0, 1, bins + 1))
    for i in range(bins):
        lo, hi = qs[i], qs[i+1] + (1e-9 if i == bins-1 else 0)
        mask = (preds >= lo) & (preds < hi if i < bins-1 else preds <= hi)
        if mask.sum() == 0:
            continue
        avg_pred = preds[mask].mean()
        actual_wr = labels[mask].mean()
        out.append({"bin": i+1, "n": int(mask.sum()),
                    "predicted": round(float(avg_pred), 3),
                    "actual": round(float(actual_wr), 3),
                    "calibration_err": round(float(avg_pred - actual_wr), 3)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=20,
                    help="Min paired samples to attempt retrain")
    args = ap.parse_args()

    if not HAS_SKLEARN:
        print("ERROR: sklearn not installed. pip install scikit-learn")
        return 1

    paired = _pair_data()
    print(f"Paired (decision ↔ outcome) samples: {len(paired)}")
    if len(paired) < args.min:
        print(f"Need ≥ {args.min} samples to retrain. "
              f"Currently have {len(paired)}.")
        print("Recommendation: keep bot running, ml_update_priors.py "
              "will adapt weights weekly in the meantime.")
        return 0

    X, y, names = _build_matrix(paired)
    print(f"Feature matrix: {X.shape}")
    print(f"Win rate (overall): {y.mean()*100:.1f}%")
    print()

    models = {
        "LogReg":        lambda: LogisticRegression(max_iter=1000, C=1.0,
                                                     class_weight="balanced"),
        "LogReg_L1":     lambda: LogisticRegression(max_iter=1000, C=0.5,
                                                     penalty="l1",
                                                     solver="liblinear"),
        "RF_depth4":     lambda: RandomForestClassifier(n_estimators=120,
                                                         max_depth=4,
                                                         min_samples_leaf=3,
                                                         class_weight="balanced",
                                                         random_state=42),
        "GBM_d3":        lambda: GradientBoostingClassifier(n_estimators=80,
                                                              max_depth=3,
                                                              learning_rate=0.05,
                                                              random_state=42),
    }

    print(f"{'model':12s} {'brier':>8s} {'auc':>6s} {'best_thr':>9s} "
          f"{'kept':>5s} {'wr':>6s}")
    print("-" * 65)

    candidates = []
    for name, fn in models.items():
        preds, labels = _walk_forward_cv(X, y, fn, min_train=10)
        if len(preds) < 5:
            continue
        try:
            brier = brier_score_loss(labels, preds)
        except Exception:
            brier = 1.0
        try:
            auc = roc_auc_score(labels, preds) if len(set(labels)) > 1 else 0.5
        except Exception:
            auc = 0.5

        # Find threshold that maximizes (kept_wr × kept_count_sqrt)
        best_thr = 0.5
        best_score = -1
        best_metrics = None
        for thr in np.linspace(0.3, 0.8, 11):
            keep = preds >= thr
            if keep.sum() < 3:
                continue
            wr = labels[keep].mean()
            kept = keep.sum()
            score = wr * math.sqrt(kept)
            if score > best_score:
                best_score = score
                best_thr = float(thr)
                best_metrics = (int(kept), float(wr))
        kept, wr = best_metrics if best_metrics else (0, 0)
        candidates.append({
            "name": name,
            "brier": round(brier, 4),
            "auc": round(auc, 3),
            "best_thr": round(best_thr, 3),
            "kept": kept,
            "wr": round(wr, 3),
            "calibration": _calibration_table(preds, labels),
        })
        print(f"{name:12s} {brier:>8.4f} {auc:>6.3f} {best_thr:>9.3f} "
              f"{kept:>5d} {wr*100:>5.1f}%")

    # Compare with current ml_score outputs
    cur_preds = np.array([p["ml_score"] for p in paired if p["ml_score"] is not None])
    cur_labels = np.array([p["label"] for p in paired if p["ml_score"] is not None])
    if len(cur_preds) >= 5:
        cur_brier = brier_score_loss(cur_labels, cur_preds)
        try:
            cur_auc = roc_auc_score(cur_labels, cur_preds) if len(set(cur_labels)) > 1 else 0.5
        except Exception:
            cur_auc = 0.5
        print(f"{'CURRENT_v2':12s} {cur_brier:>8.4f} {cur_auc:>6.3f}")

    # Save report
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_paired": len(paired),
        "n_wins": int(y.sum()),
        "n_losses": int(len(y) - y.sum()),
        "feature_names": names,
        "candidates": candidates,
        "current_v2_brier": float(cur_brier) if len(cur_preds) >= 5 else None,
        "current_v2_auc": float(cur_auc) if len(cur_preds) >= 5 else None,
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = os.path.join(REPORT_DIR, f"ml_retrain_report_{ts}.json")
    with open(out, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nFull report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
