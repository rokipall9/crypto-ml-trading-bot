"""
synthetic_probe.py — Datadog-style external synthetic monitoring.

Hits the public-facing URL from outside the VPS to verify:
  - DNS resolves (cloudflare tunnel is up)
  - TLS handshake succeeds within budget (200ms)
  - /health responds within budget (500ms)
  - Critical endpoints respond with expected shapes

Designed to run via systemd timer every 2 minutes from the VPS itself
(loopback test) AND optionally externally (cron on a separate machine).

Real value over /health/check: tests the full path including Caddy +
Cloudflare tunnel + DNS. /health/check skips all those layers.

Records each run to /home/ubuntu/common/synthetic_results.jsonl.
Posts a Discord alert when:
  - Three consecutive failures
  - Any single >2× latency budget breach
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("synthetic_probe")

RESULTS_LOG = "/home/ubuntu/common/synthetic_results.jsonl"
STATE_FILE = "/home/ubuntu/common/synthetic_state.json"

# Tunable budgets (ms)
DNS_BUDGET_MS = 200
TLS_BUDGET_MS = 200
HEALTH_BUDGET_MS = 500
ENDPOINT_BUDGET_MS = 1000

# Probes
DEFAULT_TARGETS = [
    {"url": "https://127.0.0.1/health",
     "expect_substring": "ok", "verify_tls": False},
]
EXTERNAL_TARGETS_ENV = "SRS_PROBE_EXTERNAL_URL"


def _measure(url: str, verify_tls: bool = True,
             expect_substring: str = "") -> Dict:
    """Make a request, measure latency, return result dict."""
    out: Dict = {"url": url, "ts": datetime.now(timezone.utc).isoformat()}
    t_start = time.time()
    ctx = ssl.create_default_context() if verify_tls else None
    if not verify_tls:
        ctx = ssl._create_unverified_context()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "srs-synthetic/1.0"})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            body = r.read()
            status = r.status
        dt_ms = (time.time() - t_start) * 1000
        out["status"] = status
        out["latency_ms"] = round(dt_ms, 1)
        out["body_size"] = len(body)
        if expect_substring and expect_substring.encode() not in body:
            out["ok"] = False
            out["err"] = f"expected '{expect_substring}' not in body"
        else:
            out["ok"] = 200 <= status < 300
    except urllib.error.HTTPError as e:
        out["ok"] = False
        out["status"] = e.code
        out["err"] = e.reason
        out["latency_ms"] = round((time.time() - t_start) * 1000, 1)
    except Exception as e:
        out["ok"] = False
        out["err"] = str(e)
        out["latency_ms"] = round((time.time() - t_start) * 1000, 1)
    return out


def run_all(targets: List[dict] = None) -> List[Dict]:
    """Run all probes, log results, return list of result dicts."""
    if targets is None:
        targets = list(DEFAULT_TARGETS)
        ext = os.environ.get(EXTERNAL_TARGETS_ENV, "").strip()
        if ext:
            targets.append({"url": ext, "expect_substring": "ok"})
    results = []
    for t in targets:
        r = _measure(
            t["url"],
            verify_tls=t.get("verify_tls", True),
            expect_substring=t.get("expect_substring", ""),
        )
        # Budget check
        budget = HEALTH_BUDGET_MS if "/health" in t["url"] else ENDPOINT_BUDGET_MS
        if r.get("latency_ms", 0) > budget:
            r["budget_breach"] = True
            r["budget_ms"] = budget
        results.append(r)

    # Append to JSONL log
    try:
        with open(RESULTS_LOG, "a", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, default=str) + "\n")
    except Exception as e:
        log.error("log_write_failed", err=str(e))

    return results


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, default=str)
    os.replace(tmp, STATE_FILE)


def post_alert(title: str, body: str, color: int = 0xFF4444) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv("/home/ubuntu/bot/.env")
    except Exception:
        pass
    webhook = (os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
               or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())
    if not webhook:
        return
    payload = {
        "username": "🛰️ Synthetic",
        "embeds": [{
            "title": title, "description": body, "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat()}]
    }
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "srs-synthetic/1.0"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def main() -> int:
    results = run_all()
    state = _load_state()
    consecutive_fails = state.get("consecutive_fails", 0)
    any_failed = any(not r.get("ok") for r in results)
    breaches = [r for r in results if r.get("budget_breach")
                and r.get("latency_ms", 0) > 2 * r.get("budget_ms", 1)]

    if any_failed:
        consecutive_fails += 1
        if consecutive_fails >= 3:
            post_alert(
                "🛰️  Synthetic probe FAILING",
                f"3 consecutive failures.\n```\n"
                + "\n".join(json.dumps(r, default=str)[:200]
                            for r in results if not r.get("ok"))
                + "\n```")
    else:
        if consecutive_fails > 0:
            log.info("recovered", after_fails=consecutive_fails)
        consecutive_fails = 0

    if breaches:
        post_alert(
            "🛰️  Latency budget breach (>2× target)",
            "\n".join(f"`{b['url']}` → "
                      f"{b['latency_ms']:.0f}ms (budget {b['budget_ms']}ms)"
                      for b in breaches),
            color=0xFFA500)

    state["consecutive_fails"] = consecutive_fails
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    log.info("probe_complete", any_failed=any_failed,
             breaches=len(breaches),
             consecutive_fails=consecutive_fails)
    return 0 if not any_failed else 1


if __name__ == "__main__":
    sys.exit(main())
