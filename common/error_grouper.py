"""
error_grouper.py — Sentry-style error fingerprinting + dedup.

Goal: when the same error fires 1000 times in 5 min, the operator gets
ONE Discord alert with "1000× occurrences" — not 1000 separate alerts.

Fingerprint = SHA256 of (component, error_type, top-frame-of-traceback).
Variants of the same error (different timestamps, different request IDs)
collapse onto a single fingerprint. Counter increments; alert posted on
the FIRST occurrence and then every Nth occurrence after.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import traceback
from typing import Dict, Optional

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("error_grouper")

STATE_FILE = "/home/ubuntu/common/error_groups.json"
TTL_SEC = 24 * 3600
ALERT_ON_OCCURRENCES = (1, 10, 100, 1000)  # alert at these milestones
_LOCK = threading.RLock()


def _load() -> Dict[str, dict]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: Dict[str, dict]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, default=str)
    os.replace(tmp, STATE_FILE)


def _fingerprint(component: str, exc: Exception) -> str:
    """Stable group-key based on (component, exception type, top frame)."""
    err_type = type(exc).__name__
    tb = traceback.extract_tb(exc.__traceback__)
    top_frame = ""
    if tb:
        f = tb[-1]
        top_frame = f"{os.path.basename(f.filename)}:{f.name}:{f.lineno}"
    raw = f"{component}|{err_type}|{top_frame}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def capture(component: str, exc: Exception,
            extra: Optional[dict] = None) -> dict:
    """Record an error occurrence. Returns the group record + alert decision.

    Returns: {"fingerprint", "occurrences", "should_alert", "first_seen"}.
    Caller is responsible for actually posting the alert when should_alert.
    """
    fp = _fingerprint(component, exc)
    now = time.time()
    with _LOCK:
        d = _load()
        # Lazy prune
        d = {k: v for k, v in d.items()
             if v.get("last_seen", 0) > now - TTL_SEC}
        rec = d.get(fp, {
            "fingerprint": fp,
            "component": component,
            "err_type": type(exc).__name__,
            "first_seen": now,
            "occurrences": 0,
            "last_message": "",
        })
        rec["occurrences"] += 1
        rec["last_seen"] = now
        rec["last_message"] = str(exc)[:200]
        if extra:
            rec["last_extra"] = {k: str(v)[:100] for k, v in extra.items()}

        should_alert = rec["occurrences"] in ALERT_ON_OCCURRENCES
        d[fp] = rec
        _save(d)

    log.warn("error_captured", fingerprint=fp,
             component=component, err_type=type(exc).__name__,
             occurrences=rec["occurrences"], should_alert=should_alert)

    return {**rec, "should_alert": should_alert}


def stats() -> dict:
    d = _load()
    now = time.time()
    live = {k: v for k, v in d.items()
            if v.get("last_seen", 0) > now - TTL_SEC}
    return {
        "total_groups": len(d),
        "live_groups": len(live),
        "top": sorted(live.values(),
                      key=lambda v: -v.get("occurrences", 0))[:10],
    }


if __name__ == "__main__":
    import json as _json
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(_json.dumps(stats(), indent=2, default=str))
    else:
        print("Usage: error_grouper.py stats")
