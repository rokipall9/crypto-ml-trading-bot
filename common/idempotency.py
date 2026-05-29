"""
idempotency.py — Stripe-style Idempotency-Key middleware.

Caller sends `Idempotency-Key: <opaque>` on a mutating request.
We cache the response keyed on that string for 24h. Repeated requests
with the same key return the cached response — never re-execute.

Used by /api/webhooks/<provider> (Bug #17 follow-up + Stripe duplicate
event safety) and any other mutating endpoint we add later.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Optional, Tuple

CACHE_FILE = "/home/ubuntu/common/idempotency_cache.json"
TTL_SEC = 24 * 3600
_LOCK = threading.RLock()
_CACHE = {"key": None, "data": None}


def _now() -> float:
    return time.time()


def _load() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        st = os.stat(CACHE_FILE)
        cur_key = (CACHE_FILE,
                   getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)),
                   st.st_size)
        if _CACHE["key"] == cur_key and _CACHE["data"] is not None:
            return dict(_CACHE["data"])
        with open(CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        _CACHE["key"] = cur_key
        _CACHE["data"] = dict(d)
        return d
    except Exception:
        return {}


def _save(d: dict) -> None:
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, default=str)
    os.replace(tmp, CACHE_FILE)
    try:
        st = os.stat(CACHE_FILE)
        _CACHE["key"] = (CACHE_FILE,
                         getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)),
                         st.st_size)
        _CACHE["data"] = dict(d)
    except Exception:
        pass


def lookup(key: str) -> Optional[Tuple[int, dict]]:
    """Returns cached (status, body) for this idempotency key, or None."""
    if not key:
        return None
    with _LOCK:
        d = _load()
        rec = d.get(key)
        if not rec:
            return None
        if rec.get("expires_at", 0) < _now():
            d.pop(key)
            _save(d)
            return None
        return rec["status"], rec["body"]


def remember(key: str, status: int, body: dict) -> None:
    """Cache this response for 24h. Idempotency-Key replays return identical."""
    if not key:
        return
    with _LOCK:
        d = _load()
        # Prune expired (lazy)
        cutoff = _now()
        d = {k: v for k, v in d.items()
             if v.get("expires_at", 0) > cutoff}
        d[key] = {
            "status": status,
            "body": body,
            "expires_at": _now() + TTL_SEC,
        }
        _save(d)


def hash_request(method: str, path: str, body: bytes,
                 idem_key: str) -> str:
    """Stable hash for a (request, key) pair — used to detect that a
    SAME idempotency key was used with a DIFFERENT request body, which
    Stripe specifies must return 422."""
    h = hashlib.sha256()
    h.update(method.encode())
    h.update(b"\n"); h.update(path.encode())
    h.update(b"\n"); h.update(body)
    h.update(b"\n"); h.update(idem_key.encode())
    return h.hexdigest()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        d = _load()
        live = sum(1 for v in d.values() if v.get("expires_at", 0) > _now())
        print(json.dumps({"total": len(d), "live": live}, indent=2))
