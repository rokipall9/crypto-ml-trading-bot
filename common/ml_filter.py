"""
ml_filter.py — Phase 0 quality gatekeeper.

Public API:
  score_signal(sig: dict) -> dict   # tier + score + reasons + features
  record_outcome(trade_id, result)  # for the training-data flywheel
  should_allow(score_dict)          # boolean gate (honors shadow mode)

Phase 0 implementation: transparent weighted-heuristic. Each feature
maps to a sub-score in [0,1] with a documented rationale. Final score
is a weighted sum.

Phase 1 (later): swap `_phase0_score()` for a LightGBM model trained
on data accumulated in ml_outcomes.jsonl. Same return shape, same gate
threshold. No changes needed in callers.

Shadow mode (default): scores every signal, posts to ML channel, but
does NOT gate (returns allow=True regardless of tier). Set
ML_FILTER_MODE=gate in .env to enforce hard gating.
"""
from __future__ import annotations

import os
import json
import time
import sys
from datetime import datetime, timezone
from typing import Dict, Optional, List

# Make sibling imports work whether we're run as a module or imported
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ml_features


# ─── Persistent state files ──────────────────────────────────────────

STATE_FILE = "/home/ubuntu/bot/logs/ml_state.json"
OUTCOMES_LOG = "/home/ubuntu/bot/logs/ml_outcomes.jsonl"
DECISIONS_LOG = "/home/ubuntu/bot/logs/ml_decisions.jsonl"


def _load_state() -> Dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "decisions_total": 0, "thresholds": _default_thresholds()}


def _save_state(s: Dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def _default_thresholds() -> Dict:
    # Tuned 2026-05-24 from 89-trade backtest with v2 weights.
    # At cut 0.62 the filter catches 84% of historical losses (36 of 43)
    # while keeping 44% of wins — sharp drawdown reduction.
    # HIGH ≥ 0.72 = top decile by score (consistent winners).
    # MED ≥ 0.62 = the gate cutoff — trades above this win 70% of the time.
    # LOW ≥ 0.50 = informational lower bound.
    return {"HIGH": 0.72, "MED": 0.62, "LOW": 0.50}


def _filter_mode() -> str:
    """'shadow' = score-only / 'gate' = enforce minimum tier."""
    return (os.environ.get("ML_FILTER_MODE") or "shadow").strip().lower()


def _gate_min_tier() -> str:
    """Minimum tier that passes the gate when mode=gate.
    Default MED — backtest showed MED+ gives best (kept, wr, pnl, catch).
    Override with ML_GATE_MIN_TIER=HIGH for ultra-strict (4 trades, 100% wr).
    """
    return (os.environ.get("ML_GATE_MIN_TIER") or "MED").strip().upper()


# ─── Sub-score helpers (v2 — empirically tuned from 89-trade EDA) ────
#
# EDA findings (see ml_eda.py + ml_tune.py output 2026-05-24):
#   • SELL >> BUY (63.6% wr vs 42.6%)
#   • R:R < 2.0 is the loss graveyard (9% wr)
#   • strat_wr 30-45% bucket is mean-reversion sweet spot (57% wr)
#   • strat_wr 45-55% is the dead zone (29% wr)
#   • Hour 20-24 UTC is worst window (29% wr)
#   • Mon/Wed/Fri are bad days; Tue/Thu best
#   • SMC_CONFLUENCE long has 38.9% wr over 45 samples (robust)
#
# At cut 0.62 these weights catch 84% of historical losses (36 of 43)
# while keeping 44% of wins — favoring capital preservation over P&L.

def _sub_strategy_specific(f: Dict) -> float:
    """Per-strategy historical bias, side-aware where sample size allows."""
    strat = f.get("strategy") or ""
    side = (f.get("side") or "").lower()
    # Strategy+side with adequate samples
    table = {
        "SMC_CONFLUENCE|buy":          0.30,   # n=45 wr=38.9%
        "SMC_CONFLUENCE_SHORT|sell":   0.55,   # n=7 wr=50%
        "s2_mss_1h|buy":               0.30,   # n=18 wr=38.9%
        "s2_mss_1h|sell":              0.90,   # n=3 wr=100% (cautious — small)
        "s3_ote_1h|buy":               0.40,   # n=10 wr=40%
        "s3_ote_4h|buy":               0.90,   # n=3 wr=100%
        "s2_mss_4h|buy":               0.85,   # n=1 wr=100%
    }
    key = f"{strat}|{side}"
    if key in table:
        return table[key]
    fallback = {
        "SMC_CONFLUENCE":       0.35,
        "SMC_CONFLUENCE_SHORT": 0.55,
        "s2_mss_1h":            0.40,
        "s2_mss_4h":            0.80,
        "s3_ote_1h":            0.45,
        "s3_ote_4h":            0.85,
    }
    return fallback.get(strat, 0.50)


def _sub_strategy_track(f: Dict) -> float:
    """Non-monotonic mean-reversion-aware.
       wr 30-45% = sweet spot (recently down, due to recover)
       wr 45-55% = dead zone (stuck strategies underperform)
       wr 55%+ = mean reversion against
       wr <30% = slow recovery
    """
    n = f.get("strategy_n_samples", 0) or 0
    if n < 3:
        return 0.50
    wr = f.get("strategy_winrate_10", 0.5) or 0.5
    if wr < 0.30:
        return 0.45
    if wr < 0.45:
        return 1.00       # sweet spot
    if wr < 0.55:
        return 0.20       # dead zone
    if wr < 0.70:
        return 0.55
    return 0.50


def _sub_rr_quality(f: Dict) -> float:
    """Stricter v2 RR floor — <2.0R is the loss graveyard."""
    rr = f.get("target_R") or 0
    if rr < 1.5:
        return 0.05
    if rr < 2.0:
        return 0.10
    if rr < 2.3:
        return 0.55
    if rr < 2.7:
        return 0.75
    if rr < 3.2:
        return 0.95
    return 1.00


def _sub_side_bias(f: Dict) -> float:
    """SELL beat BUY by +0.94R per trade historically. Encoded as fixed
    bonus (dampened — only 12 sell samples in the dataset)."""
    side = (f.get("side") or "").lower()
    return 0.85 if side == "sell" else 0.45


def _sub_volatility(f: Dict) -> float:
    """atr_norm: 0.7-1.3 ideal, extremes bad."""
    n = f.get("atr_norm")
    if n is None:
        return 0.60
    if 0.7 <= n <= 1.3:
        return 1.00
    if 0.5 <= n <= 1.6:
        return 0.75
    if 0.3 <= n <= 2.0:
        return 0.40
    return 0.15


def _sub_trend_alignment(f: Dict) -> float:
    """Long in BULL aligned, short in BEAR aligned, CHOP neutral."""
    regime = (f.get("regime") or "").upper()
    side = (f.get("side") or "").lower()
    is_long = side in ("buy", "long")
    if regime == "BULL":
        return 1.00 if is_long else 0.20
    if regime == "BEAR":
        return 0.20 if is_long else 1.00
    if regime == "CHOP":
        return 0.45
    return 0.50


def _sub_concentration(f: Dict) -> float:
    if not f.get("bybit_has_position"):
        return 1.00
    sig_side = (f.get("side") or "").lower()
    pos_side = (f.get("bybit_position_side") or "").lower()
    same_dir = ((sig_side in ("buy", "long") and pos_side == "buy") or
                (sig_side in ("sell", "short") and pos_side == "sell"))
    return 0.40 if same_dir else 0.10


def _sub_time_quality(f: Dict) -> float:
    """Hour + DoW combined. EDA-driven."""
    hour = f.get("hour_utc", 12) or 12
    dow = f.get("dow", 1)
    if 16 <= hour < 20:
        score = 1.00
    elif 0 <= hour < 4 or 8 <= hour < 12:
        score = 0.85
    elif 20 <= hour < 24:
        score = 0.25
    else:
        score = 0.50
    if dow in (0, 2, 4):
        score = max(0.0, score - 0.20)
    elif dow in (1, 3):
        score = min(1.0, score + 0.10)
    return round(score, 3)


def _sub_funding_safety(f: Dict) -> float:
    fr = f.get("funding_rate")
    if fr is None:
        return 0.60
    side = (f.get("side") or "").lower()
    abs_fr = abs(fr)
    if abs_fr < 0.0001:
        return 1.00
    if abs_fr < 0.0003:
        return 0.85
    if fr > 0 and side == "buy":
        return 0.30
    if fr < 0 and side == "sell":
        return 0.30
    return 0.65


# ─── Phase 0 v2 scorer ───────────────────────────────────────────────

PHASE0_WEIGHTS = {
    "strategy_specific": 0.25,   # observed per-strat edge — biggest signal
    "strategy_track":    0.13,   # mean-reversion-aware history
    "rr_quality":        0.18,   # RR < 2.0 is graveyard
    "side_bias":         0.12,   # SELL crushes BUY
    "trend_alignment":   0.08,   # only matters when regime is known
    "time_quality":      0.10,   # hour + DoW
    "volatility":        0.06,
    "concentration":     0.05,
    "funding_safety":    0.03,
}
assert abs(sum(PHASE0_WEIGHTS.values()) - 1.0) < 0.001


def _phase0_score(features: Dict) -> Dict:
    """Compute the Phase 0 v2 heuristic score + diagnostic breakdown."""
    sub = {
        "strategy_specific": _sub_strategy_specific(features),
        "strategy_track":    _sub_strategy_track(features),
        "rr_quality":        _sub_rr_quality(features),
        "side_bias":         _sub_side_bias(features),
        "trend_alignment":   _sub_trend_alignment(features),
        "time_quality":      _sub_time_quality(features),
        "volatility":        _sub_volatility(features),
        "concentration":     _sub_concentration(features),
        "funding_safety":    _sub_funding_safety(features),
    }
    total = sum(sub[k] * PHASE0_WEIGHTS[k] for k in sub)
    return {"score": round(total, 4), "breakdown": sub,
            "weights": PHASE0_WEIGHTS, "model": "phase0_heuristic_v2"}


def _tier_for(score: float, thresholds: Dict) -> str:
    if score >= thresholds.get("HIGH", 0.70):
        return "HIGH"
    if score >= thresholds.get("MED", 0.50):
        return "MED"
    if score >= thresholds.get("LOW", 0.30):
        return "LOW"
    return "SKIP"


def _reasons_text(features: Dict, sub: Dict) -> List[str]:
    """Human-readable reasons for the score — shown in Discord embed."""
    reasons = []
    # Strategy track
    n = features.get("strategy_n_samples") or 0
    wr = features.get("strategy_winrate_10") or 0
    avgR = features.get("strategy_avg_R_10") or 0
    if n < 3:
        reasons.append(f"⚪ Strategy has only {n} samples — neutral score")
    elif wr >= 0.6:
        reasons.append(f"✅ Strategy winning ({wr*100:.0f}% over last {n}, avg {avgR:+.2f}R)")
    elif wr < 0.4:
        reasons.append(f"❌ Strategy losing ({wr*100:.0f}% over last {n}, avg {avgR:+.2f}R)")
    # Volatility
    norm = features.get("atr_norm")
    if norm is not None:
        if norm > 1.6:
            reasons.append(f"⚠️ Volatility hot — ATR {norm:.2f}× normal (slippage risk)")
        elif norm < 0.5:
            reasons.append(f"⚠️ Volatility dead — ATR {norm:.2f}× normal (may stall)")
        elif 0.7 <= norm <= 1.3:
            reasons.append(f"✅ Volatility normal ({norm:.2f}× median)")
    # Trend alignment
    regime = (features.get("regime") or "").upper()
    side = (features.get("side") or "").lower()
    is_long = side in ("buy", "long")
    if regime == "BULL" and is_long:
        reasons.append("✅ Long with BULL regime")
    elif regime == "BEAR" and not is_long:
        reasons.append("✅ Short with BEAR regime")
    elif regime in ("BULL", "BEAR"):
        reasons.append(f"❌ Counter-trend ({side} in {regime})")
    elif regime == "CHOP":
        reasons.append("⚠️ Chop regime — directional bias unclear")
    # R:R
    rr = features.get("target_R") or 0
    if rr >= 2.5:
        reasons.append(f"✅ Strong R:R ({rr:.2f}R)")
    elif rr < 1.5:
        reasons.append(f"❌ Weak R:R ({rr:.2f}R)")
    # Concentration
    if features.get("bybit_has_position"):
        reasons.append(f"⚠️ Already in Bybit position ({features.get('bybit_position_size')} BTC "
                       f"{features.get('bybit_position_side')}) — aggregation risk")
    # Time
    hour = features.get("hour_utc")
    dow = features.get("dow")
    if dow is not None and dow >= 5:
        reasons.append(f"⚠️ Weekend session (DoW={dow})")
    elif hour is not None and not (7 <= hour <= 20):
        reasons.append(f"⚠️ Off-peak hour ({hour:02d}:00 UTC)")
    # Funding
    fr = features.get("funding_rate")
    if fr is not None and abs(fr) > 0.0003:
        direction = "longs paying" if fr > 0 else "shorts paying"
        reasons.append(f"⚠️ Funding extreme ({fr*100:+.3f}% — {direction})")
    return reasons


# ─── Public API ──────────────────────────────────────────────────────

def score_signal(sig: Dict) -> Dict:
    """Score a signal. Returns:
        {
          "tier": "HIGH" / "MED" / "LOW" / "SKIP",
          "score": 0.0-1.0,
          "reasons": [str, ...],
          "features": {...},
          "breakdown": {sub-score: value},
          "model": "phase0_heuristic_v1",
          "mode": "shadow" / "gate",
          "allow": bool,
          "decided_at": iso str,
        }
    """
    t0 = time.time()
    state = _load_state()
    thresholds = state.get("thresholds") or _default_thresholds()

    try:
        features = ml_features.extract_features(sig)
    except Exception as e:
        # Feature extraction itself failed — be SAFE: allow in shadow,
        # score as neutral. Don't break the bot.
        features = {"error": str(e), "strategy": sig.get("strategy") or "?"}
        return {
            "tier": "MED",
            "score": 0.5,
            "reasons": [f"⚠️ Feature extraction error: {e}"],
            "features": features,
            "breakdown": {},
            "model": "phase0_heuristic_v1",
            "mode": _filter_mode(),
            "allow": True,
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": int((time.time() - t0) * 1000),
        }

    res = _phase0_score(features)
    tier = _tier_for(res["score"], thresholds)
    mode = _filter_mode()
    # In shadow mode: always allow. In gate mode: enforce min tier from
    # ML_GATE_MIN_TIER (default MED — see backtest).
    if mode == "gate":
        min_tier = _gate_min_tier()
        order = {"SKIP": 0, "LOW": 1, "MED": 2, "HIGH": 3}
        allow = order.get(tier, 0) >= order.get(min_tier, 2)
    else:
        allow = True

    out = {
        "tier": tier,
        "score": res["score"],
        "reasons": _reasons_text(features, res["breakdown"]),
        "features": features,
        "breakdown": res["breakdown"],
        "weights": res["weights"],
        "model": res["model"],
        "mode": mode,
        "allow": allow,
        "thresholds": thresholds,
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "latency_ms": int((time.time() - t0) * 1000),
    }
    # Log decision for analytics + future-training
    try:
        _append_jsonl(DECISIONS_LOG, out)
    except Exception:
        pass
    state["decisions_total"] = int(state.get("decisions_total", 0)) + 1
    _save_state(state)
    return out


def record_outcome(trade_id: str, outcome: Dict) -> None:
    """Append a closed-trade outcome to the training-data log.
    Caller passes whatever is known: realized R, pnl, exit_reason, etc.
    """
    rec = {
        "trade_id": trade_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **outcome,
    }
    try:
        _append_jsonl(OUTCOMES_LOG, rec)
    except Exception as e:
        print(f"[ml_filter] record_outcome failed: {e}")


def should_allow(score_dict: Dict) -> bool:
    """Convenience: caller can ignore everything but this."""
    return bool(score_dict.get("allow", True))


# ─── utilities ───────────────────────────────────────────────────────

def _append_jsonl(path: str, obj: Dict) -> None:
    line = json.dumps(obj, default=str)
    with open(path, "a") as f:
        f.write(line + "\n")


# ─── CLI: quick smoke test ───────────────────────────────────────────

if __name__ == "__main__":
    # Sample SMC long signal — sanity check
    sample_sig = {
        "strategy": "SMC_CONFLUENCE",
        "side": "buy",
        "entry": 80000.0,
        "sl": 79500.0,
        "tp": 81250.0,
    }
    import pprint
    pprint.pprint(score_signal(sample_sig))
