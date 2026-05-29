#!/usr/bin/env python3
"""
ml_backtest.py — Apply the Phase 0 ML scorer to every closed trade we
have on record, time-aware (no future leakage). Report tier counts,
win rate per tier, and whether the filter would have caught the losses.

Important honesty notes:
  * Strategy track is time-aware: only counts predecessor trades that
    closed BEFORE the trade being scored.
  * ATR / regime / funding are unknown historically — we use neutral
    defaults. The score is therefore a CONSERVATIVE re-creation:
    real-time scores will be slightly different.
"""
import json
import sys
from datetime import datetime, timezone
from typing import Dict, List

sys.path.insert(0, "/home/ubuntu/common")
import ml_filter   # we re-use its scoring functions
import ml_features


PATHS = [
    "/home/ubuntu/bot/logs/paper_trades.json",
    "/home/ubuntu/bot/logs/paper_trades_book.json",
    "/home/ubuntu/bot/logs/live_trades.json",
]


def _iso_to_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _load_all_closes() -> List[Dict]:
    """Unified list of {strategy, side, entry, sl, tp, opened_at, closed_at,
    realized_R, realized_pnl, source}."""
    rows = []
    for path in PATHS:
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        # paper_trades*.json use closed_trades
        for t in d.get("closed_trades", []):
            strat = t.get("strategy") or t.get("method") or "?"
            side = (t.get("side") or "").lower()
            entry = float(t.get("entry") or 0)
            sl = float(t.get("sl_original") or t.get("sl_current")
                       or t.get("stop") or 0)
            tp = float(t.get("tp1") or t.get("target") or 0)
            risk = float(t.get("risk_amount") or 0)
            pnl = float(t.get("realized_pnl") or t.get("pnl") or 0)
            R = t.get("realized_r")
            if R is None and risk > 0:
                R = pnl / risk
            elif R is None:
                R = 0
            rows.append({
                "id": t.get("id"),
                "strategy": strat,
                "side": "buy" if side in ("buy", "long") else "sell",
                "entry": entry, "sl": sl, "tp": tp,
                "opened_at": t.get("opened_at"),
                "closed_at": t.get("closed_at"),
                "R": float(R),
                "pnl": pnl,
                "source": path.split("/")[-1],
                "status": t.get("status", ""),
            })
        # live_trades.json uses closed_orders
        for o in d.get("closed_orders", []):
            R = o.get("realized_R")
            if R is None:
                continue
            rows.append({
                "id": o.get("live_id"),
                "strategy": o.get("strategy"),
                "side": "buy" if (o.get("side") or "").lower() in ("buy", "long")
                       else "sell",
                "entry": float(o.get("entry_price_actual")
                              or o.get("entry_signal") or 0),
                "sl": float(o.get("stop") or 0),
                "tp": float(o.get("target") or 0),
                "opened_at": o.get("opened_at"),
                "closed_at": o.get("closed_at"),
                "R": float(R),
                "pnl": float(o.get("realized_pnl_usdt") or 0),
                "source": "live_trades.json",
                "status": o.get("status", ""),
            })
    # De-dup: live + paper share IDs; prefer live (has real fill)
    by_id = {}
    for r in rows:
        existing = by_id.get(r["id"])
        if not existing or r["source"] == "live_trades.json":
            by_id[r["id"]] = r
    return list(by_id.values())


def _strategy_track_at(closes: List[Dict], strategy: str,
                       at_ms: int, limit: int = 10) -> Dict:
    """Recent track AS OF the given time — only predecessors."""
    preds = []
    for r in closes:
        if r["strategy"] != strategy:
            continue
        cdt = _iso_to_dt(r["closed_at"])
        if cdt is None:
            continue
        if int(cdt.timestamp() * 1000) >= at_ms:
            continue
        preds.append(r)
    preds.sort(key=lambda x: x["closed_at"], reverse=True)
    preds = preds[:limit]
    n = len(preds)
    if n == 0:
        return {"n": 0, "winrate": 0.5, "avg_R": 0.0, "last_3_R": []}
    wins = sum(1 for x in preds if x["R"] > 0)
    return {
        "n": n,
        "winrate": round(wins / n, 3),
        "avg_R": round(sum(x["R"] for x in preds) / n, 3),
        "last_3_R": [round(x["R"], 2) for x in preds[:3]],
    }


def _backtest_score(trade: Dict, history: List[Dict]) -> Dict:
    """Reconstruct features at the moment THIS trade was opened, then
    score it with the same Phase 0 weights the live filter uses.
    Unknown features (ATR, regime, funding) use neutral defaults."""
    opened = _iso_to_dt(trade["opened_at"])
    if opened is None:
        # Skip if we can't time-align
        return None
    opened_ms = int(opened.timestamp() * 1000)

    track = _strategy_track_at(history, trade["strategy"], opened_ms)
    entry, sl, tp = trade["entry"], trade["sl"], trade["tp"]
    sl_dist = abs(entry - sl) if entry and sl else 0
    tp_dist = abs(tp - entry) if entry and tp else 0
    rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0

    features = {
        "strategy": trade["strategy"],
        "side": trade["side"],
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "sl_dist_pct": round(sl_dist / entry * 100, 4) if entry else 0,
        "tp_dist_pct": round(tp_dist / entry * 100, 4) if entry else 0,
        "target_R": round(rr, 3),
        # Unknown historicals — neutral defaults
        "atr_15m": None,
        "atr_norm": None,
        "hour_utc": opened.hour,
        "dow": opened.weekday(),
        "regime": "UNKNOWN",
        "funding_rate": None,
        "strategy_winrate_10": track["winrate"],
        "strategy_avg_R_10": track["avg_R"],
        "strategy_last_3_R": track["last_3_R"],
        "strategy_n_samples": track["n"],
        "bybit_has_position": False,
        "bybit_position_side": "",
        "bybit_position_size": 0.0,
    }
    res = ml_filter._phase0_score(features)
    tier = ml_filter._tier_for(res["score"], ml_filter._default_thresholds())
    return {"score": res["score"], "tier": tier,
            "breakdown": res["breakdown"], "features": features}


def main():
    closes = _load_all_closes()
    # Sort chronologically by close time
    closes.sort(key=lambda r: r["closed_at"] or "")
    print(f"Loaded {len(closes)} closed trades total")
    print()

    # Score each trade with time-aware predecessors
    rows = []
    for t in closes:
        s = _backtest_score(t, closes)
        if s is None:
            continue
        rows.append({**t, **{"ml_score": s["score"], "ml_tier": s["tier"]}})

    # Detail table — only last 30 for compactness
    print("=" * 110)
    print("DETAIL — last 30 closed trades (chronological)")
    print("=" * 110)
    hdr = f"{'closed_at':19s} {'strategy':16s} {'side':4s} {'R':>6s} {'pnl_usdt':>10s} {'ml_tier':7s} {'ml_score':>8s} {'ml_right?':10s}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows[-30:]:
        win = r["R"] > 0
        # ML "right" if HIGH+win or LOW/SKIP+loss; MED is neutral
        if r["ml_tier"] == "HIGH" and win:
            verdict = "✅ caught"
        elif r["ml_tier"] in ("LOW", "SKIP") and not win:
            verdict = "✅ caught"
        elif r["ml_tier"] == "HIGH" and not win:
            verdict = "❌ false +"
        elif r["ml_tier"] in ("LOW", "SKIP") and win:
            verdict = "❌ false -"
        else:
            verdict = "·neutral·"
        closed_str = (r["closed_at"] or "")[:19]
        print(f"{closed_str:19s} {r['strategy'][:16]:16s} {r['side'][:4]:4s} "
              f"{r['R']:>+6.2f} {r['pnl']:>+10.2f} {r['ml_tier']:7s} "
              f"{r['ml_score']:>8.3f} {verdict:10s}")

    # ─── Aggregate by tier ─────────────────────────────────────────
    from collections import defaultdict
    by_tier = defaultdict(list)
    for r in rows:
        by_tier[r["ml_tier"]].append(r)

    print()
    print("=" * 110)
    print("AGGREGATE — outcome by ML tier (the headline result)")
    print("=" * 110)
    print(f"{'tier':6s} {'n':>5s} {'wins':>5s} {'losses':>7s} {'winrate':>8s} "
          f"{'avg_R':>8s} {'sum_R':>8s} {'sum_pnl':>10s}")
    print("-" * 70)
    for tier in ["HIGH", "MED", "LOW", "SKIP"]:
        bucket = by_tier.get(tier, [])
        if not bucket:
            continue
        n = len(bucket)
        wins = sum(1 for x in bucket if x["R"] > 0)
        losses = n - wins
        wr = wins / n if n else 0
        avg_R = sum(x["R"] for x in bucket) / n if n else 0
        sum_R = sum(x["R"] for x in bucket)
        sum_pnl = sum(x["pnl"] for x in bucket)
        print(f"{tier:6s} {n:>5d} {wins:>5d} {losses:>7d} {wr*100:>7.1f}% "
              f"{avg_R:>+8.2f} {sum_R:>+8.2f} {sum_pnl:>+10.2f}")

    # ─── Filter scenarios ──────────────────────────────────────────
    total_R = sum(r["R"] for r in rows)
    total_pnl = sum(r["pnl"] for r in rows)
    print()
    print("=" * 110)
    print("WHAT-IF — if we had GATED at different thresholds")
    print("=" * 110)
    print(f"Baseline (took every trade):  n={len(rows)}  "
          f"sumR={total_R:+.2f}  sumPnL=${total_pnl:+.2f}")
    print()
    for cutoff in ["HIGH", "MED", "LOW"]:
        keep = [r for r in rows
                if r["ml_tier"] in (["HIGH"] if cutoff == "HIGH"
                                    else ["HIGH", "MED"] if cutoff == "MED"
                                    else ["HIGH", "MED", "LOW"])]
        skipped = [r for r in rows if r not in keep]
        if not keep:
            print(f"  gate={cutoff:5s}: would have skipped EVERYTHING")
            continue
        k_R = sum(r["R"] for r in keep)
        k_pnl = sum(r["pnl"] for r in keep)
        s_R = sum(r["R"] for r in skipped)
        s_pnl = sum(r["pnl"] for r in skipped)
        wins = sum(1 for r in keep if r["R"] > 0)
        wr = wins / len(keep) if keep else 0
        print(f"  gate={cutoff:5s}: kept {len(keep):>3d} ({wr*100:>5.1f}% wr)  "
              f"sumR={k_R:+6.2f}  sumPnL=${k_pnl:+8.2f}   "
              f"|  skipped {len(skipped):>3d}  sumR={s_R:+6.2f}  "
              f"sumPnL_avoided=${-s_pnl:+8.2f}")

    # ─── Loss-specific breakdown ───────────────────────────────────
    losses = [r for r in rows if r["R"] < 0]
    print()
    print("=" * 110)
    print(f"LOSS BREAKDOWN — {len(losses)} losing trades, where did ML rank them?")
    print("=" * 110)
    loss_by_tier = defaultdict(int)
    for r in losses:
        loss_by_tier[r["ml_tier"]] += 1
    for tier in ["HIGH", "MED", "LOW", "SKIP"]:
        n = loss_by_tier.get(tier, 0)
        pct = n / len(losses) * 100 if losses else 0
        bar = "█" * int(pct / 3)
        print(f"  {tier:6s} {n:>3d} losses ({pct:>5.1f}%) {bar}")
    print()
    # And same for wins
    wins = [r for r in rows if r["R"] > 0]
    print(f"WIN BREAKDOWN — {len(wins)} winning trades, where did ML rank them?")
    win_by_tier = defaultdict(int)
    for r in wins:
        win_by_tier[r["ml_tier"]] += 1
    for tier in ["HIGH", "MED", "LOW", "SKIP"]:
        n = win_by_tier.get(tier, 0)
        pct = n / len(wins) * 100 if wins else 0
        bar = "█" * int(pct / 3)
        print(f"  {tier:6s} {n:>3d} wins   ({pct:>5.1f}%) {bar}")


if __name__ == "__main__":
    main()
