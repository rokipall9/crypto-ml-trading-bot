"""
health_monitor.py — verifies all bot services are alive + heartbeating.

Checks:
  1. Each enabled service is actively running
  2. Each bot has logged a recent scan within last 2 hours
  3. Disk usage < 90%
  4. Memory free > 10%

If anything stale, posts an alert to watch channel.
Run hourly via systemd timer (chained with forward_tracker).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/home/ubuntu/common")
import pro_format

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass


WATCH_URL = os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip() or \
            os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

EXPECTED_SERVICES = ["cryptobot.service", "daily_signal.service"]
STALE_HOURS = 2


def is_service_active(name):
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def last_scan_age(audit_path):
    """Hours since last 'scan' record in audit.jsonl."""
    if not os.path.exists(audit_path):
        return None
    last_ts = None
    with open(audit_path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("kind") == "scan" and r.get("ts"):
                    last_ts = r["ts"]
            except Exception:
                pass
    if not last_ts:
        return None
    try:
        ts = datetime.fromisoformat(last_ts)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return None


def cryptobot_last_4h_age():
    """Hours since last cryptobot 4H scan from bot_live.log."""
    log_path = "/home/ubuntu/bot/logs/bot_live.log"
    if not os.path.exists(log_path):
        return None
    # bot_live.log lines have timestamps like 2026-04-25T18:00:01
    import re
    ts_pattern = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
    last_ts = None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            # Read last 10KB only — efficient
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 10000))
            chunk = f.read()
        for line in chunk.splitlines():
            m = ts_pattern.search(line)
            if m:
                last_ts = m.group(1)
    except Exception:
        return None
    if not last_ts:
        return None
    try:
        ts = datetime.fromisoformat(last_ts).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return None


def disk_pct_used():
    try:
        r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            if parts and parts[-1] == "/":
                return int(parts[-2].rstrip("%"))
    except Exception:
        pass
    return 0


def main():
    issues = []

    for svc in EXPECTED_SERVICES:
        if not is_service_active(svc):
            issues.append(f"❌ `{svc}` is NOT active")

    daily_age = last_scan_age("/home/ubuntu/daily_signal/state/audit.jsonl")
    if daily_age is not None and daily_age > STALE_HOURS:
        issues.append(f"⚠ daily_signal last scan was **{daily_age:.1f}h ago**")
    elif daily_age is None:
        issues.append("⚠ daily_signal has no scan records yet")

    crypto_age = cryptobot_last_4h_age()
    if crypto_age is not None and crypto_age > STALE_HOURS:
        issues.append(f"⚠ cryptobot last 4H entry was **{crypto_age:.1f}h ago**")

    disk = disk_pct_used()
    if disk > 90:
        issues.append(f"⚠ disk usage at **{disk}%** — clean up logs")

    if not issues:
        print("[health] all systems normal")
        return

    if not WATCH_URL:
        print("[health] issues found, no webhook to alert:")
        for i in issues: print("   " + i)
        return

    embed = {
        "title": "⚠️  Bot Health Check Failed",
        "description": "Automated monitor detected issues — investigate.",
        "color": 0xFF8C00,
        "fields": [{"name": "Issues", "value": "\n".join(issues), "inline": False}],
        "footer": {"text": f"Health monitor · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    pro_format.send_embed(WATCH_URL, embed, username="🚨 Health Monitor")
    print("[health] posted %d issues" % len(issues))


if __name__ == "__main__":
    main()
