"""
feature_flags.py — Runtime experiment gates.

Gate experimental code paths behind named flags. Operators flip flags
without restarts (config_reload integration). Subscribers can be opted
into specific flags by token-prefix targeting.

Storage: /home/ubuntu/common/feature_flags.json (atomic writes).

Flag types:
  - boolean global:  {"new_scoring": true}
  - per-subscriber:  {"new_scoring": {"prefixes": ["abc12345", "def67890"]}}
  - percentage roll: {"new_scoring": {"rollout_pct": 25}}  (deterministic by token hash)

Usage:
    from feature_flags import is_enabled

    if is_enabled("new_scoring", token=user_token):
        score = new_scoring_v2(market_data)
    else:
        score = legacy_scoring(market_data)

    # Operator-side:
    feature_flags.set("new_scoring", True)
    feature_flags.set("new_scoring", {"prefixes": ["abc12345"]})
    feature_flags.set("new_scoring", {"rollout_pct": 50})
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("feature_flags")

FLAGS_FILE = "/home/ubuntu/common/feature_flags.json"

# Bug #19/#26/#27 fix: cache by (path, mtime_ns, size).
_FLAGS_CACHE: Dict[str, Any] = {"key": None, "data": None}


def _flags_stat_key(path: str):
    st = os.stat(path)
    return (path, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)),
            st.st_size)


def _load() -> Dict[str, Any]:
    if not os.path.exists(FLAGS_FILE):
        return {}
    try:
        cur_key = _flags_stat_key(FLAGS_FILE)
        if (_FLAGS_CACHE["key"] == cur_key
                and _FLAGS_CACHE["data"] is not None):
            return dict(_FLAGS_CACHE["data"])
        with open(FLAGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _FLAGS_CACHE["key"] = cur_key
        _FLAGS_CACHE["data"] = dict(data)
        return data
    except Exception:
        return {}


def _save(d: Dict[str, Any]) -> None:
    tmp = FLAGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str, sort_keys=True)
    os.replace(tmp, FLAGS_FILE)
    try:
        _FLAGS_CACHE["key"] = _flags_stat_key(FLAGS_FILE)
        _FLAGS_CACHE["data"] = dict(d)
    except Exception:
        pass


def _hash_pct(token: str) -> int:
    """Deterministic 0–99 bucket for stable rollout."""
    return int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % 100


def is_enabled(flag: str,
               token: Optional[str] = None,
               default: bool = False) -> bool:
    """Check if flag is enabled for given token (or globally)."""
    flags = _load()
    val = flags.get(flag, default)

    # Boolean global
    if isinstance(val, bool):
        return val
    # Numeric global (legacy)
    if isinstance(val, (int, float)):
        return bool(val)

    # Dict — targeted rollout
    if isinstance(val, dict):
        if token:
            prefixes = val.get("prefixes") or []
            if any(token.startswith(p) for p in prefixes):
                return True
            rollout = val.get("rollout_pct")
            if rollout is not None:
                try:
                    return _hash_pct(token) < int(rollout)
                except (TypeError, ValueError):
                    return default
        # No token but dict flag — fall back to "enabled" key if present
        if "enabled" in val:
            return bool(val["enabled"])
        return default

    return default


def set_flag(flag: str, value: Any) -> None:
    """Set a flag value (any JSON-serializable type)."""
    d = _load()
    d[flag] = value
    d.setdefault("_meta", {})[flag] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save(d)
    log.info("flag_set", flag=flag, value=value)


def remove_flag(flag: str) -> bool:
    d = _load()
    if flag in d:
        d.pop(flag)
        if "_meta" in d and flag in d["_meta"]:
            d["_meta"].pop(flag)
        _save(d)
        log.info("flag_removed", flag=flag)
        return True
    return False


def list_flags() -> Dict[str, Any]:
    d = _load()
    # Strip _meta from output
    return {k: v for k, v in d.items() if k != "_meta"}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        print(json.dumps(list_flags(), indent=2, default=str))
    elif len(sys.argv) > 2 and sys.argv[1] == "get":
        flag = sys.argv[2]
        token = sys.argv[3] if len(sys.argv) > 3 else None
        print(json.dumps({"flag": flag, "token": token,
                          "enabled": is_enabled(flag, token=token)},
                         indent=2, default=str))
    elif len(sys.argv) > 2 and sys.argv[1] == "set":
        # Parse value: try bool/int/json
        flag = sys.argv[2]
        raw = sys.argv[3] if len(sys.argv) > 3 else "true"
        if raw.lower() == "true":   value = True
        elif raw.lower() == "false": value = False
        else:
            try: value = json.loads(raw)
            except Exception: value = raw
        set_flag(flag, value)
        print(f"set {flag} = {value}")
    elif len(sys.argv) > 2 and sys.argv[1] == "remove":
        print(f"removed: {remove_flag(sys.argv[2])}")
    else:
        print("Usage:")
        print("  feature_flags.py list")
        print("  feature_flags.py get <flag> [token]")
        print("  feature_flags.py set <flag> <value>")
        print("  feature_flags.py remove <flag>")
