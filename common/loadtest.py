"""
loadtest.py — Stdlib-only HTTP load tester for status_server.

Records throughput + p50/p95/p99 latency + error rate against an endpoint.
Multi-threaded. No external deps.

Usage:
    python3 loadtest.py https://127.0.0.1/health -c 50 -n 5000
    python3 loadtest.py https://127.0.0.1/api/stats -c 20 -n 1000

Output: human-readable report + JSON line for trending.
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import List


def make_request(url: str, ctx, headers: dict) -> tuple:
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0
    return (code, time.time() - t0)


def worker(url, ctx, headers, count, results, lock):
    out = []
    for _ in range(count):
        out.append(make_request(url, ctx, headers))
    with lock:
        results.extend(out)


def percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0
    idx = int(len(sorted_vals) * pct)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("-c", "--concurrency", type=int, default=10)
    p.add_argument("-n", "--requests", type=int, default=1000)
    p.add_argument("-H", "--header", action="append", default=[],
                   help='Extra header "Key: Value"')
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS verification (for self-signed)")
    args = p.parse_args()

    ctx = ssl._create_unverified_context() if args.insecure else None
    headers = {}
    for h in args.header:
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip()] = v.strip()

    per_worker = max(1, args.requests // args.concurrency)
    total = per_worker * args.concurrency
    results = []
    lock = threading.Lock()
    threads = []

    print(f"Load test: {args.url}")
    print(f"Concurrency: {args.concurrency}  ·  Total: {total} requests")
    print("Running...")

    t_start = time.time()
    for _ in range(args.concurrency):
        t = threading.Thread(target=worker,
                             args=(args.url, ctx, headers, per_worker,
                                   results, lock), daemon=True)
        t.start(); threads.append(t)
    for t in threads:
        t.join()
    t_elapsed = time.time() - t_start

    codes = [r[0] for r in results]
    latencies = sorted(r[1] for r in results)
    n = len(results)
    n_2xx = sum(1 for c in codes if 200 <= c < 300)
    n_429 = codes.count(429)
    n_4xx = sum(1 for c in codes if 400 <= c < 500 and c != 429)
    n_5xx = sum(1 for c in codes if 500 <= c < 600)
    n_err = sum(1 for c in codes if c == 0)

    rps = n / t_elapsed
    p50 = percentile(latencies, 0.50) * 1000
    p95 = percentile(latencies, 0.95) * 1000
    p99 = percentile(latencies, 0.99) * 1000
    pmax = (max(latencies) if latencies else 0) * 1000

    print()
    print(f"Total time:       {t_elapsed:.2f} s")
    print(f"Throughput:       {rps:.0f} req/sec")
    print(f"Status 2xx:       {n_2xx}  ({n_2xx/n*100:.1f}%)")
    print(f"Status 429:       {n_429}  ({n_429/n*100:.1f}%)")
    print(f"Status 4xx:       {n_4xx}")
    print(f"Status 5xx:       {n_5xx}")
    print(f"Connection err:   {n_err}")
    print(f"Latency p50:      {p50:.1f} ms")
    print(f"Latency p95:      {p95:.1f} ms")
    print(f"Latency p99:      {p99:.1f} ms")
    print(f"Latency max:      {pmax:.1f} ms")

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "url": args.url, "concurrency": args.concurrency,
        "total": n, "elapsed_s": round(t_elapsed, 2),
        "rps": round(rps, 1),
        "p50_ms": round(p50, 1), "p95_ms": round(p95, 1),
        "p99_ms": round(p99, 1), "max_ms": round(pmax, 1),
        "status_2xx": n_2xx, "status_429": n_429,
        "status_4xx": n_4xx, "status_5xx": n_5xx, "errors": n_err,
    }
    print()
    print("# Trend record (append to /home/ubuntu/common/loadtest_history.jsonl):")
    print(json.dumps(record, default=str))
    return 0 if n_5xx == 0 and n_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
