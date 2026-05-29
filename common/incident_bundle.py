"""
incident_bundle.py — Capture system state when an incident occurs.

Used by bot_watchdog when it auto-restarts. Saves a JSON bundle with:
  - reason
  - last 200 lines of bot log
  - heartbeat snapshot
  - ledger size + last close
  - webhook queue stats
  - active bans
  - canary status
  - recent audit log entries

Bundles persist at /home/ubuntu/common/incidents/incident_<ts>.json.
The watchdog includes a summary in its Discord alert.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List

sys.path.insert(0, "/home/ubuntu/common")

INCIDENT_DIR = "/home/ubuntu/common/incidents"
BOT_LOG = "/home/ubuntu/bot/logs/bot_live.log"


def _tail(path: str, n: int = 200) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        try:
            f.seek(-n * 200, 2)  # ~200 chars per line; rough seek
        except OSError:
            f.seek(0)
        data = f.read().decode("utf-8", errors="replace")
    return data.splitlines()[-n:]


def capture(reason: str, **extra) -> str:
    """Write a bundle. Returns path."""
    os.makedirs(INCIDENT_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(INCIDENT_DIR, f"incident_{ts}.json")

    bundle: Dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "extra": extra,
    }

    try:
        import heartbeat
        bundle["heartbeat"] = heartbeat.read()
        bundle["heartbeat_age_s"] = heartbeat.age_seconds()
    except Exception as e:
        bundle["heartbeat_err"] = str(e)

    try:
        from ledger import LEDGER, count_lines
        bundle["ledger"] = {
            "path": LEDGER,
            "exists": os.path.exists(LEDGER),
            "size": (os.path.getsize(LEDGER) if os.path.exists(LEDGER) else 0),
            "lines": count_lines(),
        }
    except Exception as e:
        bundle["ledger_err"] = str(e)

    try:
        import webhook_queue
        bundle["webhook_queue"] = webhook_queue.stats()
    except Exception as e:
        bundle["webhook_queue_err"] = str(e)

    try:
        import ip_ban
        bundle["active_bans"] = ip_ban.get_active_bans()
    except Exception as e:
        bundle["bans_err"] = str(e)

    try:
        import bot_metrics
        bundle["bot_metrics"] = bot_metrics.snapshot()
    except Exception as e:
        bundle["bot_metrics_err"] = str(e)

    try:
        import audit_log
        bundle["recent_audit"] = audit_log.tail(20)
    except Exception as e:
        bundle["audit_err"] = str(e)

    bundle["bot_log_tail_200"] = _tail(BOT_LOG, 200)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, default=str, indent=2)
    return path


def summarize(bundle_path: str, max_log_lines: int = 8) -> str:
    """Return a short human-readable summary suitable for a Discord embed."""
    if not os.path.exists(bundle_path):
        return "_(bundle missing)_"
    with open(bundle_path, encoding="utf-8") as f:
        b = json.load(f)
    lines = []
    lines.append(f"**Reason:** {b.get('reason', '?')}")
    hb = b.get("heartbeat", {})
    lines.append(f"**Heartbeat:** {b.get('heartbeat_age_s', '?')}s old, "
                 f"PID {hb.get('pid', '?')}")
    ledg = b.get("ledger", {})
    lines.append(f"**Ledger:** {ledg.get('lines', 0)} lines, "
                 f"{ledg.get('size', 0):,}B")
    wq = b.get("webhook_queue", {})
    lines.append(f"**Webhook queue:** "
                 f"pending {wq.get('pending', 0)} · "
                 f"dead {wq.get('dead_letter', 0)}")
    if b.get("bot_log_tail_200"):
        tail = "\n".join(b["bot_log_tail_200"][-max_log_lines:])
        # Truncate to fit Discord field limit (1024 chars)
        if len(tail) > 800:
            tail = tail[-800:]
        lines.append(f"**Last log:**\n```\n{tail}\n```")
    return "\n".join(lines)


def list_recent(n: int = 10) -> List[str]:
    if not os.path.isdir(INCIDENT_DIR):
        return []
    files = sorted(os.listdir(INCIDENT_DIR))
    return [os.path.join(INCIDENT_DIR, f) for f in files[-n:]]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "capture":
        path = capture("manual_test", source="cli")
        print(f"wrote {path}")
        print(summarize(path))
    elif len(sys.argv) > 1 and sys.argv[1] == "list":
        for p in list_recent():
            print(p)
    else:
        print("Usage: incident_bundle.py [capture|list]")
