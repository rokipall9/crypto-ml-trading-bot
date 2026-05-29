"""
market_pulse.py — Extreme-condition alerter for funding/OI/long-short.

Independent of strategies. Fires when market conditions reach extremes
that historically precede big moves:

  - Funding rate > 0.05% (very long crowd, short-squeeze risk)
  - Funding rate < -0.05% (very short crowd, long-squeeze risk)
  - 24h price change > 5% in either direction
  - Long/short ratio > 2.5 (over-leveraged longs)
  - Long/short ratio < 0.4 (over-leveraged shorts)

Each extreme has a 2h cooldown (no spam). Fires Discord embed on detect.

Run via systemd timer every 30 min.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("market_pulse")

STATE_FILE = "/home/ubuntu/common/market_pulse_state.json"
COOLDOWN_SEC = 2 * 3600

# Extreme thresholds
FUNDING_HIGH_PCT = 0.05          # +0.05% funding = very long
FUNDING_LOW_PCT = -0.05
PRICE_MOVE_PCT = 5.0              # 24h |%change|
LONG_SHORT_HIGH = 2.5
LONG_SHORT_LOW = 0.4

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass

WEBHOOK = (os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
           or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())

TV_CHART_URL = os.environ.get(
    "TRADINGVIEW_CHART_URL",
    "https://www.tradingview.com/chart/pViMM9Zt/?symbol=BYBIT%3ABTCUSDT.P",
).strip()


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, default=str)
    os.replace(tmp, STATE_FILE)


def _fetch_funding(symbol: str) -> dict:
    out: dict = {}
    try:
        url = (f"https://fapi.binance.com/fapi/v1/premiumIndex"
               f"?symbol={symbol}")
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        out["funding_pct"] = float(data["lastFundingRate"]) * 100
        out["mark_price"] = float(data["markPrice"])
    except Exception as e:
        log.warn("funding_fetch_failed", symbol=symbol, err=str(e))
    return out


def _fetch_ticker(symbol: str) -> dict:
    out: dict = {}
    try:
        url = (f"https://api.binance.com/api/v3/ticker/24hr"
               f"?symbol={symbol}")
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        out["price"] = float(data["lastPrice"])
        out["change_24h_pct"] = float(data["priceChangePercent"])
    except Exception as e:
        log.warn("ticker_fetch_failed", symbol=symbol, err=str(e))
    return out


def _fetch_long_short(symbol: str) -> dict:
    out: dict = {}
    try:
        url = (f"https://fapi.binance.com/futures/data/topLongShortPositionRatio"
               f"?symbol={symbol}&period=1h&limit=1")
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        if data:
            out["long_short_ratio"] = float(data[0]["longShortRatio"])
            out["long_pct"] = float(data[0]["longAccount"]) * 100
            out["short_pct"] = float(data[0]["shortAccount"]) * 100
    except Exception as e:
        log.warn("long_short_fetch_failed", symbol=symbol, err=str(e))
    return out


def _fetch_extremes(symbol: str = "BTCUSDT") -> Dict:
    """Pull funding, ticker, long-short from Binance — IN PARALLEL.

    Cuts cold-fetch latency from ~3×400ms (sequential) to ~400ms
    (parallel). Used by check_and_fire and the public dashboard.
    """
    from concurrent.futures import ThreadPoolExecutor
    out: Dict = {"symbol": symbol}
    with ThreadPoolExecutor(max_workers=3) as pool:
        funding_fut = pool.submit(_fetch_funding, symbol)
        ticker_fut = pool.submit(_fetch_ticker, symbol)
        ls_fut = pool.submit(_fetch_long_short, symbol)
        out.update(funding_fut.result())
        out.update(ticker_fut.result())
        out.update(ls_fut.result())
    return out


def fetch_extremes(symbol: str = "BTCUSDT") -> Dict:
    """Public wrapper — same as _fetch_extremes but stable API for callers
    outside this module (e.g. status_server)."""
    return _fetch_extremes(symbol)


def _post_pulse(title: str, body: str, color: int = 0xFFA500) -> bool:
    if not WEBHOOK:
        return False
    body_with_link = body + f"\n\n📊 [Open chart]({TV_CHART_URL})"
    payload = {
        "username": "📡 Market Pulse",
        "embeds": [{"title": title,
                    "url": TV_CHART_URL,
                    "description": body_with_link,
                    "color": color,
                    "timestamp": datetime.now(timezone.utc).isoformat()}],
    }
    try:
        req = urllib.request.Request(
            WEBHOOK, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "srs-market-pulse/1.0"})
        urllib.request.urlopen(req, timeout=5).read()
        return True
    except Exception as e:
        log.error("post_fail", err=str(e))
        return False


def check_and_fire(symbols: List[str] = None) -> dict:
    if symbols is None:
        symbols = ["BTCUSDT", "ETHUSDT"]
    state = _load_state()
    now = time.time()
    fired = 0
    for sym in symbols:
        m = _fetch_extremes(sym)
        if not m:
            continue

        # Build trigger checks
        triggers = []
        funding = m.get("funding_pct")
        if funding is not None:
            if funding >= FUNDING_HIGH_PCT:
                triggers.append(("funding_high",
                                 f"💸 **{sym} funding {funding:+.3f}%** — "
                                 f"longs paying shorts; squeeze risk DOWN"))
            elif funding <= FUNDING_LOW_PCT:
                triggers.append(("funding_low",
                                 f"💸 **{sym} funding {funding:+.3f}%** — "
                                 f"shorts paying longs; squeeze risk UP"))
        change = m.get("change_24h_pct")
        if change is not None:
            if change >= PRICE_MOVE_PCT:
                triggers.append(("big_move_up",
                                 f"📈 **{sym} +{change:.2f}% / 24h** — "
                                 f"price ${m.get('price', 0):,.0f}"))
            elif change <= -PRICE_MOVE_PCT:
                triggers.append(("big_move_down",
                                 f"📉 **{sym} {change:+.2f}% / 24h** — "
                                 f"price ${m.get('price', 0):,.0f}"))
        ls = m.get("long_short_ratio")
        if ls is not None:
            if ls >= LONG_SHORT_HIGH:
                triggers.append(("ls_high",
                                 f"⚖️ **{sym} top traders {ls:.2f}× long** — "
                                 f"crowded long, contrarian short signal"))
            elif ls <= LONG_SHORT_LOW:
                triggers.append(("ls_low",
                                 f"⚖️ **{sym} top traders {ls:.2f}× long** — "
                                 f"crowded short, contrarian long signal"))

        for trigger_id, body in triggers:
            key = f"{sym}:{trigger_id}"
            last = state.get(key, 0)
            if now - last < COOLDOWN_SEC:
                continue
            color = (0xFF4444 if "down" in trigger_id or "low" in trigger_id
                     else 0x00C851)
            ok = _post_pulse(f"📡 Market Pulse · {sym}", body, color)
            if ok:
                state[key] = now
                fired += 1
                log.info("pulse_fired", trigger=key)

    _save_state(state)
    log.info("pulse_complete", symbols=len(symbols), fired=fired)
    return {"symbols": len(symbols), "fired": fired}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        print(json.dumps(check_and_fire(), indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "fetch":
        sym = sys.argv[2] if len(sys.argv) > 2 else "BTCUSDT"
        print(json.dumps(_fetch_extremes(sym), indent=2, default=str))
    else:
        print("Usage: market_pulse.py [check|fetch <SYMBOL>]")
