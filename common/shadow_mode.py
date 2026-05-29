"""
shadow_mode.py — Run candidate strategies without firing real alerts.

When testing a new strategy variant ("BREAKOUT_4H_v2"), record what it
WOULD have done without actually alerting subscribers:

    shadow_mode.record_decision(
        strategy="BREAKOUT_4H_v2",
        decision={"action": "alert", "score": 8, "side": "LONG",
                  "entry": 79473, "sl": 78900, "tp": 81500, "ts": "..."},
    )

After enough data accumulates (forward_tracker fills outcomes), compare:
    shadow_mode.compare("BREAKOUT_4H", "BREAKOUT_4H_v2")
    # → win-rate / total-R / Sharpe delta

This is the SAFE A/B testing layer. Subscribers see only the live
strategy's alerts; the shadow strategy's decisions are recorded
silently for later analysis.

Storage: /home/ubuntu/common/shadow_results.jsonl (separate from
forward_results.jsonl to keep production data clean).
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("shadow_mode")

SHADOW_FILE = "/home/ubuntu/common/shadow_results.jsonl"


def record_decision(strategy: str, decision: dict,
                    bar_ts: str = "") -> None:
    """Append a shadow-mode decision. decision is a dict mirroring what
    a real alert would contain."""
    rec = {
        "strategy": strategy,
        "ts": datetime.now(timezone.utc).isoformat(),
        "bar_ts": bar_ts,
        **decision,
    }
    with open(SHADOW_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
        f.flush()
    log.info("shadow_decision", strategy=strategy,
             action=decision.get("action"))


def read_all() -> List[dict]:
    if not os.path.exists(SHADOW_FILE):
        return []
    out = []
    with open(SHADOW_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def stats(strategy: str) -> Dict:
    """Compute shadow stats for a strategy. Outcomes are filled in by
    forward_tracker if the bar_ts matches a closed trade in the live ledger."""
    rows = [r for r in read_all() if r.get("strategy") == strategy]
    n_decisions = len(rows)
    n_alerts = sum(1 for r in rows if r.get("action") == "alert")
    n_with_outcome = sum(1 for r in rows if r.get("status") in
                         ("TP", "SL", "BE", "TIME"))
    if n_with_outcome == 0:
        return {"strategy": strategy, "n_decisions": n_decisions,
                "n_alerts": n_alerts, "n_resolved": 0,
                "wr_pct": 0, "total_r": 0, "note": "no outcomes yet"}
    tp = sum(1 for r in rows if r.get("status") == "TP")
    total_r = sum(float(r.get("r", 0)) for r in rows)
    return {
        "strategy": strategy,
        "n_decisions": n_decisions,
        "n_alerts": n_alerts,
        "n_resolved": n_with_outcome,
        "wr_pct": round(tp / n_with_outcome * 100, 1),
        "total_r": round(total_r, 2),
        "avg_r": round(total_r / n_with_outcome, 3),
    }


def compare(strategy_a: str, strategy_b: str) -> Dict:
    """Side-by-side comparison."""
    sa = stats(strategy_a); sb = stats(strategy_b)
    out = {"a": sa, "b": sb}
    if sa.get("n_resolved", 0) > 0 and sb.get("n_resolved", 0) > 0:
        out["delta_wr_pct"] = sb["wr_pct"] - sa["wr_pct"]
        out["delta_total_r"] = sb["total_r"] - sa["total_r"]
        out["delta_avg_r"] = sb["avg_r"] - sa["avg_r"]
        out["verdict"] = ("B better" if sb["total_r"] > sa["total_r"]
                          else ("A better" if sa["total_r"] > sb["total_r"]
                                else "tie"))
    else:
        out["verdict"] = "insufficient data"
    return out


def list_strategies() -> List[str]:
    rows = read_all()
    return sorted(set(r.get("strategy") for r in rows if r.get("strategy")))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "stats":
        print(json.dumps(stats(sys.argv[2]), indent=2, default=str))
    elif len(sys.argv) >= 4 and sys.argv[1] == "compare":
        print(json.dumps(compare(sys.argv[2], sys.argv[3]),
                         indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "list":
        for s in list_strategies(): print(f"  {s}")
    else:
        print("Usage:")
        print("  shadow_mode.py list")
        print("  shadow_mode.py stats <strategy>")
        print("  shadow_mode.py compare <strategy_a> <strategy_b>")
