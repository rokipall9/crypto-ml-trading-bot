"""
tracing.py — Honeycomb-style high-cardinality wide-event tracing.

Standard structured logs are 1 message per line with a few key=value
pairs. Tracing events are MUCH wider — every relevant context detail
attached so you can slice + dice in a query.

Wide events (~50 fields per record) are written to:
  /home/ubuntu/common/trace_events.jsonl

Each request lifecycle generates ONE event with: trace_id, span_id,
parent_span_id, service, operation, duration_ms, status_code, all
relevant business + technical context. Replaces ad-hoc per-step log
lines with one rich record.

Usage in status_server (already in place via X-Request-Id):
  from tracing import emit
  emit("http_request",
       trace_id=rid,
       method="GET", path="/api/v1/stats",
       tier=tier, ip=ip, latency_ms=dt_ms,
       status=code, ip_hash=...,
       cache_hit=False, ...)

Operator queries: jq filtering on trace_events.jsonl.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

TRACE_FILE = "/home/ubuntu/common/trace_events.jsonl"
MAX_TRACE_FILE_BYTES = 50 * 1024 * 1024   # 50 MB rotation
ARCHIVE_DIR = "/home/ubuntu/common/trace_archive"

_LOCK = threading.Lock()


def _rotate_if_needed() -> None:
    if not os.path.exists(TRACE_FILE):
        return
    if os.path.getsize(TRACE_FILE) < MAX_TRACE_FILE_BYTES:
        return
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    import gzip, shutil
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(ARCHIVE_DIR, f"trace.{stamp}.jsonl.gz")
    try:
        with open(TRACE_FILE, "rb") as fin, \
             gzip.open(dest, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        open(TRACE_FILE, "w").close()
    except Exception:
        pass


def emit(operation: str, trace_id: str = "", span_id: str = "",
         parent_span_id: str = "", duration_ms: Optional[float] = None,
         **fields: Any) -> None:
    """Write one wide event for `operation`. trace_id ties spans together."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "operation": operation,
        "service": "srs-status-server",
    }
    if duration_ms is not None:
        rec["duration_ms"] = float(duration_ms)
    rec.update(fields)
    line = json.dumps(rec, default=str) + "\n"
    with _LOCK:
        _rotate_if_needed()
        try:
            with open(TRACE_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


def query(trace_id: str = "", operation: str = "",
          since_iso: str = "", limit: int = 100) -> list:
    """Filter trace events. trace_id ties one request's spans together."""
    if not os.path.exists(TRACE_FILE):
        return []
    out = []
    since_ts = None
    if since_iso:
        try:
            since_ts = datetime.fromisoformat(
                since_iso.replace("Z", "+00:00"))
        except Exception:
            pass
    with open(TRACE_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if trace_id and rec.get("trace_id") != trace_id:
                continue
            if operation and rec.get("operation") != operation:
                continue
            if since_ts:
                try:
                    rts = datetime.fromisoformat(
                        str(rec["ts"]).replace("Z", "+00:00"))
                    if rts < since_ts:
                        continue
                except Exception:
                    pass
            out.append(rec)
    return out[-limit:]


def trace_summary() -> dict:
    """High-level stats."""
    if not os.path.exists(TRACE_FILE):
        return {"events": 0}
    n = 0
    ops = {}
    sample = []
    with open(TRACE_FILE, encoding="utf-8") as f:
        for line in f:
            n += 1
            try:
                r = json.loads(line)
                op = r.get("operation", "?")
                ops[op] = ops.get(op, 0) + 1
                if len(sample) < 5:
                    sample.append(r)
            except Exception:
                pass
    return {"events": n, "by_operation": ops, "sample": sample}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        print(json.dumps(trace_summary(), indent=2, default=str))
    elif len(sys.argv) > 2 and sys.argv[1] == "query":
        print(json.dumps(query(trace_id=sys.argv[2]),
                         indent=2, default=str))
    else:
        print("Usage: tracing.py [summary|query <trace_id>]")
