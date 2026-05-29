"""
bot_runtime.py — Single import bringing together the production runtime
for any bot or strategy module.

Usage:
    from bot_runtime import log, queue, dedup, metrics, audit

    log.info("scan started", strategy="BREAKOUT")
    if dedup.seen(dedup.make_key("bot", "BTC", opened_at)):
        return
    queue.send(WEBHOOK_URL, payload, files=[("chart.png", png)])
    metrics.inc("alerts_fired_total")
    audit.record("alert_posted", actor="bot", strategy="BREAKOUT")

Replaces ad-hoc print() + direct urlopen() with the durable, audited,
deduplicated, instrumented stack built across rounds 1-11.
"""
from __future__ import annotations

import sys
from typing import List, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")

from srs_logger import get_logger
import webhook_queue
import dedup as _dedup_mod
import bot_metrics
import audit_log


# Default logger — callers can override via get_logger("their_name")
log = get_logger("bot")


class _Queue:
    """Durable Discord webhook delivery facade."""
    @staticmethod
    def send(webhook_url: str, payload: dict,
             files: Optional[List[Tuple[str, bytes]]] = None) -> str:
        """Enqueue for durable delivery (retry + dead-letter). Returns queue id."""
        return webhook_queue.enqueue(webhook_url, payload, files=files)

    @staticmethod
    def stats() -> dict:
        return webhook_queue.stats()


queue = _Queue()


class _Dedup:
    """Idempotent alert deduplication facade."""
    TTL_DEFAULT = 6 * 3600

    @staticmethod
    def make_key(*parts) -> str:
        return _dedup_mod.make_key(*parts)

    @staticmethod
    def seen(key: str, ttl: int = TTL_DEFAULT) -> bool:
        """True if key was seen within ttl. Otherwise marks seen, returns False."""
        return _dedup_mod.seen(key, ttl=ttl)


dedup = _Dedup()


class _Metrics:
    """Bot-side counter/gauge/histogram facade. Exposed via /metrics."""
    @staticmethod
    def inc(key: str, by: int = 1) -> None:
        bot_metrics.inc(key, by=by)

    @staticmethod
    def set_gauge(key: str, value: float) -> None:
        bot_metrics.set_gauge(key, value)

    @staticmethod
    def observe(key: str, value: float) -> None:
        bot_metrics.observe(key, value)

    @staticmethod
    def snapshot() -> dict:
        return bot_metrics.snapshot()


metrics = _Metrics()


class _Audit:
    """Audit-trail facade for admin/system actions."""
    @staticmethod
    def record(action: str, actor: str = "bot", **kw) -> None:
        audit_log.record(action, actor=actor, **kw)


audit = _Audit()


def health() -> dict:
    """Smoke probe — used by tests and /api/admin/runtime."""
    return {
        "log_name": log.name,
        "queue": queue.stats(),
        "metrics_keys": sorted(metrics.snapshot().keys()),
        "ready": True,
    }


__all__ = ["log", "queue", "dedup", "metrics", "audit", "health"]


if __name__ == "__main__":
    import json
    print(json.dumps(health(), default=str, indent=2))
    log.info("bot_runtime smoke", source="cli")
    print("OK")
