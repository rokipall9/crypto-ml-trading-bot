"""
srs_logger.py — Structured JSON-lines logger with daily rotation.

One source of truth across all bots. Replaces ad-hoc print() calls.

Usage:
    from srs_logger import get_logger
    log = get_logger("bot_name")
    log.info("scan started", strategy="BREAKOUT", count=42)
    log.error("api fail", err=str(e))

Files: /home/ubuntu/common/logs/<name>.YYYYMMDD.jsonl
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

LOG_DIR = "/home/ubuntu/common/logs"
os.makedirs(LOG_DIR, exist_ok=True)

_LOCK = threading.Lock()
_FH_CACHE: dict = {}


def _path_for(name: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return os.path.join(LOG_DIR, f"{name}.{day}.jsonl")


def _write(name: str, level: str, msg: str, **kw: Any) -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "lvl": level,
        "src": name,
        "msg": msg,
        **kw,
    }
    line = json.dumps(rec, default=str) + "\n"
    p = _path_for(name)
    with _LOCK:
        fh = _FH_CACHE.get(p)
        if fh is None:
            for old_p, old_fh in list(_FH_CACHE.items()):
                if old_p != p:
                    try: old_fh.close()
                    except Exception: pass
                    _FH_CACHE.pop(old_p, None)
            fh = open(p, "a", encoding="utf-8")
            _FH_CACHE[p] = fh
        fh.write(line)
        fh.flush()
    sys.stdout.write(f"[{level}] {name}: {msg} {kw if kw else ''}\n")
    sys.stdout.flush()


class _Logger:
    def __init__(self, name: str): self.name = name
    def info(self, msg, **kw):  _write(self.name, "INFO",  msg, **kw)
    def warn(self, msg, **kw):  _write(self.name, "WARN",  msg, **kw)
    def error(self, msg, **kw): _write(self.name, "ERROR", msg, **kw)
    def debug(self, msg, **kw): _write(self.name, "DEBUG", msg, **kw)


def get_logger(name: str) -> _Logger:
    return _Logger(name)


def prune(days: int = 14) -> int:
    """Delete log files older than N days. Returns count removed."""
    if not os.path.isdir(LOG_DIR):
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for f in os.listdir(LOG_DIR):
        full = os.path.join(LOG_DIR, f)
        try:
            if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
                os.remove(full); removed += 1
        except Exception:
            pass
    return removed


if __name__ == "__main__":
    log = get_logger("test")
    log.info("hello", x=1, y="abc")
    log.warn("danger")
    print(f"Pruned {prune()} old logs")
