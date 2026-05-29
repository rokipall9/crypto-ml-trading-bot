"""
track_record.py — read forward_results.jsonl, compute per-strategy stats.

Used by alert embeds to show:
  "BREAKOUT live: 4 trades · 50% WR · +1.5R"

Builds visible track record over time, exactly what subscribers want.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Dict, Optional


LEDGER = "/home/ubuntu/common/forward_results.jsonl"


def _load_closes():
    if not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("event") == "close":
                    out.append(r)
            except Exception:
                pass
    return out


def by_strategy(strategy: str = None, bot: str = None) -> Dict:
    """Stats filtered by strategy/system name and/or bot."""
    closes = _load_closes()
    rows = [r for r in closes
            if (not strategy or r.get("system") == strategy)
            and (not bot or r.get("bot") == bot)]
    if not rows:
        return {"n": 0, "tp": 0, "sl": 0, "be": 0, "time": 0,
                "wr": 0, "total_r": 0, "label": "—"}
    tp = sum(1 for r in rows if r["status"] == "TP")
    sl = sum(1 for r in rows if r["status"] == "SL")
    be = sum(1 for r in rows if r.get("status") == "BE")
    time_ = sum(1 for r in rows if r.get("status") == "TIME")
    n = len(rows)
    total_r = sum(r.get("r", 0) for r in rows)
    wr = tp / n * 100 if n else 0
    label = "%d trades · %.0f%% WR · %+.2fR" % (n, wr, total_r)
    return {"n": n, "tp": tp, "sl": sl, "be": be, "time": time_,
            "wr": wr, "total_r": total_r, "label": label}


def all_summary() -> Dict:
    """Overall summary across everything."""
    return by_strategy()


def per_strategy_table() -> Dict[str, Dict]:
    """Dict of {strategy: stats} for all strategies seen in ledger."""
    closes = _load_closes()
    grouped = defaultdict(list)
    for r in closes:
        key = "%s/%s" % (r.get("bot", "?"), r.get("system", "?"))
        grouped[key].append(r)

    out = {}
    for key, rows in grouped.items():
        tp = sum(1 for r in rows if r["status"] == "TP")
        n = len(rows)
        total_r = sum(r.get("r", 0) for r in rows)
        wr = tp / n * 100 if n else 0
        out[key] = {"n": n, "wr": wr, "total_r": total_r,
                    "label": "%d · %.0f%% · %+.2fR" % (n, wr, total_r)}
    return out
