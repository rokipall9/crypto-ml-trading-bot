"""
escalation_policy.py — PagerDuty-style tiered escalation.

Replaces the single-channel alert_escalation with proper L1 → L2 → L3
escalation:
  L1 (immediate)    — Discord watch channel
  L2 (5 min unack)  — Discord ESCALATION channel + @here
  L3 (15 min unack) — External webhook (PagerDuty, Opsgenie, custom)

Acknowledgment: an operator clicks the Discord message reaction or
hits POST /api/admin/incidents/<id>/ack to stop escalation. Without
ack, escalation marches up the chain.

State persisted to /home/ubuntu/common/incidents_open.json.
Run via systemd timer every 1 minute.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger
import audit_log

log = get_logger("escalation_policy")

OPEN_INCIDENTS_FILE = "/home/ubuntu/common/incidents_open.json"
L2_AFTER_SEC = 5 * 60     # escalate to L2 after 5 min unacked
L3_AFTER_SEC = 15 * 60    # escalate to L3 after 15 min unacked

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass

L1_WEBHOOK = (os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip())
L2_WEBHOOK = (os.environ.get("DISCORD_ESCALATION_WEBHOOK", "").strip()
              or L1_WEBHOOK)
L3_WEBHOOK = (os.environ.get("PAGERDUTY_WEBHOOK", "").strip()
              or os.environ.get("OPSGENIE_WEBHOOK", "").strip())


def _load_open() -> Dict[str, dict]:
    if not os.path.exists(OPEN_INCIDENTS_FILE):
        return {}
    try:
        with open(OPEN_INCIDENTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_open(d: Dict[str, dict]) -> None:
    tmp = OPEN_INCIDENTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, default=str, indent=2)
    os.replace(tmp, OPEN_INCIDENTS_FILE)


def open_incident(incident_id: str, title: str, body: str,
                  severity: str = "warning") -> dict:
    """Create a new open incident. Triggers L1 immediately.
    Loop #9: idempotent — same incident_id while open returns existing
    record (no re-trigger of L1 webhook to avoid double-pinging)."""
    d = _load_open()
    if incident_id in d and d[incident_id].get("status") not in ("resolved",):
        return d[incident_id]
    rec = {
        "id": incident_id,
        "title": title,
        "body": body,
        "severity": severity,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "level": 1,
        "history": [{"level": 1, "at": time.time(),
                     "channel": "discord_watch"}],
    }
    d[incident_id] = rec
    _save_open(d)
    _post_l1(rec)
    audit_log.record("incident_opened", actor="escalation_policy",
                     incident_id=incident_id, severity=severity)
    return rec


def acknowledge(incident_id: str, actor: str = "operator") -> bool:
    """Stop escalation for this incident."""
    d = _load_open()
    if incident_id not in d:
        return False
    d[incident_id]["status"] = "acknowledged"
    d[incident_id]["acked_at"] = datetime.now(timezone.utc).isoformat()
    d[incident_id]["acked_by"] = actor
    _save_open(d)
    audit_log.record("incident_acked", actor=actor,
                     incident_id=incident_id)
    return True


def resolve(incident_id: str, actor: str = "operator") -> bool:
    """Mark resolved (auto-purged from open list on next sweep)."""
    d = _load_open()
    if incident_id not in d:
        return False
    d[incident_id]["status"] = "resolved"
    d[incident_id]["resolved_at"] = datetime.now(timezone.utc).isoformat()
    _save_open(d)
    audit_log.record("incident_resolved", actor=actor,
                     incident_id=incident_id)
    return True


def _post(webhook: str, title: str, body: str,
          color: int, level: int) -> bool:
    if not webhook:
        log.warn("no_webhook_configured", level=level)
        return False
    payload = {
        "username": f"🚨 SRS L{level}",
        "embeds": [{
            "title": title,
            "description": body,
            "color": color,
            "footer": {"text": f"escalation L{level}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    if level >= 2:
        payload["content"] = "@here"
        payload["allowed_mentions"] = {"parse": ["everyone"]}
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "srs-escalation/1.0"})
        urllib.request.urlopen(req, timeout=5).read()
        return True
    except Exception as e:
        log.error("post_fail", level=level, err=str(e))
        return False


def _post_l1(rec: dict) -> None:
    _post(L1_WEBHOOK, rec["title"], rec["body"], 0xFFA500, 1)


def _post_l2(rec: dict) -> None:
    body = (rec["body"] + f"\n\n_Unacknowledged for {L2_AFTER_SEC//60} min._"
            "\nAcknowledge: `/incident_ack " + rec["id"] + "`")
    _post(L2_WEBHOOK, rec["title"], body, 0xFF4444, 2)


def _post_l3(rec: dict) -> None:
    body = (rec["body"]
            + f"\n\n_Unacknowledged for {L3_AFTER_SEC//60} min — "
              "external on-call._")
    _post(L3_WEBHOOK or L2_WEBHOOK, rec["title"], body, 0xFF0000, 3)


def sweep() -> dict:
    """Called every minute. Promote open incidents that aren't acked.
    Loop #9: also auto-resolves incidents when /health/check has been
    healthy for 10 consecutive minutes (10 sweep cycles)."""
    d = _load_open()
    now = time.time()
    promoted = 0
    cleaned = 0
    # Auto-resolve: check current health, increment counter, resolve at 10
    try:
        import health_check
        hc = health_check.run_all()
        is_healthy = hc.get("status") == "healthy"
        for iid, rec in d.items():
            if rec["status"] != "open":
                continue
            consec = int(rec.get("consec_healthy", 0))
            if is_healthy:
                rec["consec_healthy"] = consec + 1
                if rec["consec_healthy"] >= 10:
                    rec["status"] = "resolved"
                    rec["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    rec["resolved_by"] = "auto"
                    audit_log.record("incident_auto_resolved",
                                     actor="escalation_policy",
                                     incident_id=iid)
            else:
                rec["consec_healthy"] = 0
    except Exception:
        pass
    for iid, rec in list(d.items()):
        if rec["status"] in ("acknowledged", "resolved"):
            # Resolved: purge after 1 hour
            if rec["status"] == "resolved":
                resolved_at = rec.get("resolved_at")
                try:
                    rt = datetime.fromisoformat(
                        str(resolved_at).replace("Z", "+00:00")).timestamp()
                    if now - rt > 3600:
                        d.pop(iid)
                        cleaned += 1
                except Exception:
                    pass
            continue
        try:
            opened_at = datetime.fromisoformat(
                str(rec["opened_at"]).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        age = now - opened_at
        cur_level = rec.get("level", 1)
        if age >= L3_AFTER_SEC and cur_level < 3:
            rec["level"] = 3
            rec["history"].append({"level": 3, "at": now,
                                   "channel": "external_pagerduty"})
            _post_l3(rec)
            promoted += 1
        elif age >= L2_AFTER_SEC and cur_level < 2:
            rec["level"] = 2
            rec["history"].append({"level": 2, "at": now,
                                   "channel": "discord_escalation"})
            _post_l2(rec)
            promoted += 1
    if promoted or cleaned:
        _save_open(d)
    log.info("sweep_complete", open_count=len(d),
             promoted=promoted, cleaned=cleaned)
    return {"open": len(d), "promoted": promoted, "cleaned": cleaned}


def list_open() -> List[dict]:
    d = _load_open()
    return [v for v in d.values() if v.get("status") != "resolved"]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sweep":
        print(json.dumps(sweep(), indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "open":
        print(json.dumps(list_open(), indent=2, default=str))
    elif len(sys.argv) > 2 and sys.argv[1] == "ack":
        print(f"acked: {acknowledge(sys.argv[2])}")
    elif len(sys.argv) > 2 and sys.argv[1] == "test":
        rec = open_incident(
            f"test_{int(time.time())}",
            "🧪 Test incident",
            "This is a test escalation. Will L1→L2→L3 in 5/15 min.",
            severity="warning")
        print(json.dumps(rec, indent=2, default=str))
    else:
        print("Usage: escalation_policy.py [sweep|open|ack <id>|test]")
