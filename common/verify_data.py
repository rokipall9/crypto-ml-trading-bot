"""
verify_data.py — Data integrity verifier for forward_results.jsonl.

Pytest covers code; this covers DATA. Run periodically (cron, CI, or
on-demand) to catch:
  - Malformed JSON lines (already skipped by reader, but counted here)
  - Orphan alerts: event=alert with no matching event=close after N days
  - Closes without an open: event=close with no prior alert in same trade
  - R values outside sanity range (-5R to +10R)
  - Duplicate trade ids (if id field present)
  - Time travel: exit_time before opened_at

Exit code 0 = clean, 1 = issues found.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("verify_data")

LEDGER = "/home/ubuntu/common/forward_results.jsonl"
ORPHAN_DAYS = 7
R_MIN = -5.0
R_MAX = 10.0


def _parse_ts(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def verify() -> dict:
    issues: Dict[str, List] = defaultdict(list)
    by_trade_id: Dict[str, list] = defaultdict(list)
    open_alerts: Dict[str, dict] = {}   # trade_id → alert record
    closes_seen: Dict[str, dict] = {}
    bad_lines = 0
    total_lines = 0

    if not os.path.exists(LEDGER):
        return {"clean": True, "issues": {}, "total_records": 0,
                "valid_records": 0, "alerts": 0, "closes": 0,
                "note": "ledger does not exist yet"}

    with open(LEDGER, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            total_lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                bad_lines += 1
                issues["malformed_json"].append({"line": n,
                                                 "preview": line[:80]})
                continue
            tid = (rec.get("id") or rec.get("trade_id")
                   or f"{rec.get('bot','?')}|{rec.get('system','?')}|"
                      f"{rec.get('opened_at','?')}")
            event = rec.get("event")
            r = rec.get("r")

            if event == "alert":
                if tid in open_alerts:
                    issues["duplicate_alert_id"].append(
                        {"id": tid, "line": n})
                else:
                    open_alerts[tid] = rec
            elif event == "close":
                if tid in closes_seen:
                    issues["duplicate_close_id"].append(
                        {"id": tid, "line": n})
                else:
                    closes_seen[tid] = rec
                # Sanity check R
                if r is not None:
                    try:
                        rv = float(r)
                        if rv < R_MIN or rv > R_MAX:
                            issues["r_out_of_range"].append(
                                {"id": tid, "r": rv, "line": n})
                    except Exception:
                        issues["r_not_numeric"].append(
                            {"id": tid, "r": r, "line": n})
                # Time travel check
                opened = _parse_ts(rec.get("opened_at"))
                exited = _parse_ts(rec.get("exit_time"))
                if opened and exited and exited < opened:
                    issues["time_travel"].append(
                        {"id": tid, "opened": rec.get("opened_at"),
                         "exit": rec.get("exit_time"), "line": n})

    # Orphan alerts: alert without close after ORPHAN_DAYS
    cutoff = datetime.now(timezone.utc) - timedelta(days=ORPHAN_DAYS)
    for tid, alert in open_alerts.items():
        if tid in closes_seen:
            continue
        opened = _parse_ts(alert.get("opened_at"))
        if opened and opened < cutoff:
            issues["orphan_alert"].append(
                {"id": tid, "opened_at": alert.get("opened_at"),
                 "age_days": (datetime.now(timezone.utc) - opened).days})

    # Closes without an open
    for tid in closes_seen:
        if tid not in open_alerts:
            issues["close_without_open"].append({"id": tid})

    summary = {
        "clean": all(len(v) == 0 for v in issues.values()),
        "total_records": total_lines,
        "valid_records": total_lines - bad_lines,
        "alerts": len(open_alerts),
        "closes": len(closes_seen),
        "issues": {k: v for k, v in issues.items() if v},
    }
    return summary


def main():
    res = verify()
    log.info("verify_complete", clean=res["clean"],
             total=res["total_records"],
             alerts=res["alerts"], closes=res["closes"],
             issue_types=list(res["issues"].keys()))
    print(json.dumps(res, indent=2, default=str))
    return 0 if res["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
