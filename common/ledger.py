"""
ledger.py — Atomic append + indexed read for forward_results.jsonl.

API:
  append(record)                     — flock + fsync, line-atomic
  read_all()                         — generator, skips corrupt lines
  read_since(offset)                 — incremental: returns (records, new_offset)
  closes()                           — only event=close records
  rotate_if_needed(max_bytes)        — gzip archive when ledger gets big
  count_lines()                      — fast count
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

# fcntl is POSIX-only (Linux/macOS); on Windows we silently skip locking
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

LEDGER  = "/home/ubuntu/common/forward_results.jsonl"
INDEX   = "/home/ubuntu/common/forward_results.index.json"
ROTATED_DIR = "/home/ubuntu/common/forward_results_archive"
MAX_BYTES = 25 * 1024 * 1024  # 25MB triggers rotation


def append(rec: Dict[str, Any]) -> None:
    """Atomic, fsync'd append."""
    if "ts" not in rec:
        rec["ts"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(rec, default=str) + "\n"
    with open(LEDGER, "a", encoding="utf-8") as f:
        if _HAS_FCNTL:
            try: fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except Exception: pass
        f.write(line)
        f.flush()
        try: os.fsync(f.fileno())
        except Exception: pass
        if _HAS_FCNTL:
            try: fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception: pass


def read_all() -> Iterator[Dict[str, Any]]:
    if not os.path.exists(LEDGER):
        return
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue  # corrupt line — never crash


def read_since(offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """For incremental dashboards: return new records + new byte offset."""
    if not os.path.exists(LEDGER):
        return [], 0
    out: List[Dict[str, Any]] = []
    with open(LEDGER, encoding="utf-8") as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        new_offset = f.tell()
    return out, new_offset


def closes() -> Iterator[Dict[str, Any]]:
    for r in read_all():
        if r.get("event") == "close":
            yield r


def rotate_if_needed(max_bytes: int = MAX_BYTES) -> Optional[str]:
    """Gzip-archive ledger if > max_bytes. Returns archive path or None."""
    if not os.path.exists(LEDGER):
        return None
    if os.path.getsize(LEDGER) < max_bytes:
        return None
    os.makedirs(ROTATED_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive = os.path.join(ROTATED_DIR, f"forward_results.{stamp}.jsonl.gz")
    with open(LEDGER, "rb") as fin, gzip.open(archive, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    open(LEDGER, "w").close()
    try: os.remove(INDEX)
    except FileNotFoundError: pass
    return archive


def count_lines() -> int:
    if not os.path.exists(LEDGER):
        return 0
    n = 0
    with open(LEDGER, "rb") as f:
        for _ in f:
            n += 1
    return n


if __name__ == "__main__":
    print(f"ledger: {LEDGER}")
    print(f"  exists: {os.path.exists(LEDGER)}")
    print(f"  size: {os.path.getsize(LEDGER) if os.path.exists(LEDGER) else 0:,} bytes")
    print(f"  lines: {count_lines():,}")
    print(f"  closes: {sum(1 for _ in closes()):,}")
