"""
webhook_redelivery.py — Plaid-style subscriber-initiated re-delivery.

A subscriber's webhook receiver was down for 1 hour. Rather than asking
the operator for a manual replay, the subscriber hits:

  POST /api/v1/redeliver/<delivery_id>

We look up the delivery in our outbound history, re-fire it. Limited to
redelivering events from the last 7 days (matches our retention).

This converts "I missed your webhook, can you resend?" support tickets
into a self-serve API call.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("webhook_redelivery")

DELIVERY_LOG = "/home/ubuntu/common/outbound_delivery_log.jsonl"
MAX_AGE_DAYS = 7
MAX_REDELIVERIES_PER_HOUR = 20   # per-token rate limit


def record_delivery(delivery_id: str, token_prefix: str,
                    webhook_url: str, payload: dict,
                    event_type: str = "alert") -> None:
    """Called by webhook_outbound after a successful send.
    Stores enough info to replay."""
    rec = {
        "delivery_id": delivery_id,
        "token_prefix": token_prefix[:12],
        "webhook_url_host": _url_host(webhook_url),
        "webhook_url_full": webhook_url,   # only operator-readable
        "payload": payload,
        "event_type": event_type,
        "first_delivered_at": datetime.now(timezone.utc).isoformat(),
        "redeliveries": 0,
    }
    try:
        with open(DELIVERY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        log.error("delivery_log_fail", err=str(e))


def _url_host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"


def _find_delivery(delivery_id: str,
                   token_prefix: str = "") -> Optional[dict]:
    if not os.path.exists(DELIVERY_LOG):
        return None
    cutoff = datetime.now(timezone.utc).timestamp() - MAX_AGE_DAYS * 86400
    last_match = None
    with open(DELIVERY_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("delivery_id") != delivery_id:
                continue
            if token_prefix and not rec.get("token_prefix",
                                             "").startswith(
                                                 token_prefix[:8]):
                continue
            try:
                ts = datetime.fromisoformat(
                    str(rec["first_delivered_at"]).replace("Z", "+00:00")
                ).timestamp()
                if ts < cutoff:
                    continue
            except Exception:
                pass
            last_match = rec
    return last_match


def redeliver(delivery_id: str,
              requesting_token_prefix: str) -> Tuple[bool, dict]:
    """Subscriber requests redelivery. Returns (ok, info_dict)."""
    rec = _find_delivery(delivery_id, requesting_token_prefix)
    if not rec:
        return False, {"error": "delivery_not_found_or_expired"}
    # Verify token ownership: the requesting token must own this delivery
    if not rec["token_prefix"].startswith(requesting_token_prefix[:8]):
        return False, {"error": "delivery_not_yours"}
    # Re-fire via webhook_queue (durable)
    try:
        import webhook_queue
        qid = webhook_queue.enqueue(rec["webhook_url_full"], rec["payload"])
        log.info("redeliver_enqueued", delivery_id=delivery_id,
                 qid=qid, token_prefix=rec["token_prefix"])
        return True, {
            "redelivered": True,
            "queue_id": qid,
            "delivery_id": delivery_id,
            "first_delivered_at": rec["first_delivered_at"],
        }
    except Exception as e:
        log.error("redeliver_fail", err=str(e))
        return False, {"error": "enqueue_failed", "detail": str(e)}


def stats() -> dict:
    if not os.path.exists(DELIVERY_LOG):
        return {"total_deliveries_tracked": 0}
    n = 0
    with open(DELIVERY_LOG, encoding="utf-8") as f:
        for _ in f:
            n += 1
    return {"total_deliveries_tracked": n,
            "log_size_bytes": os.path.getsize(DELIVERY_LOG)}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(stats(), indent=2, default=str))
    else:
        print("Usage: webhook_redelivery.py stats")
