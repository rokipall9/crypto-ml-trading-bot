"""
webhook_outbound.py — Push alerts to subscribers' webhook URLs.

Subscribers register a webhook URL + (optional) secret via prefs.
When an alert fires, this module:
  1. Iterates subscribers with a webhook_url set in their prefs
  2. Filters per their preferences (matches_alert)
  3. Pushes the alert HMAC-signed to their URL
  4. Retries on 5xx with exponential backoff
  5. Audits every delivery

Format sent to subscriber:
  POST <subscriber_webhook_url>
  Content-Type: application/json
  X-SRS-Timestamp: <unix epoch>
  X-SRS-Signature: <HMAC-SHA256 hex of '<ts>.<body>' using subscriber_secret>
  X-SRS-Event: alert | close

Subscribers should verify the signature and use timestamp ±5 min.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse


# ─── Bug #2 fix: URL allowlist to prevent SSRF ───
# Subscribers can register webhook_url; we won't POST to any URL that
# resolves to a private/loopback/link-local address, or non-HTTPS.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network(c) for c in [
        "0.0.0.0/8",          # "this network"
        "10.0.0.0/8",         # RFC 1918
        "100.64.0.0/10",      # CGNAT
        "127.0.0.0/8",        # loopback
        "169.254.0.0/16",     # link-local + AWS metadata
        "172.16.0.0/12",      # RFC 1918
        "192.0.0.0/24",       # IETF
        "192.168.0.0/16",     # RFC 1918
        "198.18.0.0/15",      # benchmark
        "224.0.0.0/4",        # multicast
        "240.0.0.0/4",        # reserved
        "::1/128",            # IPv6 loopback
        "fc00::/7",           # IPv6 ULA
        "fe80::/10",          # IPv6 link-local
    ]
]


def _validate_outbound_url(url: str) -> Tuple[bool, str, str]:
    """Returns (ok, reason, resolved_ip).

    Bug #16 fix: also returns the resolved IP that passed checks. Caller
    must use this exact IP for the actual request (NOT the hostname),
    so DNS rebinding between validate-time and request-time can't bypass
    the allowlist. SNI / Host header use the original hostname."""
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"url_parse_failed: {e}", ""
    if parsed.scheme != "https":
        return False, "scheme_must_be_https", ""
    if not parsed.hostname:
        return False, "no_hostname", ""
    try:
        addrs = socket.getaddrinfo(parsed.hostname, None)
    except Exception as e:
        return False, f"dns_failed: {e}", ""
    # Pick first address; reject if ANY resolved address is blocked
    chosen_ip = ""
    for _, _, _, _, sockaddr in addrs:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
            for net in _BLOCKED_NETWORKS:
                if ip in net:
                    return False, f"blocked_ip_range: {ip} in {net}", ""
            if not chosen_ip:
                chosen_ip = sockaddr[0]
        except ValueError:
            return False, f"bad_ip: {sockaddr[0]}", ""
    return True, "ok", chosen_ip


# Bug #14 fix: never follow redirects (blocked URL might 302 to allowed one
# that 302s to a blocked one — endless evasion otherwise).
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_301(self, *a, **k): return None
    def http_error_302(self, *a, **k): return None
    def http_error_303(self, *a, **k): return None
    def http_error_307(self, *a, **k): return None
    def http_error_308(self, *a, **k): return None


_OPENER = urllib.request.build_opener(_NoRedirect())

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger
import audit_log
import subscriber_prefs

log = get_logger("webhook_outbound")

CLOCK_SKEW_SEC = 300
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0


def sign(secret: str, body: bytes, timestamp: str) -> str:
    msg = (timestamp + ".").encode() + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_inbound(secret: str, body: bytes,
                   timestamp_header: str,
                   signature_header: str) -> Tuple[bool, str]:
    """Helper for subscribers to verify what we sent."""
    if not timestamp_header or not signature_header:
        return False, "missing_headers"
    try:
        ts = int(timestamp_header)
    except (TypeError, ValueError):
        return False, "bad_timestamp"
    if abs(int(time.time()) - ts) > CLOCK_SKEW_SEC:
        return False, "clock_skew"
    expected = sign(secret, body, timestamp_header)
    if not hmac.compare_digest(expected, signature_header):
        return False, "signature_mismatch"
    return True, "ok"


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Bug #20 fix: connect to a validated IP but present the original
    hostname for SNI + cert validation. Defeats DNS rebinding (Bug #16)
    AND keeps TLS cert checks correct."""
    def __init__(self, hostname: str, pinned_ip: str,
                 port: int = 443, timeout: int = 8,
                 context: Optional[ssl.SSLContext] = None):
        # Initialize with the hostname (used for SNI + cert validation)
        super().__init__(hostname, port=port, timeout=timeout,
                         context=context or ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self):
        # Step 1: TCP connect to pinned IP (NOT to whatever DNS returns now)
        sock = socket.create_connection((self._pinned_ip, self.port),
                                          self.timeout)
        # Step 2: TLS wrap with the original hostname for SNI + verification
        self.sock = self._context.wrap_socket(
            sock, server_hostname=self.host)


def _send_one(url: str, secret: str, event_type: str,
              alert: dict, delivery_id: str = "") -> Tuple[bool, str]:
    """Single delivery attempt (no retry).
    Bug #2/#14/#16/#20 fixes preserved.

    Upgrade #2 (TradingView): payload enriched with structured _meta block
    (delivery_id, chart_url, ISO timestamp).
    Upgrade #7 (GitHub): emit X-Hub-Signature-256 alongside X-SRS-Signature.
    Plus X-SRS-Delivery-ID for at-least-once dedup on receiver side."""
    import uuid
    ok, reason, ip = _validate_outbound_url(url)
    if not ok:
        log.warn("ssrf_blocked", url_host=_url_host(url), reason=reason)
        return False, f"url_blocked: {reason} (permanent)"
    parsed = urlparse(url)
    # Upgrade #2: enrich payload (TradingView-style)
    if not delivery_id:
        delivery_id = uuid.uuid4().hex[:16]
    enriched = {
        **alert,
        "_meta": {
            "version": "1.0",
            "event_type": event_type,
            "delivered_at": datetime.now(timezone.utc).isoformat(),
            "delivery_id": delivery_id,
            "chart_url": (
                "https://head-twist-currency-advertising.trycloudflare.com"
                "/api/v1/sparkline"),
            "track_record_url": (
                "https://head-twist-currency-advertising.trycloudflare.com"
                "/api/v1/stats"),
        },
    }
    body = json.dumps(enriched, default=str).encode()
    ts = str(int(time.time()))
    headers = {
        "Host": parsed.hostname,
        "Content-Type": "application/json",
        "User-Agent": "srs-webhook-out/1.0",
        "X-SRS-Timestamp": ts,
        "X-SRS-Event": event_type,
        "X-SRS-Delivery-ID": delivery_id,
    }
    if secret:
        # Upgrade #7: dual-signature for max compat
        headers["X-SRS-Signature"] = sign(secret, body, ts)
        gh_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={gh_sig}"
    path_with_query = parsed.path or "/"
    if parsed.query:
        path_with_query += "?" + parsed.query
    try:
        conn = _PinnedHTTPSConnection(
            hostname=parsed.hostname, pinned_ip=ip,
            port=parsed.port or 443, timeout=8)
        conn.request("POST", path_with_query, body=body, headers=headers)
        resp = conn.getresponse()
        # No redirect-following — fix for Bug #14 (HTTPSConnection doesn't
        # auto-follow anyway; this is naturally redirect-resistant)
        if 300 <= resp.status < 400:
            return False, f"http {resp.status}: redirect refused"
        return (200 <= resp.status < 300,
                f"http {resp.status}: {resp.reason}")
    except ssl.SSLCertVerificationError as e:
        return False, f"tls_cert_invalid: {e}"
    except Exception as e:
        return False, f"err: {e}"


def _send_with_retry(url: str, secret: str, event_type: str,
                     alert: dict) -> Tuple[bool, str, int]:
    """Retry up to MAX_RETRIES with exponential backoff. Returns
    (success, last_reason, attempts)."""
    for attempt in range(MAX_RETRIES):
        ok, reason = _send_one(url, secret, event_type, alert)
        if ok:
            return True, reason, attempt + 1
        # Retry on 5xx + transient
        if "http 5" in reason or "err:" in reason:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_BASE ** attempt)
                continue
        return False, reason, attempt + 1
    return False, "max_retries_exceeded", MAX_RETRIES


def deliver(alert: dict, event_type: str = "alert") -> Dict:
    """Iterate all subscribers, push alert to those whose prefs match.
    Non-blocking: dispatches to background thread per subscriber."""
    prefs_all = subscriber_prefs.list_all()
    delivered = []
    failed = []

    def _job(token_prefix: str, p: dict):
        url = (p.get("webhook_url") or "").strip()
        if not url:
            return
        notif_mode = p.get("notification_mode", "channel")
        if notif_mode != "webhook":
            return
        # Build a fake "token" for matches_alert that only uses the prefix
        # (matches_alert reads via get(token), keyed by token[:12])
        # We store keyed by prefix already (subscriber_prefs uses token[:12]),
        # so we need a sentinel token whose prefix matches:
        if not subscriber_prefs.matches_alert("X" * 12 + token_prefix, alert):
            # This won't actually match because of prefix mismatch; instead
            # we read prefs directly from p
            if not _matches_local(p, alert):
                return

        secret = (p.get("webhook_secret") or "").strip()
        ok, reason, attempts = _send_with_retry(url, secret, event_type, alert)
        rec = {
            "token_prefix": token_prefix,
            "url_host": _url_host(url),
            "ok": ok, "reason": reason, "attempts": attempts,
        }
        if ok:
            delivered.append(rec)
            log.info("delivered", **rec)
        else:
            failed.append(rec)
            log.error("delivery_failed", **rec)
        audit_log.record(
            "outbound_webhook" if ok else "outbound_webhook_failed",
            actor="webhook_outbound",
            **rec)

    threads = []
    for tprefix, p in prefs_all.items():
        t = threading.Thread(target=_job, args=(tprefix, p), daemon=True)
        t.start(); threads.append(t)
    for t in threads:
        t.join(timeout=30)

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "subscribers_processed": len(prefs_all),
        "delivered": len(delivered),
        "failed": len(failed),
        "details": {"delivered": delivered, "failed": failed},
    }


def _matches_local(prefs: dict, alert: dict) -> bool:
    """Same logic as subscriber_prefs.matches_alert but using already-loaded prefs."""
    score = alert.get("score")
    if score is not None:
        try:
            if int(score) < int(prefs.get("min_score", 7)):
                return False
        except (ValueError, TypeError):
            pass
    strategies = prefs.get("strategies") or []
    if strategies and alert.get("system") not in strategies:
        return False
    symbols = prefs.get("symbols") or []
    if symbols and alert.get("symbol") not in symbols:
        return False
    return True


def _url_host(url: str) -> str:
    """Extract host for log redaction (don't log full subscriber URLs)."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"


if __name__ == "__main__":
    # Smoke test: deliver a fake alert (no subscribers configured → no-op)
    res = deliver({
        "system": "BREAKOUT_4H", "symbol": "BTCUSDT",
        "side": "LONG", "score": 8, "entry": 79473,
    })
    print(json.dumps(res, indent=2, default=str))
