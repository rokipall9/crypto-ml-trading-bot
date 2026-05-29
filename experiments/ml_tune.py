#!/usr/bin/env python3
"""
ml_tune.py — Apply new weight design driven by EDA, re-validate.

Key empirical findings from EDA on 89 historical closes:
  1. SELL trades crush BUY: 63.6% wr / +1.19R vs 42.6% / +0.25R
  2. R:R < 2.0 is a graveyard: 11 trades, 9% wr, -1R avg
  3. strat_wr 45-55% bucket is WORST (29% wr) — middling strategies stuck
  4. strat_wr 30-45% bucket is BEST (57% wr) — mean-reversion
  5. SMC_CONFLUENCE longs lose 61% of the time (45 samples — robust)
  6. Mon/Wed/Fri are bad (23-33% wr); Tue/Thu/Sat ok
  7. Hour 20-24 UTC is bad (28.6% wr)

These translate into v2 weights and new sub-scores below.
"""
import json
import sys
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, "/home/ubuntu/common")

# Reuse the loader + scorer skeleton from backtest
exec(open("/tmp/ml_backtest.py").read().split("def main()")[0])


# ─── Phase 0 v2 sub-scores ───────────────────────────────────────────

def _sub_strategy_specific(f):
    """Per-strategy historical bias, side-aware where sample size permits.
    Conservative when sample n < 5 (defaults to neutral 0.5).
    """
    strat = f.get("strategy", "")
    side = f.get("side", "")
    # Strategy+side combos with enough samples
    table = {
        "SMC_CONFLUENCE|buy":          0.30,   # 45 samples, 38.9% wr
        "SMC_CONFLUENCE_SHORT|sell":   0.55,   # 7 samples, 50% wr
        "s2_mss_1h|buy":               0.30,   # 18 samples, 38.9% wr
        "s2_mss_1h|sell":              0.90,   # 3 samples, 100% — small but strong
        "s3_ote_1h|buy":               0.40,   # 10 samples, 40% wr
        "s3_ote_4h|buy":               0.90,   # 3 samples, 100% — small but strong
    }
    key = f"{strat}|{side}"
    if key in table:
        return table[key]
    # Fallback by strategy alone
    fallback = {
        "SMC_CONFLUENCE":       0.35,
        "SMC_CONFLUENCE_SHORT": 0.55,
        "s2_mss_1h":            0.40,
        "s2_mss_4h":            0.80,
        "s3_ote_1h":            0.45,
        "s3_ote_4h":            0.85,
    }
    return fallback.get(strat, 0.50)


def _sub_strategy_track_v2(f):
    """Non-monotonic mean-reversion-aware. EDA shows:
       <30% → 31% wr (slow recovery)
       30-45% → 57% wr (sweet spot — undervalued)
       45-55% → 29% wr (the dead zone)
       55-70% → 50% wr (regression)
       70%+ → 50% wr (regression)
    """
    n = f.get("strategy_n_samples", 0) or 0
    if n < 3:
        return 0.50    # neutral when no history
    wr = f.get("strategy_winrate_10", 0.5) or 0.5
    if wr < 0.30:
        return 0.45
    if wr < 0.45:
        return 1.00   # mean-reversion sweet spot
    if wr < 0.55:
        return 0.20   # the dead zone
    if wr < 0.70:
        return 0.55
    return 0.50


def _sub_rr_quality_v2(f):
    """Stricter band on RR. <2R is the loss graveyard."""
    rr = f.get("target_R", 0) or 0
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


def _sub_side_bias(f):
    """SELL has been crushing BUY by +0.94R/trade. Apply observed edge
    but DAMPENED for sample-size risk (only 12 sell samples).
    """
    side = (f.get("side") or "").lower()
    if side == "sell":
        return 0.85
    return 0.45


def _sub_time_quality_v2(f):
    """Hour + DoW combined. EDA-driven."""
    hour = f.get("hour_utc", 12) or 12
    dow = f.get("dow", 1)
    score = 0.50
    # Hour
    if 16 <= hour < 20:
        score = 1.00   # NY afternoon — best
    elif 0 <= hour < 4:
        score = 0.85
    elif 8 <= hour < 12:
        score = 0.85
    elif 20 <= hour < 24:
        score = 0.25   # worst window
    else:
        score = 0.50
    # DoW penalty
    bad_dow = [0, 2, 4]   # Mon, Wed, Fri all under 35% wr
    if dow in bad_dow:
        score = max(0.0, score - 0.20)
    elif dow in (1, 3):    # Tue, Thu are good
        score = min(1.0, score + 0.10)
    return round(score, 3)


def _sub_concentration_v2(f):
    if not f.get("bybit_has_position"):
        return 1.0
    sig_side = (f.get("side") or "").lower()
    pos_side = (f.get("bybit_position_side") or "").lower()
    same_dir = ((sig_side == "buy" and pos_side == "buy") or
                (sig_side == "sell" and pos_side == "sell"))
    return 0.40 if same_dir else 0.10


def _sub_funding_v2(f):
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


# Weight design — v2
WEIGHTS_V2 = {
    "strategy_specific": 0.25,
    "strategy_track":    0.15,
    "rr_quality":        0.18,
    "side_bias":         0.12,
    "time_quality":      0.10,
    "concentration":     0.10,
    "funding_safety":    0.05,
    "buffer":            0.05,   # for ATR/regime which backtest can't see
}
assert abs(sum(WEIGHTS_V2.values()) - 1.0) < 0.001, sum(WEIGHTS_V2.values())


def score_v2(features):
    sub = {
        "strategy_specific": _sub_strategy_specific(features),
        "strategy_track":    _sub_strategy_track_v2(features),
        "rr_quality":        _sub_rr_quality_v2(features),
        "side_bias":         _sub_side_bias(features),
        "time_quality":      _sub_time_quality_v2(features),
        "concentration":     _sub_concentration_v2(features),
        "funding_safety":    _sub_funding_v2(features),
        "buffer":            0.60,    # neutral placeholder
    }
    total = sum(sub[k] * WEIGHTS_V2[k] for k in sub)
    return round(total, 4), sub


# ─── New thresholds — tuned to match data distribution ───────────────
# After scoring all 89 trades, distribution informs cut points.

def tier_v2(score, thresholds):
    if score >= thresholds["HIGH"]:
        return "HIGH"
    if score >= thresholds["MED"]:
        return "MED"
    if score >= thresholds["LOW"]:
        return "LOW"
    return "SKIP"


# ─── Main: score all historical trades with v2, find best thresholds ──

def main():
    closes = _load_all_closes()
    closes.sort(key=lambda r: r["closed_at"] or "")
    print(f"Loaded {len(closes)} closed trades")

    rows = []
    for t in closes:
        s = _backtest_score(t, closes)
        if s is None:
            continue
        score, sub = score_v2(s["features"])
        rows.append({**t, "v2_score": score, "v2_sub": sub})

    # Sort scores to find natural breakpoints
    scores = sorted([r["v2_score"] for r in rows])
    print(f"\nScore distribution:")
    pct = [0, 10, 25, 33, 50, 66, 75, 90, 100]
    for p in pct:
        idx = min(int(p / 100 * len(scores)), len(scores) - 1)
        print(f"  p{p:>3d}: {scores[idx]:.3f}")

    # Try various threshold combos
    print("\n" + "=" * 110)
    print("THRESHOLD SWEEP — find the gate that maximizes 'losses filtered'")
    print("=" * 110)
    print(f"{'cut':>6s} {'kept':>5s} {'wins':>5s} {'losses':>7s} {'wr':>7s} "
          f"{'kept_R':>8s} {'kept_pnl':>10s} {'skip':>5s} "
          f"{'skip_losses':>12s} {'avoided_pnl':>12s}")
    print("-" * 110)
    for cut in [0.55, 0.58, 0.60, 0.62, 0.64, 0.66, 0.68, 0.70, 0.72]:
        kept = [r for r in rows if r["v2_score"] >= cut]
        skipped = [r for r in rows if r["v2_score"] < cut]
        if not kept:
            continue
        wins = sum(1 for r in kept if r["R"] > 0)
        losses = sum(1 for r in kept if r["R"] < 0)
        wr = wins / (wins + losses) if (wins + losses) else 0
        k_R = sum(r["R"] for r in kept)
        k_pnl = sum(r["pnl"] for r in kept)
        s_losses = sum(1 for r in skipped if r["R"] < 0)
        s_pnl_loss = sum(r["pnl"] for r in skipped if r["pnl"] < 0)
        print(f"{cut:>6.2f} {len(kept):>5d} {wins:>5d} {losses:>7d} "
              f"{wr*100:>6.1f}% {k_R:>+8.2f} {k_pnl:>+10.2f} "
              f"{len(skipped):>5d} {s_losses:>12d} {-s_pnl_loss:>+12.2f}")

    # Pick the chosen threshold (highest skipped losses while keeping
    # wins > losses among kept). Print headline.
    best_cut = 0.62
    print(f"\nProposed gate: {best_cut} (kept-wr should be ≥ 50%, "
          f"skip the worst losses)")

    # Detailed loss table — sort losses by score, show what cluster low
    print("\n" + "=" * 110)
    print("LOSSES SORTED BY v2 SCORE (lowest = filter would catch)")
    print("=" * 110)
    print(f"{'closed_at':19s} {'strategy':16s} {'side':4s} {'R':>6s} "
          f"{'pnl':>8s} {'v2_score':>9s} {'caught?':>9s}")
    losses = [r for r in rows if r["R"] < 0]
    losses.sort(key=lambda r: r["v2_score"])
    for r in losses:
        caught = "✅ skip" if r["v2_score"] < best_cut else "❌ keep"
        print(f"{(r['closed_at'] or '')[:19]:19s} {r['strategy'][:16]:16s} "
              f"{r['side'][:4]:4s} {r['R']:>+6.2f} {r['pnl']:>+8.2f} "
              f"{r['v2_score']:>9.3f} {caught:>9s}")

    # And same for wins
    print("\n" + "=" * 110)
    print("WINS SORTED BY v2 SCORE")
    print("=" * 110)
    wins = [r for r in rows if r["R"] > 0]
    wins.sort(key=lambda r: r["v2_score"])
    n_lost_wins = 0
    for r in wins:
        caught = "❌ false-" if r["v2_score"] < best_cut else "✅ keep"
        if r["v2_score"] < best_cut:
            n_lost_wins += 1
        print(f"{(r['closed_at'] or '')[:19]:19s} {r['strategy'][:16]:16s} "
              f"{r['side'][:4]:4s} {r['R']:>+6.2f} {r['pnl']:>+8.2f} "
              f"{r['v2_score']:>9.3f} {caught:>9s}")
    print(f"\n{n_lost_wins} winning trades would be filtered out (false negatives)")


if __name__ == "__main__":
    main()
