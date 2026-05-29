"""
bot_metrics.py — Lightweight counters for the bot, exposed via /metrics.

The bot calls bot_metrics.inc("scans_total") and the value lives in
/home/ubuntu/common/bot_metrics.json. status_server reads this on /metrics
and merges into the Prometheus exposition.

Atomic write via os.replace(). Counters survive restart.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict

METRICS_FILE = "/home/ubuntu/common/bot_metrics.json"
_LOCK = threading.Lock()


def _load() -> dict:
    if not os.path.exists(METRICS_FILE):
        return {}
    try:
        with open(METRICS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: dict) -> None:
    d["updated_at"] = time.time()
    tmp = METRICS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, default=str)
    os.replace(tmp, METRICS_FILE)


def inc(key: str, by: int = 1) -> None:
    """Increment a counter."""
    with _LOCK:
        d = _load()
        d[key] = int(d.get(key, 0)) + by
        _save(d)


def set_gauge(key: str, value: float) -> None:
    """Set a gauge value."""
    with _LOCK:
        d = _load()
        d[key] = float(value)
        _save(d)


def observe(key: str, value: float, max_samples: int = 100) -> None:
    """Append a sample to a sliding-window list (for distributions)."""
    with _LOCK:
        d = _load()
        samples = d.get(key, [])
        if not isinstance(samples, list):
            samples = []
        samples.append(float(value))
        d[key] = samples[-max_samples:]
        _save(d)


def snapshot() -> dict:
    return _load()


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=2, default=str))
