"""
scan_mtf.py — Multi-timeframe SMC scanner with INTRA-CANDLE entry.

Called every minute from srs_main.scan_srs(). Strategies fire as soon
as conditions are met against the LIVE current price — they no longer
wait for the higher-timeframe candle to close.

ACTIVE strategies (May 4 2026 onwards — SMC-only stack):
  • SMC_CONFLUENCE          (long)   4h cooldown
  • SMC_CONFLUENCE_SHORT    (short)  4h cooldown

State persists across bot restarts in /home/ubuntu/common/mtf_scan_state.json.
"""
from __future__ import annotations

import os
import sys
import json
import time
import urllib.request
from datetime import datetime, timezone
from typing import List, Dict, Optional

sys.path.insert(0, "/home/ubuntu/bot")
sys.path.insert(0, "/home/ubuntu/common")

import market_data

STATE_FILE = "/home/ubuntu/common/mtf_scan_state.json"

COOLDOWN_SEC = {
    "SMC_CONFLUENCE":        4 * 3600,  # 4 hours
    "SMC_CONFLUENCE_SHORT":  4 * 3600,  # 4 hours
}


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("[MTF] state_save_fail: %s" % e)


def _fetch_live_price(symbol: str = "BTCUSDT") -> Optional[float]:
    """Direct ticker call — much faster than klines for live price."""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        req = urllib.request.Request(url, headers={"User-Agent": "srs-mtf/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return float(data["price"])
    except Exception as e:
        print("[MTF] live_price_fail: %s" % e)
        return None


def _format_signal(sig: Dict) -> Dict:
    """Convert raw strategy signal → paper_trader-compatible dict."""
    interval = sig.get("interval", "1h")
    bars_to_h = {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}
    time_stop_h = sig.get("time_stop_bars", 16) * bars_to_h.get(interval, 1.0)
    side = sig["side"]
    entry = sig["entry"]
    tp_far = entry * 5 if side == "buy" else entry * 0.01
    return {
        "symbol": "BTCUSDT",
        "side": side,
        "interval": interval,
        "entry": entry,
        "sl": sig["sl"],
        "tp1": sig["tp"],
        "tp2": tp_far,
        "tp3": tp_far,
        "strategy_name": sig["strategy"],
        "method": sig["strategy"],
        "probability": 0.4,
        "risk_pct": sig.get("risk_pct", 0.005) * 100,
        "srs_score": 75,
        "srs_grade": "MTF",
        "regime": "MULTI",
        "atr": sig.get("atr", 0),
        "trail_atr": sig.get("trail_atr", 1.5),
        "time_stop_hours": time_stop_h,
        "reason": sig["reason"],
    }


def _on_cooldown(state: dict, name: str, now_ts: float) -> int:
    """Returns seconds remaining on cooldown, or 0 if clear to fire."""
    last = float(state.get("last_fire", {}).get(name, 0))
    cooldown = COOLDOWN_SEC.get(name, 3600)
    if now_ts - last < cooldown:
        return int(cooldown - (now_ts - last))
    return 0


def _mark_fired(state: dict, name: str, now_ts: float) -> None:
    state.setdefault("last_fire", {})[name] = now_ts


def _macro_regime(df_1d) -> str:
    """Classify BTC daily trend → BULL / BEAR / CHOP.

    Added 2026-05-06 to gate the SMC_CONFLUENCE / SMC_CONFLUENCE_SHORT
    calls so we don't fire counter-trend signals that systematically lose.
    Operator's rule: 'if market is buy, the buy will win and the sell will
    lose' — so we only run the side aligned with the daily trend.

      BULL: EMA21 > EMA55 AND EMA21 slope > +0.5% over last 5 daily bars
      BEAR: EMA21 < EMA55 AND EMA21 slope < -0.5% over last 5 daily bars
      CHOP: anything in between → allow both directions (current behavior)
    """
    if df_1d is None or len(df_1d) < 60:
        return "CHOP"  # insufficient data → allow both
    try:
        closes = df_1d["close"]
        ema21 = closes.ewm(span=21, adjust=False).mean()
        ema55 = closes.ewm(span=55, adjust=False).mean()
        e21 = float(ema21.iloc[-1])
        e55 = float(ema55.iloc[-1])
        e21_5d = float(ema21.iloc[-6])
        slope = (e21 - e21_5d) / e21_5d * 100 if e21_5d > 0 else 0
        if e21 > e55 and slope > 0.5:
            return "BULL"
        if e21 < e55 and slope < -0.5:
            return "BEAR"
        return "CHOP"
    except Exception:
        return "CHOP"


def scan() -> List[Dict]:
    """Run all multi-TF strategies against live current price.

    Each strategy fires INTRA-CANDLE when its setup + trigger conditions
    align. Per-strategy cooldown prevents re-fire of same setup.
    """
    try:
        import smc_confluence
    except Exception as e:
        print("[MTF] import_fail: %s" % e)
        return []

    state = _load_state()
    now_ts = time.time()
    signals: List[Dict] = []

    # Live price (fast ticker call)
    live_price = _fetch_live_price("BTCUSDT")
    if live_price is None:
        # Fallback: pull last 15m close
        try:
            df = market_data.download("BTCUSDT", "15m", total_candles=10)
            live_price = float(df["close"].iloc[-1])
        except Exception:
            print("[MTF] no_live_price — abort scan")
            _save_state(state)
            return []

    # ─── DISABLED on May 4 2026 — operator chose SMC-only stack ──────
    # The strategies VWAP_DIP_15M, EMA_CROSS_1H, BREAKOUT_DAILY were
    # archived to operator's PC under srs/strategy_archive/ and are no
    # longer called from here. Their regime entries in regime.py are
    # also empty tuples (() = always blocked), as a defense-in-depth
    # check in case someone re-imports them by accident.

    # ─── Determine macro regime (BULL/BEAR/CHOP) BEFORE direction checks ─
    # Pull just the daily candles once. We need them for regime + the
    # confluence checks, so caching pays off either way.
    dfs = None
    regime = "CHOP"
    long_cd = _on_cooldown(state, "SMC_CONFLUENCE", now_ts)
    short_cd = _on_cooldown(state, "SMC_CONFLUENCE_SHORT", now_ts)
    if long_cd == 0 or short_cd == 0:
        try:
            dfs = {
                "15m": market_data.download("BTCUSDT", "15m", total_candles=300),
                "1h":  market_data.download("BTCUSDT", "1h",  total_candles=200),
                "4h":  market_data.download("BTCUSDT", "4h",  total_candles=200),
                "1d":  market_data.download("BTCUSDT", "1d",  total_candles=200),
            }
            regime = _macro_regime(dfs.get("1d"))
            # Print regime once every 10 min so log isn't spammy
            if int(now_ts) % 600 < 60:
                print("[MTF] macro regime: %s" % regime)
        except Exception as e:
            print("[MTF] data fetch failed: %s — defaulting to CHOP" % e)

    # ─── SMC_CONFLUENCE (bullish, long) ──────────────────────────
    name = "SMC_CONFLUENCE"
    cd = long_cd
    if cd > 0:
        if int(now_ts) % 600 < 60:
            print("[MTF/%s] cooldown %dm" % (name, cd // 60))
    elif regime == "BEAR":
        # Block longs when daily is clearly bearish
        if int(now_ts) % 600 < 60:
            print("[MTF/%s] BLOCKED (regime=BEAR — counter-trend, skip)" % name)
    elif dfs is not None:
        try:
            sig, debug = smc_confluence.confluence_check(dfs, current_price=live_price)
            if sig:
                signals.append(_format_signal(sig))
                _mark_fired(state, name, now_ts)
                print("[MTF/%s] FIRED @ $%.2f: %s" % (name, live_price, sig["reason"]))
            else:
                aligned = debug.get("aligned_tfs", 0)
                if debug.get("near_miss") and int(now_ts) % 300 < 60:
                    print("[MTF/SMC] near-miss (%d/4 aligned): %s" %
                          (aligned, debug.get("reason", "")))
        except Exception as e:
            print("[MTF/%s] error: %s" % (name, e))

    # ─── SMC_CONFLUENCE_SHORT (bearish, short) ───────────────────
    name = "SMC_CONFLUENCE_SHORT"
    cd = short_cd
    if cd > 0:
        if int(now_ts) % 600 < 60:
            print("[MTF/%s] cooldown %dm" % (name, cd // 60))
    elif regime == "BULL":
        # Block shorts when daily is clearly bullish
        if int(now_ts) % 600 < 60:
            print("[MTF/%s] BLOCKED (regime=BULL — counter-trend, skip)" % name)
    elif dfs is not None:
        try:
            sig, debug = smc_confluence.bearish_confluence_check(
                dfs, current_price=live_price)
            if sig:
                signals.append(_format_signal(sig))
                _mark_fired(state, name, now_ts)
                print("[MTF/%s] FIRED @ $%.2f: %s" % (name, live_price, sig["reason"]))
            else:
                aligned = debug.get("aligned_tfs", 0)
                if debug.get("near_miss") and int(now_ts) % 300 < 60:
                    print("[MTF/SMC_SHORT] near-miss (%d/4 aligned): %s" %
                          (aligned, debug.get("reason", "")))
        except Exception as e:
            print("[MTF/%s] error: %s" % (name, e))

    _save_state(state)
    return signals


if __name__ == "__main__":
    sigs = scan()
    print("\n%d signal(s) returned" % len(sigs))
    for s in sigs:
        print("  • %s | %s @ $%.2f → SL $%.2f / TP $%.2f" % (
            s["strategy_name"], s["side"].upper(),
            s["entry"], s["sl"], s["tp1"]))
