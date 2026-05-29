"""
alert_history.py — Historical alert query API.

Subscribers can query "show me all my BREAKOUT alerts in March with score≥8."
Powers the /api/v1/alerts subscriber endpoint.

Filters:
  strategy   — exact match (e.g. "BREAKOUT_4H")
  symbol     — exact match (e.g. "BTCUSDT")
  min_score  — only alerts with score >= this
  status     — "TP", "SL", "BE", "TIME", "OPEN" (open = no close yet)
  since      — ISO date or YYYY-MM-DD
  until      — ISO date or YYYY-MM-DD

Output: JSON list, joined alert + close events. Capped at 500 by default.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

sys.path.insert(0, "/home/ubuntu/common")
from ledger import read_all


def _parse_date(s: Optional[str]):
    if not s:
        return None
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        # YYYY-MM-DD form
        return datetime.fromisoformat(s + "T00:00:00+00:00")
    except Exception:
        return None


def _join_records() -> Dict[str, dict]:
    alerts: Dict[str, dict] = {}
    closes: Dict[str, dict] = {}
    for r in read_all():
        tid = (r.get("id") or r.get("trade_id")
               or f"{r.get('bot','?')}|{r.get('system','?')}|"
                  f"{r.get('opened_at','?')}")
        if r.get("event") == "alert":
            alerts[tid] = r
        elif r.get("event") == "close":
            closes[tid] = r
    joined: Dict[str, dict] = {}
    for tid, alert in alerts.items():
        rec = dict(alert)
        if tid in closes:
            close = closes[tid]
            rec["status"] = close.get("status", "?")
            rec["exit_price"] = close.get("exit_price")
            rec["exit_time"] = close.get("exit_time")
            rec["r"] = close.get("r")
        else:
            rec["status"] = "OPEN"
        joined[tid] = rec
    # Also include closes whose alert is missing (shouldn't happen often)
    for tid, close in closes.items():
        if tid not in joined:
            joined[tid] = dict(close, status=close.get("status", "?"))
    return joined


def query(strategy: Optional[str] = None,
          symbol: Optional[str] = None,
          min_score: Optional[int] = None,
          status: Optional[str] = None,
          since: Optional[str] = None,
          until: Optional[str] = None,
          limit: int = 500) -> List[dict]:
    since_dt = _parse_date(since)
    until_dt = _parse_date(until)
    out: List[dict] = []
    for rec in _join_records().values():
        if strategy and rec.get("system") != strategy:
            continue
        if symbol and rec.get("symbol") != symbol:
            continue
        if min_score is not None:
            try:
                if int(rec.get("score", 0)) < int(min_score):
                    continue
            except (ValueError, TypeError):
                continue
        if status and rec.get("status") != status:
            continue
        if since_dt or until_dt:
            ts = (_parse_date(rec.get("opened_at"))
                  or _parse_date(rec.get("exit_time")))
            if ts is None:
                continue
            if since_dt and ts < since_dt:
                continue
            if until_dt and ts > until_dt:
                continue
        out.append(rec)
    # Sort: most recent first
    out.sort(key=lambda r: r.get("opened_at") or r.get("exit_time") or "",
             reverse=True)
    return out[:limit]


def to_csv(rows: List[dict]) -> bytes:
    """Render rows as CSV bytes."""
    if not rows:
        return b"event,system,symbol,side,score,entry,sl,tp,exit_price,status,r,opened_at,exit_time\n"
    fields = ["event", "system", "symbol", "side", "score",
              "entry", "sl", "tp", "exit_price", "status", "r",
              "opened_at", "exit_time"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--strategy"); p.add_argument("--symbol")
    p.add_argument("--min-score", type=int)
    p.add_argument("--status")
    p.add_argument("--since"); p.add_argument("--until")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--csv", action="store_true")
    args = p.parse_args()

    rows = query(strategy=args.strategy, symbol=args.symbol,
                 min_score=args.min_score, status=args.status,
                 since=args.since, until=args.until, limit=args.limit)
    if args.csv:
        sys.stdout.buffer.write(to_csv(rows))
    else:
        print(json.dumps({"items": rows, "count": len(rows)},
                         indent=2, default=str))
