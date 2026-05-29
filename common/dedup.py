"""
dedup.py — Idempotent alert deduplication.

Bots call dedup.seen(key) BEFORE posting. Returns True if seen within
ttl (skip post). Atomic write, auto-prunes expired entries.

Use: key = dedup.make_key(bot_name, symbol, opened_at, system)
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Dict

CACHE_FILE = "/home/ubuntu/common/dedup_cache.json"
TTL_SECONDS = 6 * 3600  # 6 hours covers a typical 4H bar cycle + buffer


def _load() -> Dict[str, float]:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: Dict[str, float]) -> None:
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, CACHE_FILE)


def make_key(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def seen(key: str, ttl: int = TTL_SECONDS) -> bool:
    """True if key was seen in last ttl seconds. Otherwise marks seen, returns False."""
    now = time.time()
    d = _load()
    # Prune expired
    d = {k: v for k, v in d.items() if v > now - ttl}
    if key in d:
        _save(d)
        return True
    d[key] = now
    _save(d)
    return False


def reset() -> int:
    if not os.path.exists(CACHE_FILE):
        return 0
    n = len(_load())
    _save({})
    return n


if __name__ == "__main__":
    d = _load()
    now = time.time()
    print(f"cache: {CACHE_FILE}")
    print(f"  entries: {len(d)}")
    print(f"  oldest: {(now - min(d.values())):.0f}s ago" if d else "  empty")
