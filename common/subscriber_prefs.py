"""
subscriber_prefs.py — Per-subscriber filter preferences.

Each token can have preferences:
  min_score          — only deliver alerts at or above this score
  strategies         — whitelist of strategy names (empty = all)
  symbols            — whitelist of symbol names (empty = all)
  webhook_url        — outbound URL for push delivery (optional)
  notification_mode  — "channel" (default), "dm", "webhook", "off"
  timezone           — for daily summary timing (default UTC)

Stored in /home/ubuntu/common/subscriber_prefs.json, keyed by token prefix.
Bot reads this when deciding whether to deliver an alert to a given
subscriber.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, Optional

sys.path.insert(0, "/home/ubuntu/common")

import threading

PREFS_FILE = "/home/ubuntu/common/subscriber_prefs.json"

# Bug #24 fix: lock RMW cycles to prevent concurrent set_prefs/reset
# from losing each other's writes.
_PREFS_LOCK = threading.RLock()

# Discord Gateway-style intents: subscriber declares which event types
# they want delivered. Empty list = all.
DEFAULTS = {
    "min_score": 7,
    "strategies": [],     # empty = all
    "symbols": [],        # empty = all
    "event_types": [],    # NEW: empty = all (alert + every close type)
    "webhook_url": "",
    "webhook_secret": "", # NEW: HMAC signing secret for outbound delivery
    "notification_mode": "channel",
    "timezone": "UTC",
}


def _load() -> Dict[str, dict]:
    if not os.path.exists(PREFS_FILE):
        return {}
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: Dict[str, dict]) -> None:
    tmp = PREFS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    os.replace(tmp, PREFS_FILE)


def get(token: str) -> dict:
    """Return prefs for token, merged with defaults."""
    key = token[:12]   # prefix-keyed for short URLs/keys
    d = _load()
    rec = d.get(key, {})
    return {**DEFAULTS, **rec}


def set_prefs(token: str, **updates) -> dict:
    """Update one or more preference keys. Returns new merged dict.
    Bug #24 fix: lock-protected RMW."""
    key = token[:12]
    invalid = [k for k in updates if k not in DEFAULTS]
    if invalid:
        raise KeyError(f"unknown prefs: {invalid}")
    with _PREFS_LOCK:
        d = _load()
        rec = d.get(key, {})
        rec.update(updates)
        d[key] = rec
        _save(d)
        return {**DEFAULTS, **rec}


def reset(token: str) -> bool:
    """Remove all custom prefs for a token. Bug #24 fix: locked RMW."""
    key = token[:12]
    with _PREFS_LOCK:
        d = _load()
        if key in d:
            d.pop(key)
            _save(d)
            return True
        return False


def matches_alert(token: str, alert: dict) -> bool:
    """Should this subscriber receive this alert?

    Discord-Gateway-style intent filter: also checks event_types whitelist.
    Empty event_types list = all event types delivered."""
    p = get(token)
    # Event-type intent filter (Upgrade #4)
    if p.get("event_types"):
        ev = alert.get("event", "alert")
        status = alert.get("status", "")
        if ev == "close" and status:
            ev = f"close_{status.lower()}"
        if ev not in p["event_types"]:
            return False
    score = alert.get("score")
    if score is not None:
        try:
            if int(score) < int(p["min_score"]):
                return False
        except (ValueError, TypeError):
            pass
    if p["strategies"]:
        if alert.get("system") not in p["strategies"]:
            return False
    if p["symbols"]:
        if alert.get("symbol") not in p["symbols"]:
            return False
    return True


def list_all() -> Dict[str, dict]:
    """List all subscribers with custom prefs (admin only)."""
    return _load()


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "get":
        print(json.dumps(get(sys.argv[2]), indent=2, default=str))
    elif len(sys.argv) >= 4 and sys.argv[1] == "set":
        # set <token> <key=val>...
        updates = {}
        for arg in sys.argv[3:]:
            if "=" in arg:
                k, v = arg.split("=", 1)
                if k in ("min_score",):
                    updates[k] = int(v)
                elif k in ("strategies", "symbols"):
                    updates[k] = [s.strip() for s in v.split(",") if s.strip()]
                else:
                    updates[k] = v
        print(json.dumps(set_prefs(sys.argv[2], **updates), indent=2,
                         default=str))
    elif len(sys.argv) >= 2 and sys.argv[1] == "list":
        print(json.dumps(list_all(), indent=2, default=str))
    else:
        print(json.dumps(DEFAULTS, indent=2, default=str))
