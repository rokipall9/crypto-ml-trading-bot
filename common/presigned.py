"""
presigned.py — AWS S3-style pre-signed URLs for time-limited resource access.

A subscriber wants to share their audit-log export with their accountant
or compliance reviewer for 1 hour. Instead of giving away their token
(or having the accountant create their own), they request a pre-signed
URL: a single URL that's valid for N seconds, no auth headers needed.

URL format:
  /api/v1/signed/<resource>?expires=<epoch>&sig=<HMAC>

The signature is HMAC(secret, resource + expires) using a server-side
SIGNING_SECRET. Subscribers get the URL via /api/v1/me/sign-url and
share it — the receiver hits the URL and gets the resource directly.

Use cases:
  - Subscriber sharing CSV export with accountant
  - Embedding sparkline in a one-time pitch deck without exposing token
  - Time-bounded debug access
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
from typing import Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")

# Server-side secret used to sign URLs. Read at startup; rotate by
# changing the env var and restarting status_server. Old signed URLs
# stop working immediately.
_SECRET = (os.environ.get("PRESIGN_SECRET", "").strip()
           or "fallback-secret-please-set-PRESIGN_SECRET")

# Allowed resources — explicit allowlist prevents any path coercion
ALLOWED_RESOURCES = {
    "stats":     {"target": "/api/v1/stats",     "max_ttl_sec": 3600},
    "recent":    {"target": "/api/v1/recent",    "max_ttl_sec": 3600},
    "sparkline": {"target": "/api/v1/sparkline", "max_ttl_sec": 86400},
    "alerts":    {"target": "/api/v1/alerts",    "max_ttl_sec": 7200},
}


def _sign(resource: str, expires: int) -> str:
    msg = f"{resource}\n{expires}".encode()
    return hmac.new(_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def issue(resource: str, ttl_sec: int = 600) -> Tuple[bool, dict]:
    """Issue a signed URL. Returns (ok, {url, expires_at} | error).
    Loop #6: response now includes a one-line revocation tip — operator
    can revoke ALL outstanding signed URLs by rotating PRESIGN_SECRET
    in .env (config_reload picks it up; no restart required)."""
    if resource not in ALLOWED_RESOURCES:
        return False, {"error": "unknown_resource",
                       "allowed": list(ALLOWED_RESOURCES.keys())}
    cfg = ALLOWED_RESOURCES[resource]
    ttl_sec = min(int(ttl_sec), cfg["max_ttl_sec"])
    expires = int(time.time()) + ttl_sec
    sig = _sign(resource, expires)
    return True, {
        "url": f"/api/v1/signed/{resource}?expires={expires}&sig={sig}",
        "expires_at": expires,
        "ttl_sec": ttl_sec,
        "resource": resource,
        "target": cfg["target"],
        "revocation_tip": ("rotate PRESIGN_SECRET in .env to invalidate "
                           "all outstanding signed URLs (config_reload "
                           "picks it up; no restart needed)"),
    }


def verify(resource: str, expires_str: str,
           sig_provided: str) -> Tuple[bool, str]:
    """Validate a signed URL. Returns (ok, reason)."""
    if resource not in ALLOWED_RESOURCES:
        return False, "unknown_resource"
    try:
        expires = int(expires_str)
    except (ValueError, TypeError):
        return False, "bad_expires"
    if int(time.time()) > expires:
        return False, "expired"
    expected = _sign(resource, expires)
    if not hmac.compare_digest(expected, sig_provided):
        return False, "signature_mismatch"
    return True, "ok"


def target_for(resource: str) -> Optional[str]:
    cfg = ALLOWED_RESOURCES.get(resource)
    return cfg["target"] if cfg else None


if __name__ == "__main__":
    import json
    if len(sys.argv) >= 2 and sys.argv[1] == "issue":
        res = sys.argv[2] if len(sys.argv) > 2 else "stats"
        ttl = int(sys.argv[3]) if len(sys.argv) > 3 else 600
        ok, body = issue(res, ttl)
        print(json.dumps({"ok": ok, **body}, indent=2, default=str))
    else:
        print(f"Allowed resources: {list(ALLOWED_RESOURCES.keys())}")
        print(f"Usage: presigned.py issue <resource> [ttl_sec]")
