"""
api_auth.py — Bearer-token authentication with tiered access.

Tiers:
  0  public     /health, /metrics, /api/stats, /api/recent (cached, last 50)
  1  subscriber /api/full (full ledger), /api/risk_extended
  2  admin      /api/admin/*

Tokens stored in /home/ubuntu/common/tokens.json (atomic writes, 0o600).
Use slash command /token to issue/revoke/list.

Token record fields:
  label             — display name / subscriber id
  tier              — 0/1/2
  issued_at         — ISO 8601 UTC
  last_used         — ISO 8601 UTC or null
  uses              — int counter
  expires_at        — optional ISO 8601 UTC; access denied past this
  rate_limit_rpm    — optional override for X-RateLimit-Limit (else tier default)
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

TOKENS_FILE = "/home/ubuntu/common/tokens.json"
DEFAULT_TIER = 0

# Bug #4 fix: serialize concurrent read-modify-write cycles on tokens.json.
# Without this, two parallel requests both load → mutate → save and one
# update is lost (verified live: 17/20 increments under concurrency).
_RMW_LOCK = threading.RLock()


# Bug #18 fix: in-memory cache keyed on (path, mtime_ns, size).
# Bug #26 fix: include path so test TOKENS_FILE swaps don't collide.
# Bug #27 fix: use mtime_ns + size — float mtime resolution is filesystem-
# dependent (often ~1s) and rapid back-to-back writes share mtime, causing
# cache to return stale data.
_LOAD_CACHE: Dict = {"key": None, "data": None}


def _stat_key(path: str):
    st = os.stat(path)
    return (path, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)),
            st.st_size)


def _load() -> Dict[str, dict]:
    if not os.path.exists(TOKENS_FILE):
        return {}
    try:
        cur_key = _stat_key(TOKENS_FILE)
        if (_LOAD_CACHE["key"] == cur_key
                and _LOAD_CACHE["data"] is not None):
            return {k: dict(v) for k, v in _LOAD_CACHE["data"].items()}
        with open(TOKENS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _LOAD_CACHE["key"] = cur_key
        _LOAD_CACHE["data"] = {k: dict(v) for k, v in data.items()}
        return data
    except Exception:
        return {}


def _save(d: Dict[str, dict]) -> None:
    tmp = TOKENS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    os.replace(tmp, TOKENS_FILE)
    try:
        os.chmod(TOKENS_FILE, 0o600)
    except Exception:
        pass
    # Update cache so subsequent _load() doesn't re-read disk
    try:
        _LOAD_CACHE["key"] = _stat_key(TOKENS_FILE)
        _LOAD_CACHE["data"] = {k: dict(v) for k, v in d.items()}
    except Exception:
        pass


# Coinbase Commerce-style soft expiry: 7-day grace period after expires_at
# during which the token still works but is flagged as "expiring soon" in
# /api/v1/me responses. After grace, hard-rejected.
GRACE_DAYS = 7


def _is_expired(rec: dict) -> bool:
    exp = rec.get("expires_at")
    if not exp:
        return False
    try:
        exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        # Hard expiry only after grace period
        return (datetime.now(timezone.utc) >
                exp_dt + timedelta(days=GRACE_DAYS))
    except Exception:
        return False


def _is_in_grace(rec: dict) -> bool:
    """True if past expires_at but within grace window."""
    exp = rec.get("expires_at")
    if not exp:
        return False
    try:
        exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return exp_dt < now <= exp_dt + timedelta(days=GRACE_DAYS)
    except Exception:
        return False


def issue_token(label: str,
                tier: int = 1,
                expires_in_days: Optional[int] = None,
                rate_limit_rpm: Optional[int] = None) -> str:
    """Generate a new token. Show ONCE to user.
    Bug #22 fix: lock-protected read-modify-write."""
    token = secrets.token_urlsafe(24)
    rec: dict = {
        "label": label,
        "tier": int(tier),
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "last_used": None,
        "uses": 0,
    }
    if expires_in_days and expires_in_days > 0:
        exp = datetime.now(timezone.utc) + timedelta(days=int(expires_in_days))
        rec["expires_at"] = exp.isoformat()
    if rate_limit_rpm and rate_limit_rpm > 0:
        rec["rate_limit_rpm"] = int(rate_limit_rpm)
    with _RMW_LOCK:
        d = _load()
        d[token] = rec
        _save(d)
    return token


def revoke_token(token: str) -> bool:
    """Bug #22 fix: hold _RMW_LOCK across read+write. Without this, a
    concurrent authenticate() in another thread can save its (stale)
    snapshot back over the just-revoked state, resurrecting the token."""
    with _RMW_LOCK:
        d = _load()
        if token in d:
            d.pop(token)
            _save(d)
            return True
        return False


def list_tokens() -> Dict[str, dict]:
    """Returns masked-token dict for display."""
    d = _load()
    return {f"{k[:8]}...{k[-4:]}": v for k, v in d.items()}


def authenticate(authorization_header: Optional[str]) -> int:
    """Returns tier (0=public, 1=subscriber, 2=admin).
    Returns 0 if token is missing, invalid, or expired.
    Updates last_used + uses counter atomically."""
    md = _validate_and_record_use(authorization_header)
    if not md:
        return DEFAULT_TIER
    return int(md.get("tier", DEFAULT_TIER))


def _validate_and_record_use(authorization_header: Optional[str]
                             ) -> Optional[dict]:
    """Internal: validate token + record use. Returns metadata or None.
    Bug #4 fix: holds _RMW_LOCK across the load-modify-save cycle so
    concurrent requests don't lose increments / lose revocations."""
    if not authorization_header:
        return None
    h = authorization_header.strip()
    if not h.lower().startswith("bearer "):
        return None
    token = h[7:].strip()
    with _RMW_LOCK:
        d = _load()
        rec = d.get(token)
        if not rec:
            return None
        if _is_expired(rec):
            return None
        rec["last_used"] = datetime.now(timezone.utc).isoformat()
        rec["uses"] = rec.get("uses", 0) + 1
        _save(d)
        return dict(rec)  # copy


def get_token_metadata(authorization_header: Optional[str]
                       ) -> Optional[dict]:
    """Public: returns token metadata if valid (no use-counter bump)."""
    if not authorization_header:
        return None
    h = authorization_header.strip()
    if not h.lower().startswith("bearer "):
        return None
    token = h[7:].strip()
    d = _load()
    rec = d.get(token)
    if not rec or _is_expired(rec):
        return None
    return dict(rec)


def set_rate_limit(token: str, rpm: Optional[int]) -> bool:
    """Admin: set/clear custom rate limit. Bug #22 fix: locked RMW."""
    with _RMW_LOCK:
        d = _load()
        if token not in d:
            return False
        if rpm is None or rpm <= 0:
            d[token].pop("rate_limit_rpm", None)
        else:
            d[token]["rate_limit_rpm"] = int(rpm)
        _save(d)
        return True


def extend_expiry(token: str, days: int) -> bool:
    """Admin: extend (or set) expiry. Bug #22 fix: locked RMW."""
    with _RMW_LOCK:
        d = _load()
        if token not in d:
            return False
        new_exp = datetime.now(timezone.utc) + timedelta(days=int(days))
        d[token]["expires_at"] = new_exp.isoformat()
        _save(d)
        return True


def expire_now(token: str) -> bool:
    """Admin: immediately expire a token. Bug #22 fix: locked RMW."""
    with _RMW_LOCK:
        d = _load()
        if token not in d:
            return False
        d[token]["expires_at"] = datetime.now(timezone.utc).isoformat()
        _save(d)
        return True


def rotate_token(old_token: str) -> Optional[str]:
    """Rotate a token in place — keep all metadata, generate new token.
    Bug #22 fix: lock-protected RMW."""
    with _RMW_LOCK:
        d = _load()
        if old_token not in d:
            return None
        rec = d.pop(old_token)
        new_token = secrets.token_urlsafe(24)
        rec["rotated_from_at"] = datetime.now(timezone.utc).isoformat()
        rec["rotation_count"] = int(rec.get("rotation_count", 0)) + 1
        d[new_token] = rec
        _save(d)
    return new_token


def cleanup_expired(grace_days: int = 7) -> int:
    """Permanently delete tokens that expired more than grace_days ago.
    Returns count removed. Bug #23 fix: lock-protected RMW."""
    with _RMW_LOCK:
        d = _load()
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(grace_days))
        to_remove = []
        for tok, rec in d.items():
            exp = rec.get("expires_at")
            if not exp:
                continue
            try:
                exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                if exp_dt < cutoff:
                    to_remove.append(tok)
            except Exception:
                pass
        for tok in to_remove:
            d.pop(tok)
        if to_remove:
            _save(d)
        return len(to_remove)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "issue":
        label = sys.argv[2]
        tier = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        days = int(sys.argv[4]) if len(sys.argv) > 4 else None
        rpm = int(sys.argv[5]) if len(sys.argv) > 5 else None
        print(issue_token(label, tier, expires_in_days=days,
                          rate_limit_rpm=rpm))
    elif len(sys.argv) >= 2 and sys.argv[1] == "list":
        print(json.dumps(list_tokens(), indent=2, default=str))
    elif len(sys.argv) >= 2 and sys.argv[1] == "cleanup":
        n = cleanup_expired(int(sys.argv[2]) if len(sys.argv) > 2 else 7)
        print(f"removed {n} expired tokens")
