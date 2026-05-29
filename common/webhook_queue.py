"""
webhook_queue.py — Durable Discord webhook delivery with retry.

Persistent on-disk queue + background drain thread + exponential backoff
that respects Discord's X-RateLimit-Reset-After. Failed deliveries after
MAX_RETRIES move to a dead-letter directory for manual replay.

Use:
    from webhook_queue import enqueue
    enqueue(webhook_url, payload, files=[("chart.png", png_bytes)])

Inspect:
    python3 webhook_queue.py            → stats
    python3 webhook_queue.py replay     → re-queue all dead-letter items
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("webhook_queue")

QUEUE_DIR    = "/home/ubuntu/common/webhook_queue"
DEAD_DIR     = "/home/ubuntu/common/webhook_dead"
MAX_RETRIES  = 5
BASE_BACKOFF = 2.0     # seconds — doubles each retry

os.makedirs(QUEUE_DIR, exist_ok=True)
os.makedirs(DEAD_DIR, exist_ok=True)

_drain_lock = threading.Lock()
_drain_thread: Optional[threading.Thread] = None


def enqueue(webhook_url: str,
            payload: Dict[str, Any],
            files: Optional[List[Tuple[str, bytes]]] = None) -> str:
    """Enqueue a webhook for durable delivery. Returns queue id."""
    qid = uuid.uuid4().hex
    rec = {
        "id": qid,
        "ts": datetime.now(timezone.utc).isoformat(),
        "url": webhook_url,
        "payload": payload,
        "files": [{"name": n, "size": len(b)} for n, b in (files or [])],
        "attempts": 0,
        "next_attempt": time.time(),
    }
    meta_path = os.path.join(QUEUE_DIR, qid + ".json")
    tmp = meta_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, default=str)
    os.replace(tmp, meta_path)
    if files:
        for name, blob in files:
            with open(os.path.join(QUEUE_DIR, f"{qid}_{name}"), "wb") as f:
                f.write(blob)
    log.info("enqueued", id=qid, files=len(files or []))
    _ensure_drain()
    return qid


def _send_one(rec: Dict[str, Any]) -> Tuple[bool, str, Optional[float]]:
    """Attempt one send. Returns (success, reason, retry_after_secs)."""
    qid = rec["id"]
    url = rec["url"]
    payload = rec["payload"]
    file_meta = rec.get("files", [])

    files: List[Tuple[str, bytes]] = []
    for fm in file_meta:
        path = os.path.join(QUEUE_DIR, f"{qid}_{fm['name']}")
        if os.path.exists(path):
            with open(path, "rb") as f:
                files.append((fm["name"], f.read()))

    try:
        if files:
            boundary = uuid.uuid4().hex
            body = bytearray()
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(b'Content-Disposition: form-data; name="payload_json"\r\n')
            body.extend(b"Content-Type: application/json\r\n\r\n")
            body.extend(json.dumps(payload).encode())
            body.extend(b"\r\n")
            for i, (name, blob) in enumerate(files):
                body.extend(f"--{boundary}\r\n".encode())
                body.extend(
                    f'Content-Disposition: form-data; name="files[{i}]"; '
                    f'filename="{name}"\r\n'.encode())
                body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
                body.extend(blob)
                body.extend(b"\r\n")
            body.extend(f"--{boundary}--\r\n".encode())
            req = urllib.request.Request(
                url, data=bytes(body),
                headers={"Content-Type":
                         f"multipart/form-data; boundary={boundary}",
                         "User-Agent": "srs-webhook-queue/1.0"})
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "srs-webhook-queue/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return (True, f"http {r.status}", None)
    except urllib.error.HTTPError as e:
        # Honour Discord rate limit
        retry_after = (e.headers.get("X-RateLimit-Reset-After")
                       or e.headers.get("Retry-After"))
        ra: Optional[float] = None
        if retry_after:
            try: ra = float(retry_after) + 0.5
            except Exception: pass
        # 4xx (except 429) is a permanent failure — don't retry
        if 400 <= e.code < 500 and e.code != 429:
            return (False, f"http {e.code}: {e.reason} (permanent)", None)
        return (False, f"http {e.code}: {e.reason}", ra)
    except Exception as e:
        return (False, f"exception: {e}", None)


def _drain():
    while True:
        try:
            files = sorted(f for f in os.listdir(QUEUE_DIR)
                           if f.endswith(".json"))
        except FileNotFoundError:
            break
        if not files:
            break
        now = time.time()
        worked = False
        for fn in files:
            meta = os.path.join(QUEUE_DIR, fn)
            try:
                with open(meta, encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception:
                continue
            if rec.get("next_attempt", 0) > now:
                continue
            worked = True
            ok, reason, retry_after = _send_one(rec)
            if ok:
                log.info("delivered", id=rec["id"], reason=reason,
                         attempts=rec["attempts"] + 1)
                _cleanup(rec)
            else:
                rec["attempts"] = rec.get("attempts", 0) + 1
                rec["last_reason"] = reason
                permanent = "permanent" in reason
                if permanent or rec["attempts"] >= MAX_RETRIES:
                    log.error("dead_letter", id=rec["id"],
                              attempts=rec["attempts"], reason=reason)
                    _move_to_dead(rec, reason)
                else:
                    backoff = (retry_after if retry_after
                               else BASE_BACKOFF * (2 ** (rec["attempts"] - 1)))
                    backoff = min(backoff, 120.0)  # cap 2min
                    rec["next_attempt"] = now + backoff
                    with open(meta + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(rec, f, default=str)
                    os.replace(meta + ".tmp", meta)
                    log.warn("retry_scheduled", id=rec["id"],
                             attempts=rec["attempts"],
                             backoff=round(backoff, 1), reason=reason)
        if not worked:
            time.sleep(2)
        else:
            time.sleep(0.5)


def _ensure_drain():
    global _drain_thread
    with _drain_lock:
        if _drain_thread is None or not _drain_thread.is_alive():
            _drain_thread = threading.Thread(target=_drain, daemon=True,
                                             name="webhook-drain")
            _drain_thread.start()


def _cleanup(rec: Dict[str, Any]):
    qid = rec["id"]
    paths = [os.path.join(QUEUE_DIR, qid + ".json")]
    for fm in rec.get("files", []):
        paths.append(os.path.join(QUEUE_DIR, f"{qid}_{fm['name']}"))
    for p in paths:
        try: os.remove(p)
        except FileNotFoundError: pass


def _move_to_dead(rec: Dict[str, Any], reason: str):
    """Bug #15 fix: redact the full webhook URL when persisting to
    dead-letter. Operator can still see hostname for diagnostic; the
    full URL (potentially sensitive subscriber endpoint) isn't on disk."""
    qid = rec["id"]
    rec_safe = dict(rec)
    full_url = rec.get("url", "")
    if full_url:
        try:
            from urllib.parse import urlparse
            host = urlparse(full_url).hostname or "?"
        except Exception:
            host = "?"
        url_hash = hashlib.sha256(full_url.encode()).hexdigest()[:16]
        rec_safe["url"] = f"https://{host}/<redacted:{url_hash}>"
    rec_safe["dead_reason"] = reason
    rec_safe["dead_at"] = datetime.now(timezone.utc).isoformat()
    dest = os.path.join(DEAD_DIR, qid + ".json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(rec_safe, f, default=str)
    _cleanup(rec)


def stats() -> Dict[str, int]:
    pending = len([f for f in os.listdir(QUEUE_DIR) if f.endswith(".json")])
    dead = len([f for f in os.listdir(DEAD_DIR) if f.endswith(".json")])
    return {"pending": pending, "dead_letter": dead}


def replay_dead() -> int:
    moved = 0
    for fn in os.listdir(DEAD_DIR):
        if fn.endswith(".json"):
            os.rename(os.path.join(DEAD_DIR, fn),
                      os.path.join(QUEUE_DIR, fn))
            moved += 1
    if moved:
        _ensure_drain()
    return moved


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "replay":
        print(f"Replayed {replay_dead()} dead-letter items")
    else:
        print(json.dumps(stats(), indent=2))
