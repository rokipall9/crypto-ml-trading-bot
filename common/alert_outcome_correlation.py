"""
alert_outcome_correlation.py — Closed-loop on alert quality.

Reads forward_results.jsonl, joins alert events with close events by
trade id, and computes:
  - Win rate by score bucket           (does 8/10 actually beat 7/10?)
  - Mean R by score bucket
  - Win rate by strategy
  - Calibration table                  (predicted vs actual)
  - Sample-size warnings               (n < 5 → unreliable)

This is the data you need to know if your scoring model is well-calibrated
or if you're systematically over/under-rating setups.

Run:
  python3 alert_outcome_correlation.py        # JSON output
  python3 alert_outcome_correlation.py table  # ASCII table
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from typing import Dict

sys.path.insert(0, "/home/ubuntu/common")
from ledger import read_all


def _join_alerts_to_closes() -> Dict[str, dict]:
    """Match each close back to its alert. Returns dict trade_id → joined."""
    alerts = {}
    closes = {}
    for r in read_all():
        tid = (r.get("id") or r.get("trade_id")
               or f"{r.get('bot','?')}|{r.get('system','?')}|"
                  f"{r.get('opened_at','?')}")
        if r.get("event") == "alert":
            alerts[tid] = r
        elif r.get("event") == "close":
            closes[tid] = r
    joined = {}
    for tid, close in closes.items():
        alert = alerts.get(tid, {})
        joined[tid] = {**alert, **close}  # close overrides alert fields
    return joined


def _bucket_metrics(rows: list) -> dict:
    if not rows:
        return {"n": 0}
    n = len(rows)
    tp = sum(1 for r in rows if r.get("status") == "TP")
    sl = sum(1 for r in rows if r.get("status") == "SL")
    total_r = sum(float(r.get("r", 0)) for r in rows)
    return {
        "n": n,
        "tp": tp,
        "sl": sl,
        "wr_pct": round(tp / n * 100, 1),
        "total_r": round(total_r, 2),
        "avg_r": round(total_r / n, 3),
        "reliable": n >= 5,
    }


def correlate() -> dict:
    joined = _join_alerts_to_closes()

    by_score: Dict[int, list] = defaultdict(list)
    by_strategy: Dict[str, list] = defaultdict(list)
    by_score_strategy: Dict[str, list] = defaultdict(list)

    for tid, row in joined.items():
        score = row.get("score")
        strategy = row.get("system") or "unknown"
        if score is not None:
            try:
                s = int(score)
                by_score[s].append(row)
                by_score_strategy[f"{strategy}:s{s}"].append(row)
            except (ValueError, TypeError):
                pass
        by_strategy[strategy].append(row)

    out = {
        "total_resolved": len(joined),
        "by_score":    {str(k): _bucket_metrics(v)
                        for k, v in sorted(by_score.items())},
        "by_strategy": {k: _bucket_metrics(v)
                        for k, v in sorted(by_strategy.items())},
        "calibration_warning": [],
    }

    # Calibration check: higher scores should win more
    scores = sorted(by_score.keys())
    for i in range(len(scores) - 1):
        lo, hi = scores[i], scores[i + 1]
        lo_wr = (sum(1 for r in by_score[lo] if r.get("status") == "TP")
                 / len(by_score[lo]) * 100) if by_score[lo] else 0
        hi_wr = (sum(1 for r in by_score[hi] if r.get("status") == "TP")
                 / len(by_score[hi]) * 100) if by_score[hi] else 0
        if hi_wr < lo_wr - 5 and len(by_score[hi]) >= 5:
            out["calibration_warning"].append(
                f"Score {hi} winning less than score {lo} "
                f"({hi_wr:.1f}% vs {lo_wr:.1f}%) — model miscalibrated")

    return out


def print_table(data: dict) -> None:
    print(f"\nTotal resolved trades: {data['total_resolved']}\n")
    print("─── By Score ───")
    print(f"{'Score':<8}{'n':>4}{'WR%':>8}{'Avg R':>9}{'Total R':>10}  Note")
    for k, m in data["by_score"].items():
        if m["n"] == 0:
            continue
        note = "✓" if m["reliable"] else "n<5 (unreliable)"
        print(f"  {k:<6}{m['n']:>4}{m['wr_pct']:>7.1f}%"
              f"{m['avg_r']:>9.2f}{m['total_r']:>10.2f}  {note}")
    print()
    print("─── By Strategy ───")
    print(f"{'Strategy':<25}{'n':>4}{'WR%':>8}{'Avg R':>9}{'Total R':>10}")
    for k, m in data["by_strategy"].items():
        if m["n"] == 0:
            continue
        print(f"  {k:<23}{m['n']:>4}{m['wr_pct']:>7.1f}%"
              f"{m['avg_r']:>9.2f}{m['total_r']:>10.2f}")
    if data.get("calibration_warning"):
        print()
        print("─── ⚠️  Calibration Warnings ───")
        for w in data["calibration_warning"]:
            print(f"  • {w}")
    print()


if __name__ == "__main__":
    data = correlate()
    if len(sys.argv) > 1 and sys.argv[1] == "table":
        print_table(data)
    else:
        print(json.dumps(data, indent=2, default=str))
