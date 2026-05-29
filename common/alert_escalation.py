"""
alert_escalation.py — Multi-tier critical alert escalation.

Standard Discord-webhook alerts (watchdog, data_health, canary) cover
single-failure cases. This module catches PATTERNS:
  - 3+ canary failures within 30 min  → escalate
  - Watchdog restarts ≥ 3 in 1 hour    → escalate
  - Webhook dead-letter > 50           → escalate
  - /health/check unhealthy > 30 min   → escalate

Escalation tiers (operator configures via env):
  Tier 1 (default): Discord watch channel  — already covered
  Tier 2 (this):    Discord ESCALATION channel + @here mention
  Tier 3 (future):  Email/SMS via outbound webhook (if configured)

State persisted at /home/ubuntu/common/escalation_state.json. Run via
systemd timer every 10 min. Self-throttles: max 1 escalation per
2 hours per rule.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger
import audit_log

log = get_logger("alert_escalation")

STATE_FILE = "/home/ubuntu/common/escalation_state.json"
RESTART_LOG = "/home/ubuntu/common/watchdog_restarts.jsonl"
THROTTLE_SEC = 2 * 3600

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass

ESCALATION_WEBHOOK = (os.environ.get("DISCORD_ESCALATION_WEBHOOK", "").strip()
                     or os.environ.get("DISCORD_ADMIN_WEBHOOK", "").strip()
                     or os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip())


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
        json.dump(s, f, default=str)
    os.replace(tmp, STATE_FILE)


def post_escalation(rule: str, title: str, body: str) -> bool:
    """Post to escalation webhook with @here mention."""
    if not ESCALATION_WEBHOOK:
        log.warn("no_escalation_webhook")
        return False
    payload = {
        "username": "🚨 ESCALATION",
        "content": "@here",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": [{
            "title": f"🚨  {title}",
            "description": body,
            "color": 0xFF0000,
            "footer": {"text": f"escalation rule: {rule}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    try:
        req = urllib.request.Request(
            ESCALATION_WEBHOOK, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "srs-escalation/1.0"})
        urllib.request.urlopen(req, timeout=8).read()
        audit_log.record("escalation_posted", actor="alert_escalation",
                         rule=rule, title=title)
        return True
    except Exception as e:
        log.error("post_fail", err=str(e))
        return False


def _check_canary_failures() -> Tuple[bool, str]:
    """Rule: ≥3 canary failures within 30 min → escalate."""
    canary_log = "/home/ubuntu/common/logs/canary.{}.jsonl".format(
        datetime.now(timezone.utc).strftime("%Y%m%d"))
    if not os.path.exists(canary_log):
        return False, ""
    cutoff = time.time() - 30 * 60
    failures = 0
    with open(canary_log, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("lvl") != "ERROR":
                    continue
                ts = datetime.fromisoformat(
                    r["ts"].replace("Z", "+00:00")).timestamp()
                if ts >= cutoff:
                    failures += 1
            except Exception:
                continue
    if failures >= 3:
        return True, f"canary failed {failures} times in last 30 min"
    return False, ""


def _check_restart_storm() -> Tuple[bool, str]:
    """Rule: ≥3 watchdog restarts within 1 hour → escalate."""
    if not os.path.exists(RESTART_LOG):
        return False, ""
    cutoff = time.time() - 3600
    restarts = 0
    with open(RESTART_LOG, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(
                    r["ts"].replace("Z", "+00:00")).timestamp()
                if ts >= cutoff:
                    restarts += 1
            except Exception:
                continue
    if restarts >= 3:
        return True, f"bot auto-restarted {restarts} times in last hour"
    return False, ""


def _check_dead_letter_buildup() -> Tuple[bool, str]:
    """Rule: webhook dead-letter > 50 → escalate."""
    try:
        import webhook_queue
        s = webhook_queue.stats()
        if s["dead_letter"] > 50:
            return True, (f"webhook dead-letter queue at {s['dead_letter']} "
                         f"(threshold 50)")
    except Exception:
        pass
    return False, ""


def _check_persistent_unhealthy() -> Tuple[bool, str]:
    """Rule: /health/check has been unhealthy/degraded > 30 min → escalate.
    Tracked via state file (simple heuristic: count consecutive checks)."""
    state = _load_state()
    consecutive = state.get("unhealthy_consecutive", 0)
    try:
        import health_check
        res = health_check.run_all()
        if res["status"] in ("unhealthy", "degraded"):
            consecutive += 1
        else:
            consecutive = 0
    except Exception:
        consecutive += 1
    state["unhealthy_consecutive"] = consecutive
    _save_state(state)
    # 3 consecutive 10-min checks = 30 min
    if consecutive >= 3:
        return True, f"/health/check non-healthy for ~{consecutive*10} min"
    return False, ""


RULES = [
    ("canary_storm", _check_canary_failures),
    ("restart_storm", _check_restart_storm),
    ("dead_letter_buildup", _check_dead_letter_buildup),
    ("persistent_unhealthy", _check_persistent_unhealthy),
]


def main() -> int:
    state = _load_state()
    now = time.time()
    fired = 0

    for rule_id, check in RULES:
        try:
            triggered, body = check()
        except Exception as e:
            log.error("rule_check_failed", rule=rule_id, err=str(e))
            continue
        if not triggered:
            continue
        last = state.get(f"last_fired:{rule_id}", 0)
        if now - last < THROTTLE_SEC:
            log.info("rule_throttled", rule=rule_id,
                     cooldown_remaining=int(THROTTLE_SEC - (now - last)))
            continue
        title = f"Pattern detected: {rule_id}"
        ok = post_escalation(rule_id, title, body)
        if ok:
            state[f"last_fired:{rule_id}"] = now
            fired += 1

    _save_state(state)
    log.info("escalation_run", fired=fired, rules_checked=len(RULES))
    return fired


if __name__ == "__main__":
    sys.exit(0 if main() == 0 else 1)
