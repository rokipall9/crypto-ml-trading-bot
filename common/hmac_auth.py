"""
hmac_auth.py — HMAC-SHA256 request signing for sensitive admin endpoints.

Defends against:
  - Token leaks via proxy/CDN logs (signature requires the secret AND
    request details; the bearer token alone is useless without the body)
  - Replay attacks (timestamp window of ±5 min)
  - Timing attacks (constant-time compare)

Client must send:
  Authorization: Bearer <token>           ← must be tier 2 admin
  X-Request-Timestamp: <unix epoch>
  X-Request-Signature: <hex of HMAC>

Signature input:
  message = METHOD + "\\n" + PATH + "\\n" + TIMESTAMP + "\\n" + sha256_hex(BODY)
  signature = HMAC-SHA256(token, message)
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Tuple

CLOCK_SKEW_SEC = 300   # ±5 minutes


def build_message(method: str, path: str, timestamp: str,
                  body: bytes = b"") -> str:
    body_sha = hashlib.sha256(body).hexdigest()
    return f"{method}\n{path}\n{timestamp}\n{body_sha}"


def sign(secret: str, method: str, path: str,
         timestamp: str, body: bytes = b"") -> str:
    msg = build_message(method, path, timestamp, body)
    return hmac.new(secret.encode(), msg.encode(),
                    hashlib.sha256).hexdigest()


def verify(secret: str, method: str, path: str,
           timestamp: str, signature: str,
           body: bytes = b"") -> Tuple[bool, str]:
    """Returns (ok, reason). reason ∈ {ok, missing_*, bad_timestamp,
    clock_skew, signature_mismatch}."""
    if not timestamp:
        return False, "missing_timestamp"
    if not signature:
        return False, "missing_signature"
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False, "bad_timestamp"
    now = int(time.time())
    if abs(now - ts) > CLOCK_SKEW_SEC:
        return False, "clock_skew"
    expected = sign(secret, method, path, timestamp, body)
    if not hmac.compare_digest(expected, signature):
        return False, "signature_mismatch"
    return True, "ok"


if __name__ == "__main__":
    # CLI: generate a signature for testing
    #   python3 hmac_auth.py <secret> GET /api/admin/audit
    import sys
    if len(sys.argv) < 4:
        print("Usage: hmac_auth.py <secret> <METHOD> <PATH> [body]")
        sys.exit(1)
    secret = sys.argv[1]; method = sys.argv[2].upper()
    path = sys.argv[3]; body = (sys.argv[4] if len(sys.argv) > 4 else "").encode()
    ts = str(int(time.time()))
    sig = sign(secret, method, path, ts, body)
    print(f"X-Request-Timestamp: {ts}")
    print(f"X-Request-Signature: {sig}")
    print()
    print(f'curl -i -H "Authorization: Bearer {secret}" \\')
    print(f'     -H "X-Request-Timestamp: {ts}" \\')
    print(f'     -H "X-Request-Signature: {sig}" \\')
    print(f'     https://127.0.0.1{path}')
