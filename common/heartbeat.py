"""
heartbeat.py — Bot writes liveness signal to disk every 60s.

Read by bot_watchdog.py (cron) for auto-restart detection.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

HEARTBEAT_FILE = "/home/ubuntu/common/heartbeat.json"


def write(extra: Optional[dict] = None) -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "epoch": time.time(),
        "pid": os.getpid(),
    }
    if extra:
        rec.update(extra)
    tmp = HEARTBEAT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, default=str)
    os.replace(tmp, HEARTBEAT_FILE)  # atomic on POSIX


def read() -> dict:
    if not os.path.exists(HEARTBEAT_FILE):
        return {}
    try:
        with open(HEARTBEAT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def age_seconds() -> float:
    rec = read()
    if not rec.get("epoch"):
        return float("inf")
    return time.time() - float(rec["epoch"])


if __name__ == "__main__":
    print(json.dumps(read(), indent=2, default=str))
    print(f"age: {age_seconds():.1f}s")
