"""
health_check.py — Deep diagnostic checks for /health/check.

Returns structured JSON with the status of each subsystem:
  - bot_heartbeat:    is the bot alive?
  - ledger_writable:  can we append to forward_results.jsonl?
  - disk_space:       enough free space?
  - webhook_queue:    pending vs dead-letter?
  - tokens_file:      readable + secure perms?

Used by: /health/check (full JSON), /health (simple "ok"/"degraded").
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from typing import Dict, Tuple

sys.path.insert(0, "/home/ubuntu/common")


def check_bot_heartbeat() -> Tuple[bool, dict]:
    try:
        import heartbeat
        age = heartbeat.age_seconds()
        ok = age < 300
        return ok, {"ok": ok, "age_s": round(age, 1)
                    if age != float("inf") else None}
    except Exception as e:
        return False, {"ok": False, "err": str(e)}


def check_ledger_writable() -> Tuple[bool, dict]:
    try:
        from ledger import LEDGER
        d = os.path.dirname(LEDGER)
        if not os.path.isdir(d):
            return False, {"ok": False, "err": f"dir missing: {d}"}
        if not os.access(d, os.W_OK):
            return False, {"ok": False, "err": "dir not writable"}
        if os.path.exists(LEDGER) and not os.access(LEDGER, os.W_OK):
            return False, {"ok": False, "err": "ledger not writable"}
        return True, {"ok": True, "path": LEDGER,
                      "size": (os.path.getsize(LEDGER)
                               if os.path.exists(LEDGER) else 0)}
    except Exception as e:
        return False, {"ok": False, "err": str(e)}


def check_disk_space(min_gb: float = 1.0) -> Tuple[bool, dict]:
    try:
        st = shutil.disk_usage("/home/ubuntu")
        free_gb = st.free / (1024 ** 3)
        ok = free_gb >= min_gb
        return ok, {"ok": ok, "free_gb": round(free_gb, 2),
                    "total_gb": round(st.total / (1024 ** 3), 2)}
    except Exception as e:
        return False, {"ok": False, "err": str(e)}


def check_webhook_queue() -> Tuple[bool, dict]:
    try:
        import webhook_queue
        s = webhook_queue.stats()
        # Healthy if dead_letter < 5 AND pending < 50 (anything bigger
        # suggests delivery is broken)
        ok = s["dead_letter"] < 5 and s["pending"] < 50
        return ok, {"ok": ok, **s}
    except Exception as e:
        return False, {"ok": False, "err": str(e)}


def check_tokens_file() -> Tuple[bool, dict]:
    path = "/home/ubuntu/common/tokens.json"
    if not os.path.exists(path):
        return True, {"ok": True, "exists": False}
    try:
        mode = os.stat(path).st_mode & 0o777
        secure = mode == 0o600
        return secure, {"ok": secure, "mode": oct(mode),
                        "expected": "0o600"}
    except Exception as e:
        return False, {"ok": False, "err": str(e)}


def run_all() -> dict:
    t0 = time.time()
    checks = {
        "bot_heartbeat":   check_bot_heartbeat(),
        "ledger_writable": check_ledger_writable(),
        "disk_space":      check_disk_space(),
        "webhook_queue":   check_webhook_queue(),
        "tokens_file":     check_tokens_file(),
    }
    results = {k: v[1] for k, v in checks.items()}
    n_ok = sum(1 for v in checks.values() if v[0])
    n = len(checks)
    if n_ok == n:
        status = "healthy"
    elif n_ok >= n - 1:
        status = "degraded"
    else:
        status = "unhealthy"
    return {
        "status": status,
        "checks_passed": n_ok,
        "checks_total": n,
        "checks": results,
        "duration_ms": round((time.time() - t0) * 1000, 1),
    }


if __name__ == "__main__":
    print(json.dumps(run_all(), indent=2, default=str))
