"""
data_health.py — Detect SILENT recording failures.

The watchdog catches process death. This catches the worse case:
process is alive, heartbeat fresh, but the recording pipeline silently
stopped writing closes (e.g. ledger path bug, broken paper trader, etc.)

Logic: if bot heartbeat is fresh (<5min) AND ledger mtime is >48h old,
that's a silent recording failure → alert webhook.

Throttled: alerts max once per 24h.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger
import heartbeat

log = get_logger("data_health")

LEDGER = "/home/ubuntu/common/forward_results.jsonl"
STATE_FILE = "/home/ubuntu/common/data_health_state.json"
STALE_HOURS = 48
ALERT_THROTTLE = 86400  # 1 day

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass

WEBHOOK = (os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
           or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


def post_alert(title: str, body: str, color: int = 0xFFA500) -> None:
    if not WEBHOOK:
        log.warn("no_webhook")
        return
    payload = {
        "username": "🩺 Data Health",
        "embeds": [{
            "title": title,
            "description": body,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Auto-monitored · data_health"},
        }],
    }
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "srs-data-health/1.0"})
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:
        log.error("webhook_fail", err=str(e))


def main():
    bot_age = heartbeat.age_seconds()
    bot_alive = bot_age < 300

    if os.path.exists(LEDGER):
        ledger_mtime_hours = (time.time() - os.path.getmtime(LEDGER)) / 3600
        ledger_size = os.path.getsize(LEDGER)
    else:
        ledger_mtime_hours = float("inf")
        ledger_size = 0

    log.info("check", bot_age_s=round(bot_age, 1) if bot_age != float("inf") else None,
             ledger_mtime_h=round(ledger_mtime_hours, 1)
             if ledger_mtime_hours != float("inf") else None,
             ledger_size=ledger_size)

    state = _load_state()
    last_alerted = state.get("last_alerted", 0)

    if bot_alive and ledger_mtime_hours > STALE_HOURS:
        if time.time() - last_alerted > ALERT_THROTTLE:
            log.error("silent_recording_failure",
                      bot_age=bot_age, ledger_mtime_h=ledger_mtime_hours)
            post_alert(
                "⚠️  Silent Recording Failure?",
                (f"Bot heartbeat is **fresh** ({bot_age:.0f}s) but "
                 f"`forward_results.jsonl` hasn't been written in "
                 f"**{ledger_mtime_hours:.1f} hours** "
                 f"(threshold: {STALE_HOURS}h).\n\n"
                 f"Either no scanner is firing, or the recording pipeline "
                 f"is silently broken. Check the audit pipeline + "
                 f"forward_tracker.timer."),
                color=0xFFA500,
            )
            state["last_alerted"] = time.time()
            _save_state(state)
        else:
            log.info("alert_throttled",
                     since_last=int(time.time() - last_alerted))


if __name__ == "__main__":
    main()
