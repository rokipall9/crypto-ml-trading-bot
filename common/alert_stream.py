"""
alert_stream.py — Polygon.io-style real-time alert streaming via SSE.

Subscribers connect to /api/v1/stream once and get pushed every new
alert/close as it lands — sub-second latency vs polling.

Channel subscriptions (Polygon-style query params):
  ?channels=alert,close_tp     — only TP closes + new alerts
  ?strategy=BREAKOUT_4H        — only that strategy
  ?symbol=BTCUSDT              — only that symbol

Backend: tails forward_results.jsonl mtime, emits new lines as SSE events.
Hook: status_server's /api/v1/stream handler delegates here.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Iterator, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("alert_stream")

LEDGER_PATH = "/home/ubuntu/common/forward_results.jsonl"
HEARTBEAT_INTERVAL = 15.0
MAX_DURATION = 600.0   # 10 min max per connection
POLL_INTERVAL = 1.0


def filter_record(rec: dict, channels: Optional[set] = None,
                  strategy: Optional[str] = None,
                  symbol: Optional[str] = None) -> bool:
    """Apply Polygon-style channel filters."""
    if strategy and rec.get("system") != strategy:
        return False
    if symbol and rec.get("symbol") != symbol:
        return False
    if channels:
        ev = rec.get("event", "alert")
        status = rec.get("status", "")
        if ev == "close" and status:
            ev = f"close_{status.lower()}"
        if ev not in channels:
            return False
    return True


def stream_iter(channels: Optional[set] = None,
                strategy: Optional[str] = None,
                symbol: Optional[str] = None
                ) -> Iterator[Tuple[str, str]]:
    """Yields (event_name, json_data) tuples as new ledger lines appear.
    Caller is the SSE writer loop in status_server."""
    yield ("connected", json.dumps({
        "filters": {"channels": list(channels or []),
                    "strategy": strategy, "symbol": symbol},
        "ts": time.time(),
    }))

    if not os.path.exists(LEDGER_PATH):
        # Sit idle until the file appears
        offset = 0
    else:
        offset = os.path.getsize(LEDGER_PATH)

    deadline = time.time() + MAX_DURATION
    last_heartbeat = time.time()

    while time.time() < deadline:
        # Heartbeat
        if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
            yield ("heartbeat", json.dumps({"ts": time.time()}))
            last_heartbeat = time.time()

        if not os.path.exists(LEDGER_PATH):
            time.sleep(POLL_INTERVAL)
            continue
        cur_size = os.path.getsize(LEDGER_PATH)
        if cur_size <= offset:
            time.sleep(POLL_INTERVAL)
            continue
        # Read new bytes
        try:
            with open(LEDGER_PATH, encoding="utf-8") as f:
                f.seek(offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if filter_record(rec, channels, strategy, symbol):
                        ev_name = rec.get("event", "alert")
                        status = rec.get("status", "")
                        if ev_name == "close" and status:
                            ev_name = f"close_{status.lower()}"
                        yield (ev_name, json.dumps(rec, default=str))
                offset = f.tell()
        except Exception as e:
            log.error("stream_read_error", err=str(e))
            time.sleep(POLL_INTERVAL)


def parse_filters(qs: dict) -> Tuple[Optional[set], Optional[str], Optional[str]]:
    channels_raw = (qs.get("channels") or [""])[0]
    channels = set(c.strip() for c in channels_raw.split(",") if c.strip()) \
        if channels_raw else None
    strategy = (qs.get("strategy") or [None])[0]
    symbol = (qs.get("symbol") or [None])[0]
    return channels, strategy, symbol
