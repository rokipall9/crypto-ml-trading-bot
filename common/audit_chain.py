"""
audit_chain.py — Tamper-evident audit log via hash chaining.

Each entry includes the SHA256 of the previous entry. Any insertion,
deletion, or modification breaks the chain — verifiable by anyone with
a checkpoint hash.

Format (JSONL, one entry per line):
  {"seq": 1, "ts": "...", "action": "...", ..., "prev_hash": "0"*64, "hash": "<hex>"}

The "hash" of a record is SHA256 of its JSON serialization with the
"hash" field removed. The next record's "prev_hash" must equal this.

Chain root: prev_hash of seq=1 is "0"*64.

Usage:
    from audit_chain import record, verify, head_hash
    record("token_issue", actor="discord:123", label="alice")
    ok, broken_at = verify()  # full integrity check
    h = head_hash()           # checkpoint to publish externally

Operationally: post head_hash() to a public Discord channel daily;
anyone replaying the audit chain can verify it matches.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Tuple

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

CHAIN_FILE = "/home/ubuntu/common/audit_chain.jsonl"
GENESIS_HASH = "0" * 64

# Bug #5 fix: serialize concurrent record() calls so two threads don't both
# read the same prev_hash and write conflicting seq numbers.
_CHAIN_LOCK = threading.RLock()

# Bug #11 fix: cap individual record size to keep _read_last_record's
# 4KB tail-seek correct. Larger records fall through to a full-scan path.
MAX_RECORD_SIZE = 3500   # leave headroom under the 4096 tail seek


def _hash_record(rec: dict) -> str:
    """SHA256 of the canonical JSON serialization, excluding the 'hash' field."""
    payload = {k: v for k, v in rec.items() if k != "hash"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _read_last_record() -> Tuple[int, str]:
    """Returns (last_seq, last_hash). Genesis values if file empty/missing.
    Bug #11 fix: if the 4KB tail seek doesn't yield a valid line (because
    a record exceeded 4KB or got partially split), fall back to a full
    file scan to find the actual last valid record."""
    if not os.path.exists(CHAIN_FILE):
        return 0, GENESIS_HASH
    last = None
    size = 0
    with open(CHAIN_FILE, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return 0, GENESIS_HASH
        # Fast path: try last 4KB
        chunk = max(0, size - 4096)
        f.seek(chunk)
        tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line)
                break
            except Exception:
                continue
    # Slow path: tail had no valid JSON (record > 4KB) → full scan
    if last is None and size > 0:
        with open(CHAIN_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except Exception:
                    continue
    if last is None:
        return 0, GENESIS_HASH
    return int(last.get("seq", 0)), str(last.get("hash", GENESIS_HASH))


def record(action: str, actor: str = "system", **kw) -> dict:
    """Append a new chained audit entry. Returns the entry.

    Bug #5 fix: holds _CHAIN_LOCK across read+write so two threads can't
    both read the same prev_hash and write conflicting seq numbers.
    Bug #11 fix: enforces MAX_RECORD_SIZE so the tail-seek read path stays
    correct — oversized payloads are truncated rather than breaking the chain."""
    with _CHAIN_LOCK:
        last_seq, last_hash = _read_last_record()
        rec = {
            "seq": last_seq + 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "actor": actor,
            **kw,
            "prev_hash": last_hash,
        }
        rec["hash"] = _hash_record(rec)
        line = json.dumps(rec, default=str) + "\n"
        # Bug #11: enforce size cap to preserve tail-seek invariant
        if len(line) > MAX_RECORD_SIZE:
            # Truncate string fields proportionally
            rec_truncated = dict(rec)
            for k, v in list(rec_truncated.items()):
                if k in ("seq", "ts", "hash", "prev_hash", "action", "actor"):
                    continue
                if isinstance(v, str) and len(v) > 200:
                    rec_truncated[k] = v[:200] + "...[truncated]"
            rec_truncated["_truncated"] = True
            rec_truncated["hash"] = _hash_record(rec_truncated)
            rec = rec_truncated
            line = json.dumps(rec, default=str) + "\n"
        with open(CHAIN_FILE, "a", encoding="utf-8") as f:
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
    return rec


def verify() -> Tuple[bool, int]:
    """Walk the chain from genesis. Returns (chain_valid, broken_at_seq)."""
    if not os.path.exists(CHAIN_FILE):
        return True, 0
    expected_prev = GENESIS_HASH
    seq = 0
    with open(CHAIN_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                return False, seq + 1
            seq += 1
            if int(rec.get("seq", -1)) != seq:
                return False, seq
            if rec.get("prev_hash") != expected_prev:
                return False, seq
            stored_hash = rec.get("hash", "")
            if _hash_record(rec) != stored_hash:
                return False, seq
            expected_prev = stored_hash
    return True, 0


def head_hash() -> str:
    """Return the latest entry's hash (the checkpoint)."""
    _, h = _read_last_record()
    return h


def length() -> int:
    seq, _ = _read_last_record()
    return seq


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "verify":
        ok, broken = verify()
        print(json.dumps({"chain_valid": ok, "broken_at_seq": broken,
                          "length": length(), "head_hash": head_hash()},
                         indent=2))
        sys.exit(0 if ok else 1)
    elif len(sys.argv) > 1 and sys.argv[1] == "head":
        print(head_hash())
    elif len(sys.argv) > 2 and sys.argv[1] == "record":
        rec = record(sys.argv[2], actor="cli", note=" ".join(sys.argv[3:]))
        print(json.dumps(rec, indent=2, default=str))
    else:
        print(f"Chain: {CHAIN_FILE}")
        print(f"Length: {length()}")
        print(f"Head:   {head_hash()}")
        ok, broken = verify()
        print(f"Valid:  {ok}" + (f" (broken at seq {broken})" if not ok else ""))
