"""market_intel.py — pulls funding rate + OI from Binance Futures.
Used by heartbeats, morning briefing, and the multi-exchange monitor."""
from __future__ import annotations
import json
import urllib.request
from typing import Dict, Optional


def fetch_funding_rate(symbol: str = "BTCUSDT") -> Optional[Dict]:
    """Latest funding rate from Binance Futures."""
    try:
        url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}"
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read().decode())
            return {
                "rate": float(data["lastFundingRate"]) * 100,  # percentage
                "next_funding_ts": int(data.get("nextFundingTime", 0)),
                "mark_price": float(data["markPrice"]),
                "index_price": float(data["indexPrice"]),
            }
    except Exception:
        return None


def fetch_open_interest(symbol: str = "BTCUSDT") -> Optional[Dict]:
    """Open interest from Binance Futures."""
    try:
        url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read().decode())
            return {
                "oi": float(data["openInterest"]),
                "ts": int(data.get("time", 0)),
            }
    except Exception:
        return None


def fetch_long_short_ratio(symbol: str = "BTCUSDT") -> Optional[Dict]:
    """Top trader long/short ratio (Binance Futures)."""
    try:
        url = f"https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol={symbol}&period=1h&limit=1"
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read().decode())
            if data:
                d = data[0]
                return {
                    "long_pct": float(d["longAccount"]) * 100,
                    "short_pct": float(d["shortAccount"]) * 100,
                    "ratio": float(d["longShortRatio"]),
                }
    except Exception:
        pass
    return None


def fetch_multi_exchange_btc() -> Dict[str, Optional[float]]:
    """BTC price from Binance/Bybit/Coinbase. Returns dict, missing = None."""
    out = {"binance": None, "bybit": None, "coinbase": None}

    # Binance
    try:
        with urllib.request.urlopen(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5
        ) as r:
            out["binance"] = float(json.loads(r.read().decode())["price"])
    except Exception:
        pass

    # Bybit
    try:
        with urllib.request.urlopen(
            "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT", timeout=5
        ) as r:
            data = json.loads(r.read().decode())
            if data.get("retCode") == 0 and data.get("result", {}).get("list"):
                out["bybit"] = float(data["result"]["list"][0]["lastPrice"])
    except Exception:
        pass

    # Coinbase
    try:
        with urllib.request.urlopen(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5
        ) as r:
            out["coinbase"] = float(json.loads(r.read().decode())["data"]["amount"])
    except Exception:
        pass

    return out


def summarize_funding(fr: Optional[Dict]) -> str:
    """Human-readable funding rate summary."""
    if not fr:
        return "—"
    rate = fr["rate"]
    if abs(rate) < 0.005:
        regime = "neutral"
    elif rate > 0.01:
        regime = "🔴 longs paying heavily"
    elif rate > 0:
        regime = "🟢 longs paying"
    elif rate < -0.01:
        regime = "🔴 shorts paying heavily"
    else:
        regime = "🟢 shorts paying"
    return f"{rate:+.4f}% · {regime}"
