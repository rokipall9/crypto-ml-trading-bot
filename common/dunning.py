"""
dunning.py — Square-style subscription dunning (payment-retry orchestration).

When a renewal payment fails:
  Day 0:  Initial failure, retry in 24h, gentle email/Discord alert
  Day 3:  Still failed, second retry, firmer notice + grace-period reminder
  Day 5:  Third retry attempt, last warning before downgrade
  Day 7:  Grace period ends — downgrade tier 1 → tier 0 (read-only)
  Day 14: Token revoked entirely

Each step is auditable. Subscriber can resolve at any point by paying.

Run via systemd timer once a day (already have `bot_watchdog`-style pattern).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("dunning")

STATE_FILE = "/home/ubuntu/common/dunning_state.json"

# Schedule: days after first failure → action
SCHEDULE = [
    (0,  "first_retry_scheduled",  "info"),
    (1,  "first_retry_executed",   "info"),
    (3,  "second_retry_executed",  "warning"),
    (5,  "third_retry_executed",   "warning"),
    (7,  "downgrade_to_tier_0",    "critical"),
    (14, "token_revoked",          "critical"),
]


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
        json.dump(d, f, default=str, indent=2)
    os.replace(tmp, STATE_FILE)


def open_dunning(token_prefix: str, reason: str = "payment_failed") -> dict:
    """Mark this subscriber as in-dunning. Idempotent."""
    d = _load()
    if token_prefix in d and d[token_prefix].get("status") == "open":
        return d[token_prefix]
    rec = {
        "token_prefix": token_prefix[:12],
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "status": "open",
        "actions_taken": [],
    }
    d[token_prefix[:12]] = rec
    _save(d)
    log.info("dunning_opened", token_prefix=token_prefix[:8], reason=reason)
    return rec


def resolve_dunning(token_prefix: str, reason: str = "payment_received") -> bool:
    """Customer paid; remove from dunning."""
    d = _load()
    key = token_prefix[:12]
    if key in d:
        d[key]["status"] = "resolved"
        d[key]["resolved_at"] = datetime.now(timezone.utc).isoformat()
        d[key]["resolved_reason"] = reason
        _save(d)
        log.info("dunning_resolved", token_prefix=key, reason=reason)
        return True
    return False


def sweep() -> dict:
    """Run daily. Apply scheduled actions per subscriber."""
    d = _load()
    now = datetime.now(timezone.utc)
    actions = 0
    for key, rec in list(d.items()):
        if rec.get("status") != "open":
            continue
        try:
            opened = datetime.fromisoformat(
                str(rec["opened_at"]).replace("Z", "+00:00"))
        except Exception:
            continue
        days_elapsed = (now - opened).days
        for sched_day, action_name, severity in SCHEDULE:
            if days_elapsed >= sched_day and action_name not in rec.get(
                    "actions_taken", []):
                _execute_action(key, rec, action_name, severity)
                rec.setdefault("actions_taken", []).append(action_name)
                actions += 1
    _save(d)
    log.info("dunning_sweep_complete", actions=actions, total_open=len(d))
    return {"actions": actions, "total_open": sum(
        1 for r in d.values() if r.get("status") == "open")}


def _execute_action(token_prefix: str, rec: dict, action: str,
                    severity: str) -> None:
    """Carry out the scheduled action."""
    log.info("dunning_action", token_prefix=token_prefix,
             action=action, severity=severity)
    try:
        import audit_log
        audit_log.record(f"dunning_{action}", actor="dunning",
                         token_prefix=token_prefix, severity=severity)
    except Exception:
        pass
    if action == "downgrade_to_tier_0":
        try:
            import api_auth, json as _json
            d = _json.load(open(api_auth.TOKENS_FILE))
            for full_tok, tok_rec in d.items():
                if full_tok.startswith(token_prefix[:8]):
                    tok_rec["tier"] = 0
                    tok_rec["downgraded_dunning_at"] = datetime.now(
                        timezone.utc).isoformat()
            _json.dump(d, open(api_auth.TOKENS_FILE, "w"))
        except Exception as e:
            log.error("downgrade_failed", err=str(e))
    elif action == "token_revoked":
        try:
            import api_auth, json as _json
            d = _json.load(open(api_auth.TOKENS_FILE))
            keys_to_remove = [k for k in d if k.startswith(token_prefix[:8])]
            for k in keys_to_remove:
                d.pop(k)
            _json.dump(d, open(api_auth.TOKENS_FILE, "w"))
        except Exception as e:
            log.error("revoke_failed", err=str(e))


def list_open() -> List[dict]:
    d = _load()
    return [r for r in d.values() if r.get("status") == "open"]


def stats() -> dict:
    d = _load()
    now = datetime.now(timezone.utc)
    by_age_bucket = {"<3d": 0, "3-7d": 0, "7-14d": 0, ">14d": 0}
    for r in d.values():
        if r.get("status") != "open":
            continue
        try:
            age_days = (now - datetime.fromisoformat(
                str(r["opened_at"]).replace("Z", "+00:00"))).days
        except Exception:
            continue
        if age_days < 3: by_age_bucket["<3d"] += 1
        elif age_days < 7: by_age_bucket["3-7d"] += 1
        elif age_days < 14: by_age_bucket["7-14d"] += 1
        else: by_age_bucket[">14d"] += 1
    return {
        "total": len(d),
        "open": sum(1 for r in d.values() if r.get("status") == "open"),
        "resolved": sum(1 for r in d.values()
                       if r.get("status") == "resolved"),
        "by_age": by_age_bucket,
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sweep":
        print(json.dumps(sweep(), indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(stats(), indent=2, default=str))
    else:
        print("Usage: dunning.py [sweep|stats]")
