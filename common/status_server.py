"""
status_server.py — Production HTTP server.

Endpoints:
  GET  /              → status.html (cached upstream 30s)
  GET  /api/stats     → JSON track record + risk metrics (server-cache 30s)
  GET  /api/recent    → JSON last 50 close events (server-cache 10s)
  GET  /metrics       → Prometheus exposition (Grafana-ready)
  GET  /health        → "ok"
  OPTIONS *           → CORS preflight

Production features:
  - Threaded (one thread per request, daemon)
  - Per-IP token-bucket rate limit (60 req/min)
  - In-memory TTL cache (30s stats, 10s recent)
  - gzip compression for responses > 256 bytes
  - Structured JSON-lines request log
  - Graceful SIGTERM/SIGINT shutdown
  - Internal counters exposed via /metrics
"""
from __future__ import annotations

import gzip
import http.server
import io
import json
import os
import signal
import socketserver
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Dict, Optional

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger
import api_auth
import ip_ban
import hmac_auth
log = get_logger("status_server")

PORT = 8080
BIND_HOST = os.environ.get("SRS_BIND", "127.0.0.1")  # Bug #12 fix: loopback only by default
ROOT = "/home/ubuntu/common"
HTML_FILE = "dashboard.html"   # was status.html — upgraded in Final⁸

# ─── Bug #1 fix: only honor proxy headers if connection is from trusted upstream ───
TRUSTED_PROXY_PREFIXES = ("127.", "::1", "::ffff:127.")
# Cloudflare IPv4 ranges (from cloudflare.com/ips/) — for cloudflared tunnel host
CLOUDFLARE_IPV4_PREFIXES = (
    "173.245.48.", "103.21.244.", "103.22.200.", "103.31.4.",
    "141.101.64.", "108.162.192.", "190.93.240.", "188.114.96.",
    "197.234.240.", "198.41.128.", "162.158.",
    "104.16.", "104.17.", "104.18.", "104.19.", "104.20.",
    "104.21.", "104.22.", "104.23.", "104.24.", "104.25.",
    "104.26.", "104.27.", "104.28.",
    "172.64.", "172.65.", "172.66.", "172.67.", "172.68.",
    "172.69.", "172.70.", "172.71.",
    "131.0.72.",
)


def _is_trusted_upstream(ip: str) -> bool:
    return any(ip.startswith(p) for p in
               TRUSTED_PROXY_PREFIXES + CLOUDFLARE_IPV4_PREFIXES)

# ─── Tiered rate limiting ───
# Per-IP token-bucket for tier 0; per-token bucket for tier ≥ 1
RATE_BUCKET: Dict[str, deque] = defaultdict(deque)
RATE_LOCK = threading.Lock()
RATE_WINDOW = 60.0     # seconds
TIER_LIMITS = {0: 60, 1: 300, 2: 1200}  # tier → req/min

def _rate_ok(key: str, tier: int, override_rpm: Optional[int] = None) -> bool:
    """key is the bucket name (IP for tier 0, token-prefix for higher tiers).
    override_rpm: per-token custom limit (from api_auth metadata)."""
    limit = override_rpm or TIER_LIMITS.get(tier, TIER_LIMITS[0])
    now = time.time()
    with RATE_LOCK:
        bucket = RATE_BUCKET[key]
        while bucket and bucket[0] < now - RATE_WINDOW:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
    return True


def _rate_remaining(key: str, tier: int,
                    override_rpm: Optional[int] = None) -> int:
    """Compute how many requests left in the current window."""
    limit = override_rpm or TIER_LIMITS.get(tier, TIER_LIMITS[0])
    now = time.time()
    with RATE_LOCK:
        bucket = RATE_BUCKET[key]
        while bucket and bucket[0] < now - RATE_WINDOW:
            bucket.popleft()
        return max(0, limit - len(bucket))

# ─── Response cache ───
_CACHE: Dict[str, tuple] = {}    # key -> (expires_at, body_bytes, ctype)
_CACHE_LOCK = threading.Lock()

def _cache_get(key: str) -> Optional[tuple]:
    with _CACHE_LOCK:
        ent = _CACHE.get(key)
        if ent and ent[0] > time.time():
            return ent[1], ent[2]
        if ent:
            _CACHE.pop(key, None)
    return None

def _cache_set(key: str, ttl: float, body: bytes, ctype: str) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + ttl, body, ctype)

# ─── Internal counters ───
_M = {"req_total": 0, "req_429": 0, "req_5xx": 0, "started_at": time.time()}
_M_LOCK = threading.Lock()

# ─── Bug #8 fix: per-token SSE concurrent-connection cap ───
_SSE_COUNTS: Dict[str, int] = defaultdict(int)
_SSE_LOCK = threading.Lock()
SSE_PER_TOKEN_CAP = 2

# ─── Bug #9 fix: replay protection for HMAC signatures (nonce cache) ───
_HMAC_SEEN: Dict[str, float] = {}
_HMAC_SEEN_LOCK = threading.Lock()
HMAC_NONCE_TTL = 600   # 2× the timestamp window


def _hmac_seen_already(sig: str) -> bool:
    """Returns True if signature was seen in the last HMAC_NONCE_TTL seconds."""
    now = time.time()
    with _HMAC_SEEN_LOCK:
        # Prune expired
        for s, t in list(_HMAC_SEEN.items()):
            if t < now - HMAC_NONCE_TTL:
                _HMAC_SEEN.pop(s, None)
        if sig in _HMAC_SEEN:
            return True
        _HMAC_SEEN[sig] = now
        return False

# ─── Latency histograms ───
LATENCY_BUCKETS = [0.005, 0.025, 0.1, 0.5, 2.5]  # seconds
_LAT: Dict[str, dict] = defaultdict(
    lambda: {"count": 0, "sum": 0.0, "buckets": [0] * len(LATENCY_BUCKETS)})


def _record_latency(path: str, seconds: float) -> None:
    bucket_path = path if path.startswith("/api/") or path == "/metrics" else "/other"
    with _M_LOCK:
        rec = _LAT[bucket_path]
        rec["count"] += 1
        rec["sum"] += seconds
        for i, b in enumerate(LATENCY_BUCKETS):
            if seconds <= b:
                rec["buckets"][i] += 1


def _api_stats_body() -> bytes:
    cached = _cache_get("api_stats")
    if cached:
        return cached[0]
    try:
        import track_record
        try:
            import risk_metrics
            rm = risk_metrics.compute_metrics()
        except Exception as e:
            rm = {"error": str(e)}
        body = {
            "summary": track_record.all_summary(),
            "per_strategy": track_record.per_strategy_table(),
            "risk_adjusted": rm,
            "generated_at": time.time(),
        }
    except Exception as e:
        body = {"error": str(e)}
    raw = json.dumps(body, default=str, indent=2).encode()
    _cache_set("api_stats", 30, raw, "application/json")
    return raw


def _api_recent_body(limit: int = 50) -> bytes:
    cached = _cache_get(f"api_recent_{limit}")
    if cached:
        return cached[0]
    try:
        from ledger import closes
        items = list(closes())[-limit:]
        body = {"items": items, "count": len(items)}
    except Exception as e:
        body = {"items": [], "count": 0, "error": str(e)}
    raw = json.dumps(body, default=str, indent=2).encode()
    _cache_set(f"api_recent_{limit}", 10, raw, "application/json")
    return raw


def _api_full_body() -> bytes:
    """Subscriber endpoint: full ledger as JSON array."""
    try:
        from ledger import read_all
        items = list(read_all())
        body = {"items": items, "count": len(items)}
    except Exception as e:
        body = {"items": [], "count": 0, "error": str(e)}
    return json.dumps(body, default=str).encode()


def _api_risk_extended_body() -> bytes:
    """Subscriber endpoint: full risk_metrics including bootstrap CI + Kelly."""
    try:
        import risk_metrics
        body = risk_metrics.compute_metrics()
    except Exception as e:
        body = {"error": str(e)}
    return json.dumps(body, default=str, indent=2).encode()


def _bot_metrics_lines() -> list:
    """Read /home/ubuntu/common/bot_metrics.json and emit Prometheus lines."""
    out: list = []
    path = "/home/ubuntu/common/bot_metrics.json"
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            bm = json.load(f)
    except Exception:
        return out
    for k, v in bm.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            metric = f"srs_bot_{k}"
            out.append(f"# TYPE {metric} gauge")
            out.append(f"{metric} {v}")
    return out


def _prometheus_body() -> bytes:
    try:
        import track_record
        s = track_record.all_summary()
    except Exception:
        s = {"n": 0, "tp": 0, "sl": 0, "wr": 0, "total_r": 0}
    try:
        import risk_metrics
        rm = risk_metrics.compute_metrics()
    except Exception:
        rm = {}
    with _M_LOCK:
        m = dict(_M)
    uptime = time.time() - m["started_at"]
    # Latency histograms
    with _M_LOCK:
        lat_snapshot = {k: dict(v, buckets=list(v["buckets"]))
                        for k, v in _LAT.items()}
    out = []
    out.append("# HELP srs_request_duration_seconds HTTP request latency")
    out.append("# TYPE srs_request_duration_seconds histogram")
    for p, rec in lat_snapshot.items():
        for i, b in enumerate(LATENCY_BUCKETS):
            out.append(f'srs_request_duration_seconds_bucket'
                       f'{{path="{p}",le="{b}"}} {rec["buckets"][i]}')
        out.append(f'srs_request_duration_seconds_bucket'
                   f'{{path="{p}",le="+Inf"}} {rec["count"]}')
        out.append(f'srs_request_duration_seconds_count{{path="{p}"}} {rec["count"]}')
        out.append(f'srs_request_duration_seconds_sum{{path="{p}"}} {rec["sum"]:.4f}')
    out.extend([
        "# HELP srs_trades_total Total resolved trades",
        "# TYPE srs_trades_total counter",
        f"srs_trades_total {s.get('n', 0)}",
        "# HELP srs_trades_won_total Winning trades (TP)",
        "# TYPE srs_trades_won_total counter",
        f"srs_trades_won_total {s.get('tp', 0)}",
        "# HELP srs_trades_lost_total Losing trades (SL)",
        "# TYPE srs_trades_lost_total counter",
        f"srs_trades_lost_total {s.get('sl', 0)}",
        "# HELP srs_total_r Cumulative R",
        "# TYPE srs_total_r gauge",
        f"srs_total_r {s.get('total_r', 0)}",
        "# HELP srs_win_rate Win rate percentage",
        "# TYPE srs_win_rate gauge",
        f"srs_win_rate {s.get('wr', 0)}",
        "# HELP srs_sharpe Sharpe per-trade",
        "# TYPE srs_sharpe gauge",
        f"srs_sharpe {rm.get('sharpe') or 0}",
        "# HELP srs_max_dd Max drawdown in R",
        "# TYPE srs_max_dd gauge",
        f"srs_max_dd {rm.get('max_dd', 0)}",
        "# HELP srs_http_requests_total Total HTTP requests",
        "# TYPE srs_http_requests_total counter",
        f"srs_http_requests_total {m['req_total']}",
        "# HELP srs_http_429_total Rate-limited responses",
        "# TYPE srs_http_429_total counter",
        f"srs_http_429_total {m['req_429']}",
        "# HELP srs_http_5xx_total Server errors",
        "# TYPE srs_http_5xx_total counter",
        f"srs_http_5xx_total {m['req_5xx']}",
        "# HELP srs_uptime_seconds Server uptime",
        "# TYPE srs_uptime_seconds gauge",
        f"srs_uptime_seconds {uptime:.0f}",
        "",
    ])
    # Loop #5: regime gauge (UNKNOWN=0 / RANGING=1 / TRENDING_UP=2 /
    # TRENDING_DOWN=3 / VOLATILE=4 — labeled gauge for Grafana)
    try:
        import regime as _regime
        cur = _regime.current()
        regime_name = cur.get("regime", "UNKNOWN")
        regime_code = {"UNKNOWN": 0, "RANGING": 1, "TRENDING_UP": 2,
                       "TRENDING_DOWN": 3, "VOLATILE": 4}.get(regime_name, 0)
        out.extend([
            "# HELP srs_market_regime Current market regime classification",
            "# TYPE srs_market_regime gauge",
            f'srs_market_regime{{regime="{regime_name}"}} {regime_code}',
        ])
    except Exception:
        pass
    # Loop #5: idempotency replay counter
    try:
        import idempotency as _idem
        d = _idem._load()
        live = sum(1 for v in d.values()
                   if v.get("expires_at", 0) > time.time())
        out.extend([
            "# HELP srs_idempotency_cache_size Active idempotency keys",
            "# TYPE srs_idempotency_cache_size gauge",
            f"srs_idempotency_cache_size {live}",
        ])
    except Exception:
        pass
    out.extend(_bot_metrics_lines())
    out.append("")
    return "\n".join(out).encode()


__version__ = "1.0.0"


class StatusHandler(http.server.SimpleHTTPRequestHandler):
    server_version = f"SRS/{__version__}"

    def _client_ip(self) -> str:
        # Bug #1 fix: only honor proxy headers if direct connection is from
        # a trusted upstream (loopback, Caddy, cloudflared tunnel, or CF IPs).
        # Otherwise an attacker can spoof XFF to bypass fail2ban + rate limit.
        direct = self.client_address[0] if self.client_address else "unknown"
        if not _is_trusted_upstream(direct):
            return direct
        # Trusted upstream — prefer Cloudflare's CF-Connecting-IP, then XFF
        cf = self.headers.get("CF-Connecting-IP", "").strip()
        if cf:
            return cf
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return direct

    def _send(self, code: int, body: bytes, ctype: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        with _M_LOCK:
            _M["req_total"] += 1
            if code == 429: _M["req_429"] += 1
            if code >= 500: _M["req_5xx"] += 1

        # gzip if client supports + payload non-trivial
        accept = self.headers.get("Accept-Encoding", "")
        ce = None
        if "gzip" in accept and len(body) > 256:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as g:
                g.write(body)
            body = buf.getvalue()
            ce = "gzip"

        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Accept-Encoding")
            if ce:
                self.send_header("Content-Encoding", ce)
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected mid-write

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_POST(self):
        """Upgrade #1 (Stripe Idempotency-Key) + Bug #17 fix:
        POST /api/webhooks/<provider>             — payment provider callback
        POST /api/admin/incidents/<id>/ack        — incident ACK"""
        ip = self._client_ip()
        rid = self.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
        self._rid = rid
        if ip_ban.is_banned(ip):
            self._send(403, b'{"error":"ip_banned"}\n',
                       "application/json", {"X-Request-Id": rid})
            return
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b""
        except Exception:
            self._send(400, b'{"error":"bad_body"}\n', "application/json")
            return
        idem_key = self.headers.get("Idempotency-Key", "").strip()
        if idem_key:
            try:
                import idempotency
                cached = idempotency.lookup(idem_key)
                if cached:
                    code, body_dict = cached
                    self._send(code, json.dumps(body_dict).encode(),
                               "application/json",
                               {"X-Request-Id": rid,
                                "Idempotency-Replayed": "true"})
                    return
            except Exception as e:
                log.error("idem_lookup_fail", err=str(e))
        try:
            if path.startswith("/api/webhooks/"):
                provider = path[len("/api/webhooks/"):].split("/")[0]
                sig_hdr = (self.headers.get("Stripe-Signature", "")
                           or self.headers.get("X-Signature", "")
                           or self.headers.get("X-Hub-Signature-256", ""))
                try:
                    import subscriber_lifecycle
                    code, body_dict = subscriber_lifecycle.handle_webhook(
                        provider, body, sig_hdr)
                    if idem_key:
                        try:
                            import idempotency
                            idempotency.remember(idem_key, code, body_dict)
                        except Exception:
                            pass
                    self._send(code, json.dumps(body_dict).encode(),
                               "application/json", {"X-Request-Id": rid})
                except Exception as e:
                    log.error("webhook_fail", err=str(e))
                    self._send(500, b'{"error":"internal"}\n',
                               "application/json")
            elif path.startswith("/api/admin/incidents/") and path.endswith("/ack"):
                incident_id = path[len("/api/admin/incidents/"):-len("/ack")]
                tier = api_auth.authenticate(self.headers.get("Authorization"))
                if tier < 2:
                    self._send(401, b'{"error":"admin_required"}\n',
                               "application/json")
                else:
                    try:
                        import escalation_policy
                        ok = escalation_policy.acknowledge(
                            incident_id, actor="api")
                        self._send(200 if ok else 404,
                                   json.dumps({"acknowledged": ok}).encode(),
                                   "application/json")
                    except Exception as e:
                        self._send(500, b'{"error":"internal"}\n',
                                   "application/json")
            else:
                self._send(404, b'{"error":"not_found"}\n', "application/json")
        finally:
            log.info("post_req", rid=rid, ip=ip, path=path)

    def do_GET(self):
        ip = self._client_ip()
        # Correlation id (X-Request-Id)
        rid = self.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
        self._rid = rid

        # ─── fail2ban-style ban check (first thing, cheapest) ───
        if ip_ban.is_banned(ip):
            self._send(403,
                       b'{"error":"ip_banned","retry_in":"up to 1h"}\n',
                       "application/json",
                       {"X-Ban-Reason": "repeated rate-limit violations",
                        "X-Request-Id": rid})
            return

        # Authenticate FIRST so rate-limit can be tier-aware
        auth_header = self.headers.get("Authorization")
        tier = api_auth.authenticate(auth_header)
        # Pull per-token rate-limit override if present
        token_md = (api_auth.get_token_metadata(auth_header)
                    if auth_header else None)
        override_rpm = (token_md.get("rate_limit_rpm")
                        if token_md else None)
        if tier >= 1 and auth_header:
            bucket_key = "tok:" + auth_header[7:14]
        else:
            bucket_key = "ip:" + ip

        limit = override_rpm or TIER_LIMITS.get(tier, TIER_LIMITS[0])
        if not _rate_ok(bucket_key, tier, override_rpm):
            # Track for fail2ban; if this triggered a ban, log it
            if ip_ban.record_429(ip):
                log.warn("ip_auto_banned", ip=ip,
                         duration_sec=ip_ban.BAN_DURATION)
            self._send(429,
                       f'{{"error":"rate_limited","limit_per_min":{limit},'
                       f'"tier":{tier}}}\n'.encode(),
                       "application/json",
                       {"Retry-After": "60",
                        "X-RateLimit-Limit": str(limit),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int(time.time() + 60))})
            log.warn("rate_limited", key=bucket_key, tier=tier, limit=limit,
                     path=self.path)
            return

        remaining = _rate_remaining(bucket_key, tier, override_rpm)
        rate_headers = {
            "X-RateLimit-Limit":     str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset":     str(int(time.time() + 60)),
            "X-Request-Id":          rid,
        }
        path = self.path.split("?")[0].rstrip("/") or "/"
        # API versioning: alias /api/v1/* → /api/* (canonical),
        # add Deprecation header to unversioned paths
        deprecation_header = {}
        if path.startswith("/api/v1/"):
            path = "/api" + path[len("/api/v1"):]
        elif path.startswith("/api/") and path != "/api":
            # Tell clients there's a versioned alias they should migrate to
            deprecation_header = {
                "Deprecation": "true",
                "Link": f'</api/v1{path[4:]}>; rel="successor-version"',
            }
            rate_headers.update(deprecation_header)
        t0 = time.time()
        # Upgrade #6 (Cloudflare edge cache) + Round-3 #9 (Fastly Surrogate-Key):
        # tag-based purge — invalidate by Surrogate-Key when ledger changes.
        edge_cache = {
            "Cache-Control":
            "public, max-age=15, s-maxage=30, stale-while-revalidate=60",
            "Surrogate-Key": "public-stats track-record"}
        # Tracing: start trace span (Round-3 #10 Honeycomb)
        trace_t0 = time.time()
        try:
            import tracing
        except Exception:
            tracing = None
        try:
            # ─── public ───
            if path == "/api/stats":
                self._send(200, _api_stats_body(), "application/json",
                           {**rate_headers, **edge_cache})
            elif path == "/api/recent":
                self._send(200, _api_recent_body(), "application/json",
                           {**rate_headers, **edge_cache})
            elif path == "/api/regime":
                # Upgrade #5 (Bridgewater regime detection)
                try:
                    import regime
                    self._send(200,
                               json.dumps(regime.current(), default=str,
                                          indent=2).encode(),
                               "application/json",
                               {**rate_headers, **edge_cache})
                except Exception as e:
                    log.error("regime_fail", err=str(e))
                    self._send(500, b'{"error":"regime_unavailable"}\n',
                               "application/json")
            elif path.startswith("/api/signed/"):
                # Upgrade #9 (S3-style pre-signed URLs)
                try:
                    import presigned
                    from urllib.parse import urlparse, parse_qs
                    resource = path[len("/api/signed/"):].split("/")[0]
                    qs = parse_qs(urlparse(self.path).query)
                    expires = (qs.get("expires") or [""])[0]
                    sig = (qs.get("sig") or [""])[0]
                    ok, reason = presigned.verify(resource, expires, sig)
                    if not ok:
                        self._send(403,
                                   f'{{"error":"signed_url_invalid",'
                                   f'"reason":"{reason}"}}\n'.encode(),
                                   "application/json")
                    else:
                        target = presigned.target_for(resource)
                        if target == "/api/v1/stats":
                            self._send(200, _api_stats_body(),
                                       "application/json")
                        elif target == "/api/v1/recent":
                            self._send(200, _api_recent_body(),
                                       "application/json")
                        elif target == "/api/v1/sparkline":
                            cached = _cache_get("sparkline_png")
                            if cached:
                                png = cached[0]
                            else:
                                import sparkline
                                png = sparkline.render_30d()
                                _cache_set("sparkline_png", 300, png,
                                           "image/png")
                            self._send(200, png, "image/png")
                        else:
                            self._send(501,
                                       b'{"error":"not_implemented"}\n',
                                       "application/json")
                except Exception as e:
                    log.error("signed_fail", err=str(e))
                    self._send(500, b'{"error":"internal"}\n',
                               "application/json")
            elif path == "/api/sparkline":
                # Bug #7 fix: server-side cache (5 min). Without this, every
                # public hit re-renders matplotlib → CPU/memory DoS vector.
                try:
                    cached = _cache_get("sparkline_png")
                    if cached:
                        png = cached[0]
                    else:
                        import sparkline
                        png = sparkline.render_30d()
                        _cache_set("sparkline_png", 300, png, "image/png")
                    self._send(200, png, "image/png",
                               {**rate_headers,
                                "Cache-Control": "public, max-age=300"})
                except Exception as e:
                    log.error("sparkline_fail", err=str(e))
                    self._send(500, b'{"error":"render_failed"}\n',
                               "application/json")
            elif path == "/metrics":
                self._send(200, _prometheus_body(),
                           "text/plain; version=0.0.4; charset=utf-8")
            elif path == "/health":
                # Shallow check: 200 if status_server itself is responsive
                self._send(200, b"ok\n", "text/plain")
            elif path == "/health/check":
                # Deep check — full diagnostics
                try:
                    import health_check
                    body = health_check.run_all()
                    code = (200 if body["status"] == "healthy"
                            else 200 if body["status"] == "degraded"
                            else 503)
                    self._send(code,
                               json.dumps(body, default=str, indent=2).encode(),
                               "application/json")
                except Exception as e:
                    self._send(503,
                               json.dumps({"status": "unhealthy",
                                           "error": str(e)}).encode(),
                               "application/json")
            elif path == "/api/levels":
                # Public: armed price levels for the watcher panel.
                # Lightweight file read, cached 10s.
                try:
                    cached = _cache_get("levels_body")
                    if cached:
                        body = cached[0]
                    else:
                        import price_alert
                        levels = price_alert.list_levels()
                        # Pull current prices for proximity calc
                        symbols = sorted({L["symbol"] for L in levels})
                        prices = (price_alert.fetch_prices(symbols)
                                  if symbols else {})
                        # Annotate each level with distance-from-current
                        for L in levels:
                            cur = prices.get(L["symbol"], {}).get("price")
                            if cur and L.get("level"):
                                L["current_price"] = cur
                                L["distance_pct"] = round(
                                    ((L["level"] - cur) / cur) * 100.0, 3)
                        body = json.dumps({
                            "levels": levels,
                            "prices": prices,
                            "count": len(levels),
                        }, default=str).encode()
                        _cache_set("levels_body", 10, body, "application/json")
                    self._send(200, body, "application/json",
                               {**rate_headers,
                                "Cache-Control": "public, max-age=10"})
                except Exception as e:
                    log.error("levels_fail", err=str(e))
                    self._send(500, b'{"error":"levels_unavailable"}\n',
                               "application/json")
            elif path == "/api/price":
                # Public: live BTC/ETH ticker data. Cached 5s to spare Binance.
                try:
                    cached = _cache_get("price_body")
                    if cached:
                        body = cached[0]
                    else:
                        import price_alert
                        prices = price_alert.fetch_prices(
                            ["BTCUSDT", "ETHUSDT"])
                        body = json.dumps({"prices": prices,
                                           "ts": time.time()},
                                          default=str).encode()
                        _cache_set("price_body", 5, body, "application/json")
                    self._send(200, body, "application/json",
                               {**rate_headers,
                                "Cache-Control": "public, max-age=5"})
                except Exception as e:
                    log.error("price_fail", err=str(e))
                    self._send(500, b'{"error":"price_unavailable"}\n',
                               "application/json")
            elif path == "/api/pulse":
                # Public: market pulse extremes (funding, OI, long/short).
                # Cached 60s — the underlying values move slowly.
                # Symbols fetched in parallel; per-symbol Binance calls
                # also parallelized inside fetch_extremes.
                try:
                    cached = _cache_get("pulse_body")
                    if cached:
                        body = cached[0]
                    else:
                        import market_pulse
                        from concurrent.futures import ThreadPoolExecutor
                        syms = ("BTCUSDT", "ETHUSDT")
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            results = list(pool.map(
                                market_pulse.fetch_extremes, syms))
                        out = dict(zip(syms, results))
                        body = json.dumps({"pulse": out,
                                           "ts": time.time()},
                                          default=str).encode()
                        _cache_set("pulse_body", 60, body, "application/json")
                    self._send(200, body, "application/json",
                               {**rate_headers,
                                "Cache-Control": "public, max-age=60"})
                except Exception as e:
                    log.error("pulse_fail", err=str(e))
                    self._send(500, b'{"error":"pulse_unavailable"}\n',
                               "application/json")
            elif path == "/":
                self._send_html()
            # ─── subscriber (tier >= 1) ───
            elif path == "/api/full":
                if tier < 1:
                    self._send(401,
                               b'{"error":"subscription_required",'
                               b'"hint":"send Authorization: Bearer <token>"}\n',
                               "application/json",
                               {"WWW-Authenticate": 'Bearer realm="srs"'})
                else:
                    self._send(200, _api_full_body(), "application/json",
                               rate_headers)
            elif path == "/api/risk_extended":
                if tier < 1:
                    self._send(401,
                               b'{"error":"subscription_required"}\n',
                               "application/json",
                               {"WWW-Authenticate": 'Bearer realm="srs"'})
                else:
                    self._send(200, _api_risk_extended_body(),
                               "application/json", rate_headers)
            elif path == "/api/stream":
                # Round-2 Upgrade #1 (Polygon-style): real-time SSE alert stream
                if tier < 1:
                    self._send(401,
                               b'{"error":"subscription_required"}\n',
                               "application/json")
                else:
                    try:
                        import alert_stream
                        import funnel
                        from urllib.parse import urlparse, parse_qs
                        qs = parse_qs(urlparse(self.path).query)
                        channels, strategy_filt, symbol_filt = (
                            alert_stream.parse_filters(qs))
                        # SSE response
                        self.send_response(200)
                        self.send_header("Content-Type",
                                         "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "keep-alive")
                        self.send_header("X-Request-Id", rid)
                        for k, v in rate_headers.items():
                            self.send_header(k, v)
                        self.end_headers()
                        # Track subscriber's first stream connection
                        if auth_header:
                            tok = auth_header[7:].strip()
                            try:
                                funnel.record(tok, "first_api_call")
                            except Exception:
                                pass
                        # Stream loop
                        for ev_name, data in alert_stream.stream_iter(
                                channels=channels,
                                strategy=strategy_filt,
                                symbol=symbol_filt):
                            try:
                                self.wfile.write(
                                    f"event: {ev_name}\n"
                                    f"data: {data}\n\n".encode())
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                break
                    except Exception as e:
                        log.error("stream_fail", err=str(e))
            elif path == "/api/me/lease":
                # Round-2 Upgrade #7 (Vault): issue short-lived lease
                if tier < 1 or not token_md or not auth_header:
                    self._send(401,
                               b'{"error":"authentication_required"}\n',
                               "application/json")
                else:
                    try:
                        import token_lease
                        from urllib.parse import urlparse, parse_qs
                        qs = parse_qs(urlparse(self.path).query)
                        ttl_min = int((qs.get("ttl_min") or [60])[0])
                        rec = token_lease.issue(auth_header[7:].strip(),
                                                 ttl_min=ttl_min)
                        self._send(200, json.dumps(rec, default=str,
                                                    indent=2).encode(),
                                   "application/json", rate_headers)
                    except Exception as e:
                        log.error("lease_issue_fail", err=str(e))
                        self._send(500, b'{"error":"internal"}\n',
                                   "application/json")
            elif path == "/api/admin/funnel":
                # Round-2 Upgrade #9 (PostHog): operator funnel stats
                # (admin-only — uses HMAC verification path below)
                # Note: this endpoint is special — handled later in admin block
                pass
            elif path == "/api/since":
                # Subscriber: efficient polling diff
                if tier < 1:
                    self._send(401,
                               b'{"error":"subscription_required"}\n',
                               "application/json",
                               {"WWW-Authenticate": 'Bearer realm="srs"'})
                else:
                    try:
                        from urllib.parse import urlparse, parse_qs
                        from ledger import read_all
                        qs = parse_qs(urlparse(self.path).query)
                        after_id = (qs.get("after_id") or [""])[0]
                        after_ts = (qs.get("after_ts") or [""])[0]
                        # Bug #6 fix: cap response to 1000 items max
                        MAX_SINCE_ITEMS = 1000
                        # Iterate ledger; emit records strictly after marker
                        items = []
                        passed = (not after_id and not after_ts)
                        for r in read_all():
                            tid = (r.get("id") or r.get("trade_id")
                                   or f"{r.get('bot','?')}|{r.get('system','?')}|"
                                      f"{r.get('opened_at','?')}")
                            ts_val = (r.get("ts") or r.get("opened_at")
                                      or r.get("exit_time") or "")
                            if not passed:
                                if after_id and tid == after_id:
                                    passed = True
                                    continue
                                if after_ts and ts_val and ts_val > after_ts:
                                    passed = True
                                    items.append(r)
                                    continue
                            else:
                                items.append(r)
                        # Bug #6 fix: cap to most-recent N items
                        if len(items) > MAX_SINCE_ITEMS:
                            items = items[-MAX_SINCE_ITEMS:]
                        latest_id = items[-1].get("id") if items else after_id
                        latest_ts = (items[-1].get("ts") if items
                                     else after_ts)
                        body = {"items": items, "count": len(items),
                                "latest_id": latest_id,
                                "latest_ts": latest_ts,
                                "capped": len(items) >= MAX_SINCE_ITEMS}
                        self._send(200, json.dumps(body, default=str).encode(),
                                   "application/json", rate_headers)
                    except Exception as e:
                        log.error("since_fail", err=str(e))
                        self._send(500, b'{"error":"internal"}\n',
                                   "application/json")
            elif path == "/api/me":
                # Subscriber self-serve: own metadata + prefs.
                # Upgrade #3: include in_grace_period flag.
                if tier < 1 or not token_md:
                    self._send(401,
                               b'{"error":"authentication_required"}\n',
                               "application/json",
                               {"WWW-Authenticate": 'Bearer realm="srs"'})
                else:
                    try:
                        import subscriber_prefs
                        prefs = subscriber_prefs.get(auth_header[7:].strip())
                    except Exception:
                        prefs = {}
                    in_grace = api_auth._is_in_grace(token_md)
                    me_body = {
                        "label": token_md.get("label"),
                        "tier": token_md.get("tier"),
                        "issued_at": token_md.get("issued_at"),
                        "last_used": token_md.get("last_used"),
                        "uses": token_md.get("uses", 0),
                        "expires_at": token_md.get("expires_at"),
                        "in_grace_period": in_grace,
                        "grace_days": api_auth.GRACE_DAYS,
                        "rate_limit_rpm": (override_rpm
                                           or TIER_LIMITS.get(tier)),
                        "preferences": prefs,
                    }
                    self._send(200,
                               json.dumps(me_body, default=str,
                                          indent=2).encode(),
                               "application/json", rate_headers)
            elif path == "/api/me/sign-url":
                # Upgrade #9: subscriber requests pre-signed URL
                if tier < 1:
                    self._send(401,
                               b'{"error":"subscription_required"}\n',
                               "application/json")
                else:
                    try:
                        import presigned
                        from urllib.parse import urlparse, parse_qs
                        qs = parse_qs(urlparse(self.path).query)
                        resource = (qs.get("resource") or ["stats"])[0]
                        ttl_sec = int((qs.get("ttl_sec") or [600])[0])
                        ok, body = presigned.issue(resource, ttl_sec)
                        code = 200 if ok else 400
                        self._send(code,
                                   json.dumps(body, default=str,
                                              indent=2).encode(),
                                   "application/json", rate_headers)
                    except Exception as e:
                        log.error("sign_url_fail", err=str(e))
                        self._send(500, b'{"error":"internal"}\n',
                                   "application/json")
            # ─── admin (tier 2 + HMAC signature required) ───
            elif path.startswith("/api/admin/"):
                # Bug #3 fix: always return uniform error before disclosing tier.
                # Bug #9 fix: replay protection via signature nonce cache.
                secret = auth_header[7:].strip() if auth_header else ""
                ts_hdr = self.headers.get("X-Request-Timestamp", "")
                sig_hdr = self.headers.get("X-Request-Signature", "")

                # Verify everything BEFORE distinguishing token tier
                hmac_ok = bool(secret and ts_hdr and sig_hdr)
                if hmac_ok:
                    hmac_ok, reason = hmac_auth.verify(
                        secret, "GET", path, ts_hdr, sig_hdr, b"")
                else:
                    reason = "missing_headers"
                if hmac_ok and _hmac_seen_already(sig_hdr):
                    hmac_ok = False
                    reason = "replay_detected"

                if tier < 2 or not hmac_ok:
                    log.warn("admin_denied", reason=reason
                             if not hmac_ok else "tier_lt_2",
                             tier=tier, ip=ip)
                    # Uniform error regardless of which check failed:
                    self._send(401,
                               b'{"error":"admin_required"}\n',
                               "application/json",
                               {"WWW-Authenticate": 'Bearer realm="srs"'})
                else:
                    # tier 2 + valid HMAC + not a replay → proceed
                    if path == "/api/admin/audit":
                        try:
                            import audit_log
                            entries = audit_log.tail(100)
                            body = json.dumps({"entries": entries,
                                               "count": len(entries)},
                                              default=str).encode()
                            self._send(200, body, "application/json",
                                       rate_headers)
                        except Exception as e:
                            log.error("admin_audit_fail", err=str(e))
                            self._send(500, b'{"error":"internal"}\n',
                                       "application/json")
                    elif path == "/api/admin/incidents":
                        try:
                            import incident_bundle
                            paths = incident_bundle.list_recent(20)
                            body = json.dumps(
                                {"incidents":
                                    [os.path.basename(p) for p in paths]},
                                default=str).encode()
                            self._send(200, body, "application/json",
                                       rate_headers)
                        except Exception:
                            self._send(500, b'{"error":"internal"}\n',
                                       "application/json")
                    elif path == "/api/admin/subscriptions":
                        try:
                            import subscription_metrics
                            self._send(200, json.dumps(
                                subscription_metrics.compute(),
                                default=str).encode(),
                                "application/json", rate_headers)
                        except Exception as e:
                            log.error("subs_fail", err=str(e))
                            self._send(500, b'{"error":"internal"}\n',
                                       "application/json")
                    elif path == "/api/admin/restarts":
                        log_path = "/home/ubuntu/common/watchdog_restarts.jsonl"
                        entries = []
                        if os.path.exists(log_path):
                            with open(log_path, encoding="utf-8") as f:
                                for line in f:
                                    try: entries.append(json.loads(line))
                                    except Exception: pass
                        self._send(200, json.dumps(
                            {"entries": entries[-20:],
                             "count": len(entries)},
                            default=str).encode(),
                            "application/json", rate_headers)
                    elif path == "/api/admin/bans":
                        # ip_ban already imported at module top
                        try:
                            self._send(200, json.dumps(
                                {"bans": ip_ban.get_active_bans()},
                                default=str).encode(),
                                "application/json", rate_headers)
                        except Exception:
                            self._send(500, b'{"error":"internal"}\n',
                                       "application/json")
                    elif path == "/api/admin/queue":
                        try:
                            import webhook_queue
                            self._send(200, json.dumps(
                                webhook_queue.stats()).encode(),
                                "application/json", rate_headers)
                        except Exception:
                            self._send(500, b'{"error":"internal"}\n',
                                       "application/json")
                    elif path == "/api/admin/funnel":
                        # Round-2 Upgrade #9 (PostHog): subscriber funnel
                        try:
                            import funnel
                            self._send(200, json.dumps(
                                funnel.stats(), default=str,
                                indent=2).encode(),
                                "application/json", rate_headers)
                        except Exception as e:
                            log.error("funnel_fail", err=str(e))
                            self._send(500, b'{"error":"internal"}\n',
                                       "application/json")
                    elif path == "/api/admin/errors":
                        # Round-2 Upgrade #6 (Sentry): grouped error stats
                        try:
                            import error_grouper
                            self._send(200, json.dumps(
                                error_grouper.stats(), default=str,
                                indent=2).encode(),
                                "application/json", rate_headers)
                        except Exception as e:
                            log.error("error_grouper_fail", err=str(e))
                            self._send(500, b'{"error":"internal"}\n',
                                       "application/json")
                    elif path == "/api/admin/leases":
                        # Round-2 Upgrade #7 (Vault): lease stats
                        try:
                            import token_lease
                            self._send(200, json.dumps(
                                token_lease.stats(), default=str,
                                indent=2).encode(),
                                "application/json", rate_headers)
                        except Exception as e:
                            self._send(500, b'{"error":"internal"}\n',
                                       "application/json")
                    elif path == "/api/admin/audit/stream":
                        # Bug #8 fix: cap concurrent SSE connections per token
                        token_pfx = (auth_header[7:14] if auth_header
                                     else "anon")
                        with _SSE_LOCK:
                            if _SSE_COUNTS[token_pfx] >= SSE_PER_TOKEN_CAP:
                                log.warn("sse_concurrent_limit",
                                         token_prefix=token_pfx)
                                self._send(429,
                                           b'{"error":"sse_concurrent_limit",'
                                           b'"max":2}\n',
                                           "application/json",
                                           {"Retry-After": "60"})
                                return
                            _SSE_COUNTS[token_pfx] += 1
                        # SSE: tail audit.jsonl, push new entries
                        try:
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Cache-Control", "no-cache")
                            self.send_header("Connection", "keep-alive")
                            self.send_header("X-Request-Id", rid)
                            for k, v in rate_headers.items():
                                self.send_header(k, v)
                            self.end_headers()
                            audit_path = "/home/ubuntu/common/audit.jsonl"
                            offset = (os.path.getsize(audit_path)
                                      if os.path.exists(audit_path) else 0)
                            self.wfile.write(b": connected\n\n")
                            self.wfile.flush()
                            # Push every new line as event; cap at 5 min
                            deadline = time.time() + 300
                            while time.time() < deadline:
                                if not os.path.exists(audit_path):
                                    time.sleep(1); continue
                                cur = os.path.getsize(audit_path)
                                if cur > offset:
                                    with open(audit_path,
                                              encoding="utf-8") as f:
                                        f.seek(offset)
                                        for line in f:
                                            line = line.strip()
                                            if not line: continue
                                            try:
                                                self.wfile.write(
                                                    f"data: {line}\n\n".encode())
                                                self.wfile.flush()
                                            except (BrokenPipeError,
                                                    ConnectionResetError):
                                                return
                                        offset = f.tell()
                                else:
                                    # heartbeat to keep proxies alive
                                    try:
                                        self.wfile.write(b": ping\n\n")
                                        self.wfile.flush()
                                    except (BrokenPipeError,
                                            ConnectionResetError):
                                        return
                                time.sleep(2)
                        except Exception as e:
                            log.error("sse_fail", err=str(e))
                        finally:
                            with _SSE_LOCK:
                                _SSE_COUNTS[token_pfx] = max(
                                    0, _SSE_COUNTS[token_pfx] - 1)
                    else:
                        self._send(404, b'{"error":"admin_endpoint_not_found"}\n',
                                   "application/json")
            elif path == "/admin":
                # Serve operator.html (requires admin via JS-side HMAC fetches)
                op_path = os.path.join(ROOT, "operator.html")
                if os.path.exists(op_path):
                    with open(op_path, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                else:
                    self._send(404, b"<h1>operator.html missing</h1>",
                               "text/html")
            elif path == "/status":
                # Public uptime status page
                up_path = os.path.join(ROOT, "status_uptime.html")
                if os.path.exists(up_path):
                    with open(up_path, "rb") as f:
                        self._send(200, f.read(),
                                   "text/html; charset=utf-8",
                                   {"Cache-Control":
                                    "no-cache, max-age=30"})
                else:
                    self._send(404, b"<h1>status page missing</h1>",
                               "text/html")
            elif path == "/api/alerts" or path == "/api/v1/alerts":
                # Subscriber: historical alert query
                if tier < 1:
                    self._send(401,
                               b'{"error":"subscription_required"}\n',
                               "application/json",
                               {"WWW-Authenticate": 'Bearer realm="srs"'})
                else:
                    try:
                        import alert_history
                        from urllib.parse import urlparse, parse_qs
                        qs = parse_qs(urlparse(self.path).query)
                        rows = alert_history.query(
                            strategy=(qs.get("strategy") or [None])[0],
                            symbol=(qs.get("symbol") or [None])[0],
                            min_score=int((qs.get("min_score") or [0])[0]) or None,
                            status=(qs.get("status") or [None])[0],
                            since=(qs.get("since") or [None])[0],
                            until=(qs.get("until") or [None])[0],
                            limit=int((qs.get("limit") or [500])[0]),
                        )
                        as_csv = (qs.get("format") or [""])[0] == "csv"
                        if as_csv:
                            self._send(200, alert_history.to_csv(rows),
                                       "text/csv",
                                       {**rate_headers,
                                        "Content-Disposition":
                                        'attachment; filename="alerts.csv"'})
                        else:
                            self._send(200, json.dumps(
                                {"items": rows, "count": len(rows)},
                                default=str).encode(),
                                "application/json", rate_headers)
                    except Exception as e:
                        log.error("alerts_fail", err=str(e))
                        self._send(500, b'{"error":"internal"}\n',
                                   "application/json")
            else:
                self._send(404, b'{"error":"not_found"}\n',
                           "application/json")
        except Exception as e:
            log.error("handler_exception", path=path, ip=ip, err=str(e))
            self._send(500, b'{"error":"internal"}\n', "application/json")
        finally:
            dt = time.time() - t0
            _record_latency(path, dt)
            log.info("req", rid=rid, ip=ip, path=path, tier=tier,
                     ms=round(dt * 1000, 1))
            # Round-3 #10 (Honeycomb): wide-event trace
            if tracing is not None:
                try:
                    tracing.emit("http_request",
                                 trace_id=rid,
                                 method="GET", path=path, tier=tier,
                                 latency_ms=round(dt * 1000, 1),
                                 has_token=bool(auth_header),
                                 client_ip_prefix=ip.split(".")[0] + "."
                                                  if "." in ip else "?")
                except Exception:
                    pass

    def _send_html(self):
        path = os.path.join(ROOT, HTML_FILE)
        if not os.path.exists(path):
            self._send(503, b"<h1>Status page not generated yet.</h1>",
                       "text/html; charset=utf-8")
            return
        with open(path, "rb") as f:
            body = f.read()
        self._send(200, body, "text/html; charset=utf-8",
                   {"Cache-Control": "no-cache, max-age=30"})

    def log_message(self, format, *args):
        pass  # silence stdlib access log; we have structured logs


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    os.chdir(ROOT)
    # Bug #12 fix: bind loopback only by default. External traffic must come
    # via Caddy (:443) or cloudflared tunnel — never raw plaintext :8080.
    httpd = ThreadingServer((BIND_HOST, PORT), StatusHandler)

    def shutdown(*_):
        log.info("shutdown_initiated")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Runtime config reload (SIGHUP + .env mtime watcher)
    try:
        import config_reload
        config_reload.start()
    except Exception as e:
        log.warn("config_reload_unavailable", err=str(e))

    log.info("server_start", port=PORT, threading=True, cache=True,
             rate_limits=TIER_LIMITS)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        log.info("server_stop")


if __name__ == "__main__":
    main()
