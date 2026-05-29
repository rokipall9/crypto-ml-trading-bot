"""
token_lease.py — HashiCorp Vault-style short-lived auto-renewing leases.

A "lease" is a token that expires in N minutes (default 60). Subscribers
can present a lease + their long-term credential to renew, getting a
fresh lease without re-issuing the long-term token.

If a lease leaks: damage is bounded to the remaining TTL.

Adopt path:
  Subscriber calls POST /api/v1/me/lease (with Bearer long-term-token).
  Returns: {"lease_token": "...", "expires_at": ..., "renew_url": "..."}.
  Subscriber uses lease_token for subsequent API calls.

Long-term token still works for everything; lease is opt-in for cautious
subscribers who don't want to expose the long-term token to a CI runner
or shared environment.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("token_lease")

LEASES_FILE = "/home/ubuntu/common/token_leases.json"
DEFAULT_TTL_MIN = 60
MAX_TTL_MIN = 240
_LOCK = threading.RLock()


def _load() -> Dict[str, dict]:
    if not os.path.exists(LEASES_FILE):
        return {}
    try:
        with open(LEASES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: Dict[str, dict]) -> None:
    tmp = LEASES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, default=str)
    os.replace(tmp, LEASES_FILE)
    try:
        os.chmod(LEASES_FILE, 0o600)
    except Exception:
        pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue(parent_token: str, ttl_min: int = DEFAULT_TTL_MIN) -> dict:
    """Issue a new lease bound to a parent token."""
    ttl_min = min(max(int(ttl_min), 5), MAX_TTL_MIN)
    lease = secrets.token_urlsafe(20)
    rec = {
        "lease_token": lease,
        "parent_prefix": parent_token[:8],
        "issued_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(minutes=ttl_min)).isoformat(),
        "ttl_min": ttl_min,
        "renewals": 0,
    }
    with _LOCK:
        d = _load()
        # Lazy prune expired
        now = _now()
        d = {k: v for k, v in d.items()
             if datetime.fromisoformat(
                 str(v.get("expires_at", "")).replace("Z", "+00:00")) > now}
        d[lease] = rec
        _save(d)
    log.info("lease_issued", lease_prefix=lease[:8],
             parent_prefix=parent_token[:8], ttl_min=ttl_min)
    return rec


def resolve(lease_token: str) -> Optional[Tuple[str, dict]]:
    """Return (parent_token_prefix, lease_record) if valid + unexpired.
    Returns None if invalid/expired."""
    if not lease_token:
        return None
    with _LOCK:
        d = _load()
        rec = d.get(lease_token)
        if not rec:
            return None
        try:
            exp = datetime.fromisoformat(
                str(rec["expires_at"]).replace("Z", "+00:00"))
            if _now() > exp:
                d.pop(lease_token, None)
                _save(d)
                return None
        except Exception:
            return None
        return rec["parent_prefix"], rec


def renew(lease_token: str, ttl_min: int = DEFAULT_TTL_MIN) -> Optional[dict]:
    """Renew an existing lease — returns NEW lease record."""
    res = resolve(lease_token)
    if not res:
        return None
    parent_prefix, old_rec = res
    ttl_min = min(max(int(ttl_min), 5), MAX_TTL_MIN)
    with _LOCK:
        d = _load()
        if lease_token in d:
            # Issue NEW lease with fresh string; revoke old
            d.pop(lease_token, None)
            new_lease = secrets.token_urlsafe(20)
            new_rec = {
                "lease_token": new_lease,
                "parent_prefix": parent_prefix,
                "issued_at": _now().isoformat(),
                "expires_at": (_now()
                               + timedelta(minutes=ttl_min)).isoformat(),
                "ttl_min": ttl_min,
                "renewals": int(old_rec.get("renewals", 0)) + 1,
                "renewed_from": lease_token[:8],
            }
            d[new_lease] = new_rec
            _save(d)
            log.info("lease_renewed", new_lease_prefix=new_lease[:8],
                     parent_prefix=parent_prefix,
                     renewals=new_rec["renewals"])
            return new_rec
    return None


def revoke(lease_token: str) -> bool:
    with _LOCK:
        d = _load()
        if lease_token in d:
            d.pop(lease_token)
            _save(d)
            return True
        return False


def cleanup_expired() -> int:
    """Run periodically. Returns count removed."""
    with _LOCK:
        d = _load()
        now = _now()
        before = len(d)
        d = {k: v for k, v in d.items()
             if datetime.fromisoformat(
                 str(v.get("expires_at", "")).replace("Z", "+00:00")) > now}
        removed = before - len(d)
        if removed > 0:
            _save(d)
        return removed


def stats() -> dict:
    d = _load()
    now = _now()
    live = []
    for v in d.values():
        try:
            exp = datetime.fromisoformat(
                str(v.get("expires_at", "")).replace("Z", "+00:00"))
            if exp > now:
                live.append(v)
        except Exception:
            pass
    return {
        "total_leases": len(d),
        "live_leases": len(live),
        "renewal_count_total": sum(v.get("renewals", 0) for v in live),
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(stats(), indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        print(f"removed {cleanup_expired()} expired leases")
    else:
        print("Usage: token_lease.py [stats|cleanup]")
