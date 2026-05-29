"""
bot_watchdog.py — Auto-recover from silent bot deaths.

Run via systemd timer every 2 minutes. If heartbeat is stale (>5min),
restart cryptobot.service and post an alert to Discord.

Setup:
  /etc/systemd/system/bot_watchdog.service
  /etc/systemd/system/bot_watchdog.timer
  sudoers entry: ubuntu ALL=(root) NOPASSWD: /bin/systemctl restart cryptobot.service
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger
from heartbeat import age_seconds

log = get_logger("watchdog")

STALE_LIMIT = 300  # 5 minutes
SERVICE = "cryptobot.service"
RESTART_LOG = "/home/ubuntu/common/watchdog_restarts.jsonl"

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass

WEBHOOK = (os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
           or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())


def post_alert(title: str, msg: str, color: int = 0xFFA500) -> None:
    if not WEBHOOK:
        log.warn("no_webhook_configured")
        return
    payload = {
        "username": "🐕 Watchdog",
        "embeds": [{
            "title": title,
            "description": msg,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Auto-monitored · bot_watchdog"},
        }],
    }
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "srs-watchdog/1.0"})
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:
        log.error("webhook_fail", err=str(e))


def main():
    age = age_seconds()
    if age == float("inf"):
        log.warn("no_heartbeat_yet")
        return
    if age <= STALE_LIMIT:
        log.debug("heartbeat_fresh", age=round(age, 1))
        return

    log.error("heartbeat_stale", age=round(age, 1), limit=STALE_LIMIT)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "service": SERVICE,
        "stale_age_sec": round(age, 1),
    }
    with open(RESTART_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

    # Capture incident bundle BEFORE restart (so logs reflect failure state)
    bundle_path = ""
    bundle_summary = ""
    try:
        import incident_bundle
        bundle_path = incident_bundle.capture(
            reason=f"heartbeat_stale_{age:.0f}s",
            service=SERVICE)
        bundle_summary = incident_bundle.summarize(bundle_path)
        log.info("bundle_captured", path=bundle_path)
    except Exception as e:
        log.warn("bundle_capture_failed", err=str(e))

    try:
        subprocess.run(["sudo", "/bin/systemctl", "restart", SERVICE],
                       check=True, timeout=30)
        log.info("restarted", service=SERVICE)
        # Audit
        try:
            import audit_log
            audit_log.record("bot_auto_restart", actor="watchdog",
                             stale_age_sec=age, bundle=bundle_path)
        except Exception:
            pass
        # Build alert body with incident summary
        alert_body = (
            f"`{SERVICE}` heartbeat stale **{age:.0f}s** (>{STALE_LIMIT}s) "
            f"— restarted automatically.")
        if bundle_summary:
            # Discord description limit ~4096 chars, embed total ~6000
            alert_body += "\n\n" + bundle_summary[:3500]
        post_alert("⚠️  Bot Auto-Restart", alert_body, color=0xFFA500)
    except Exception as e:
        log.error("restart_fail", err=str(e))
        post_alert(
            "❌  Bot Restart FAILED",
            f"Could not restart `{SERVICE}`: `{e}` — manual intervention "
            f"required.\n\n{bundle_summary[:3500]}",
            color=0xFF4444,
        )


if __name__ == "__main__":
    main()
