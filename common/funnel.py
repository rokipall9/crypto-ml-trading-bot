"""
funnel.py — PostHog-style subscriber lifecycle funnel tracking.

Stages (in order):
  1. webhook_received        — Stripe/Lemon webhook hit
  2. token_issued            — token created
  3. first_api_call          — subscriber actually used the token
  4. first_alert_fired       — bot generated an alert that matched their filters
  5. first_close             — alert resolved (TP/SL/BE/TIME)
  6. webhook_url_registered  — subscriber set up their own outbound URL
  7. csv_export              — subscriber pulled a CSV
  8. signed_url_issued       — subscriber issued a pre-signed URL
  9. seven_day_active        — used API in 7+ different days
  10. paid_renewal           — extended past initial trial

For each subscriber: timestamp of first occurrence at each stage.
Operator sees in /api/admin/funnel exactly where subscribers drop off.

Used by /token issue, /api/v1/me, alert_gate, etc — instrumented at
key call sites.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("funnel")

FUNNEL_FILE = "/home/ubuntu/common/funnel.json"
_LOCK = threading.RLock()

STAGES = [
    "webhook_received",
    "token_issued",
    "first_api_call",
    "first_alert_fired",
    "first_close",
    "webhook_url_registered",
    "csv_export",
    "signed_url_issued",
    "seven_day_active",
    "paid_renewal",
]


def _load() -> Dict[str, dict]:
    if not os.path.exists(FUNNEL_FILE):
        return {}
    try:
        with open(FUNNEL_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: Dict[str, dict]) -> None:
    tmp = FUNNEL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, default=str)
    os.replace(tmp, FUNNEL_FILE)


def record(token_prefix: str, stage: str,
           extra: Optional[dict] = None) -> bool:
    """Record stage hit. Returns True if this is the FIRST time the
    subscriber reached this stage (worth celebrating in audit)."""
    if stage not in STAGES:
        log.warn("unknown_stage", stage=stage)
        return False
    if not token_prefix:
        return False
    key = token_prefix[:12]
    now = datetime.now(timezone.utc).isoformat()
    is_first = False
    with _LOCK:
        d = _load()
        rec = d.get(key, {"first_token_prefix": token_prefix[:8],
                          "stages": {}, "active_days": []})
        if stage not in rec["stages"]:
            rec["stages"][stage] = now
            is_first = True
        if extra:
            rec.setdefault("extra", {})[stage] = extra
        # Active-days tracking for 7-day milestone
        today = now[:10]
        if today not in rec.get("active_days", []):
            rec.setdefault("active_days", []).append(today)
            rec["active_days"] = rec["active_days"][-30:]   # keep 30d
            if (len(rec["active_days"]) >= 7
                    and "seven_day_active" not in rec["stages"]):
                rec["stages"]["seven_day_active"] = now
                is_first = True
        d[key] = rec
        _save(d)
    if is_first:
        log.info("funnel_milestone", token_prefix=token_prefix[:8],
                 stage=stage)
    return is_first


def stats() -> dict:
    """Per-stage subscriber count, conversion rates, dropoff."""
    d = _load()
    counts = {s: 0 for s in STAGES}
    for rec in d.values():
        for stage in rec.get("stages", {}):
            if stage in counts:
                counts[stage] += 1
    # Compute conversion: %% who reached stage N+1 of those who reached N
    conversion = {}
    for i in range(len(STAGES) - 1):
        s_from = STAGES[i]
        s_to = STAGES[i + 1]
        if counts[s_from] > 0:
            conversion[f"{s_from} → {s_to}"] = round(
                100 * counts[s_to] / counts[s_from], 1)
    return {
        "total_subscribers_in_funnel": len(d),
        "by_stage": counts,
        "conversion_pct": conversion,
    }


def for_subscriber(token_prefix: str) -> dict:
    key = token_prefix[:12]
    d = _load()
    return d.get(key, {"stages": {}, "active_days": []})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(stats(), indent=2, default=str))
    elif len(sys.argv) > 2 and sys.argv[1] == "record":
        record(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "first_api_call")
    else:
        print(f"Stages: {STAGES}")
