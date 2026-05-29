"""
audit_log.py — Append-only structured audit trail for admin actions.

Every privileged operation should call:
    audit_log.record("token_issue", actor="discord:123", label="alice", tier=1)

Stored as JSONL at /home/ubuntu/common/audit.jsonl. Atomic line append
(O_APPEND on Linux). Read with tail() or search().
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Iterator, List, Optional

import gzip
import shutil

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

AUDIT_FILE = "/home/ubuntu/common/audit.jsonl"
ARCHIVE_DIR = "/home/ubuntu/common/audit_archive"

# Bug #13 fix: rotate audit log when it crosses MAX_AUDIT_BYTES.
# Without rotation, audit.jsonl grows unbounded — eventual disk-fill DoS.
MAX_AUDIT_BYTES = 25 * 1024 * 1024   # 25 MB


def _rotate_if_needed() -> None:
    if not os.path.exists(AUDIT_FILE):
        return
    if os.path.getsize(AUDIT_FILE) < MAX_AUDIT_BYTES:
        return
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive = os.path.join(ARCHIVE_DIR, f"audit.{stamp}.jsonl.gz")
    try:
        with open(AUDIT_FILE, "rb") as fin, gzip.open(archive, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        # Truncate live file (atomic via O_TRUNC)
        open(AUDIT_FILE, "w").close()
    except Exception:
        pass


def record(action: str, actor: str = "", **kw) -> None:
    """Append one audit record. Action is the verb, actor is who did it."""
    _rotate_if_needed()   # Bug #13 fix
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "actor": actor or "system",
        **kw,
    }
    line = json.dumps(rec, default=str) + "\n"
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
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


def read_all() -> Iterator[dict]:
    if not os.path.exists(AUDIT_FILE):
        return
    with open(AUDIT_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def tail(n: int = 50) -> List[dict]:
    items = list(read_all())
    return items[-n:]


def search(action: Optional[str] = None,
           actor: Optional[str] = None,
           limit: int = 100) -> List[dict]:
    out: List[dict] = []
    for r in read_all():
        if action and r.get("action") != action:
            continue
        if actor and r.get("actor") != actor:
            continue
        out.append(r)
    return out[-limit:]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "tail":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        for r in tail(n):
            print(json.dumps(r, default=str))
    else:
        items = list(read_all())
        print(f"audit_log: {AUDIT_FILE}")
        print(f"  records: {len(items)}")
        if items:
            from collections import Counter
            actions = Counter(r.get("action", "?") for r in items)
            for a, c in actions.most_common():
                print(f"  {a:<25} {c}")
