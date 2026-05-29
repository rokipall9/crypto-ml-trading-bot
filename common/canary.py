"""
canary.py — Self-monitoring tripwire.

Runs every 5 min via systemd. Checks the meta-health of the system itself:
  1. Discord API directly reachable
  2. webhook_queue dead-letter count is below threshold
  3. status_server /health/check is OK

If any check fails, writes /home/ubuntu/common/CANARY_FAILED with details.
bot_watchdog reads this file every 2 min and posts an alert via DIRECT
webhook (bypassing webhook_queue, in case that's the failing component).
When all checks pass, the file is removed.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("canary")

CANARY_FAIL_FILE = "/home/ubuntu/common/CANARY_FAILED"
DEAD_LETTER_THRESHOLD = 5


def check_discord_reachable() -> Tuple[bool, str]:
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/gateway",
            headers={"User-Agent": "srs-canary/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return (r.status == 200, f"http {r.status}")
    except Exception as e:
        return (False, str(e))


def check_webhook_queue() -> Tuple[bool, dict]:
    try:
        import webhook_queue
        s = webhook_queue.stats()
        ok = s["dead_letter"] < DEAD_LETTER_THRESHOLD
        return (ok, s)
    except Exception as e:
        return (False, {"err": str(e)})


def check_status_server() -> Tuple[bool, str]:
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:8080/health/check", timeout=5) as r:
            data = json.loads(r.read())
        ok = data.get("status") in ("healthy", "degraded")
        return (ok, data.get("status", "unknown"))
    except Exception as e:
        return (False, str(e))


def main() -> int:
    failures = []

    ok, info = check_discord_reachable()
    if not ok:
        failures.append({"check": "discord_api", "info": info})
        log.error("discord_api_unreachable", info=info)

    ok, info = check_webhook_queue()
    if not ok:
        failures.append({"check": "webhook_queue", "info": info})
        log.error("webhook_queue_unhealthy", info=info)

    ok, info = check_status_server()
    if not ok:
        failures.append({"check": "status_server", "info": info})
        log.error("status_server_unhealthy", info=info)

    if failures:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "failures": failures,
        }
        with open(CANARY_FAIL_FILE, "w", encoding="utf-8") as f:
            json.dump(rec, f, default=str, indent=2)
        return 1
    elif os.path.exists(CANARY_FAIL_FILE):
        os.remove(CANARY_FAIL_FILE)
        log.info("canary_recovered")

    log.info("canary_ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
