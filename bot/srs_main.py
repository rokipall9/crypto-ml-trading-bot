"""
srs_main.py — 4H Strategy Scanner (4 long strategies, BTC-only)
====================================================================
BULL regime: 4 validated longs (breakout, momentum, panic-dip, pullback)
BEAR + CHOP: no trading

Called every 15 minutes. Only generates signals when a 4H candle
has just closed (hours 0, 4, 8, 12, 16, 20 UTC).
Posts a watch-channel heartbeat every 4H close (see bot/discord_webhook.py).
"""
from __future__ import annotations

import sys
import time
import json
from datetime import datetime, timezone

sys.path.insert(0, "/home/ubuntu/bot")

import market_data
from strategy_engine import StrategyEngine

# ── Watch heartbeat (fail-safe import) ───────────────────────────────
try:
    from bot.discord_webhook import send_watch_heartbeat as _send_watch
except Exception as _e:
    print("[4H] Watch heartbeat import failed: %s" % _e)
    def _send_watch(*args, **kwargs):  # no-op fallback
        return False


def _safe_heartbeat(**kw) -> None:
    """Wrap watch webhook call so a webhook failure can never crash the scan."""
    try:
        _send_watch(**kw)
    except Exception as exc:
        print("[4H] Heartbeat send failed: %s" % exc)

# ── Singleton engine (persists for circuit breaker state) ─────────────
_engine = StrategyEngine()
_last_4h_check = ""  # Track last 4H candle we checked to avoid duplicates

# ── Fear & Greed cache ───────────────────────────────────────────────
_FG_CACHE: dict = {"value": 50, "ts": 0}
_FG_TTL = 7200  # 2 hours

# ── All 4H strategy names (for trailing stop updates) ────────────────
# 4 validated long strategies. S5 (VOL_RESET) and S6 (REJECTION_SHORT)
# dropped Apr 17 after solo audit. Kept out of this set so the trailing
# stop handler ignores any stale open positions under those names.
_4H_STRATEGIES = frozenset([
    "BREAKOUT_4H", "VOL_MOMENTUM_4H", "PANIC_DIP_4H",
    "EMA_PULLBACK_4H",
])


def _get_fear_greed() -> int:
    """Fetch Fear & Greed Index. Cached 2 hours."""
    import urllib.request
    now = time.time()
    if _FG_CACHE["ts"] and now - _FG_CACHE["ts"] < _FG_TTL:
        return _FG_CACHE["value"]
    try:
        with urllib.request.urlopen(
            "https://api.alternative.me/fng/?limit=1", timeout=5
        ) as resp:
            data = json.loads(resp.read().decode())
            val = int(data["data"][0]["value"])
            _FG_CACHE["value"] = val
            _FG_CACHE["ts"] = now
            return val
    except Exception:
        return _FG_CACHE.get("value", 50)


def scan_srs(balance: float = 10000.0) -> list:
    """
    Main scan function. Called every 15m candle close.
    Returns list of signal dicts formatted for paper_trader.open_paper_trade().
    """
    global _last_4h_check
    # MTF_PATCH_v1: pull multi-timeframe signals (15m/1h/daily/SMC).
    # Each strategy self-dedupes by its own candle close.
    try:
        import sys as _sys
        _sys.path.insert(0, "/home/ubuntu/common")
        import scan_mtf as _mtf
        _mtf_signals = _mtf.scan()
    except Exception as _e:
        print("[MTF] scan_failed: %s" % _e)
        _mtf_signals = []

    now = datetime.now(timezone.utc)
    now_str = now.strftime("%H:%M")
    hour = now.hour

    print("[4H] Scan at %s UTC (hour=%d)" % (now_str, hour))

    # ── Only check for NEW signals at 4H candle close ─────────────
    is_4h_close = hour % 4 == 0

    if not is_4h_close:
        print("[4H] Not a 4H candle close (hour=%d). Skip signal check." % hour)
        return _mtf_signals

    # Deduplicate: don't check same 4H candle twice
    candle_key = now.strftime("%Y-%m-%d-%H")
    if candle_key == _last_4h_check:
        print("[4H] Already checked candle %s. Skip." % candle_key)
        return _mtf_signals

    _last_4h_check = candle_key

    # ── Gate 1: Fear & Greed ─────────────────────────────────────────
    fg = _get_fear_greed()
    if fg < 15:
        print("[4H] BLOCKED: extreme fear (F&G=%d)" % fg)
        _safe_heartbeat(regime="?", price=0, atr=0, fg=fg, reasons=[],
                        blocked="Extreme fear (F&G=%d < 15)" % fg)
        return _mtf_signals
    if fg > 85:
        print("[4H] BLOCKED: extreme greed (F&G=%d)" % fg)
        _safe_heartbeat(regime="?", price=0, atr=0, fg=fg, reasons=[],
                        blocked="Extreme greed (F&G=%d > 85)" % fg)
        return _mtf_signals

    # ── Download data ────────────────────────────────────────────────
    try:
        df_4h = market_data.download("BTCUSDT", "4h", total_candles=500)
        df_1d = market_data.download("BTCUSDT", "1d", total_candles=200)
    except Exception as e:
        print("[4H] Data download failed: %s" % e)
        return _mtf_signals

    if len(df_4h) < 100 or len(df_1d) < 60:
        print("[4H] Insufficient data (4h=%d, 1d=%d)" % (len(df_4h), len(df_1d)))
        return _mtf_signals

    # ── Run strategy engine ──────────────────────────────────────────
    _engine.update_data(df_4h, df_1d)
    regime = _engine.get_regime()
    raw_signals = _engine.check_signals()

    if not raw_signals:
        price = float(df_4h.iloc[-1]["close"])
        reasons = _engine.last_reasons
        reason_str = reasons[0] if reasons else "no conditions met"
        print("[4H] No signal. Regime=%s | Price=$%.0f | %s | F&G=%d" % (
            regime, price, reason_str, fg))
        _safe_heartbeat(regime=regime, price=price,
                        atr=_engine.get_current_atr(), fg=fg,
                        reasons=list(reasons))
        return _mtf_signals

    # ── Format for paper trader ──────────────────────────────────────
    paper_signals = []
    for sig in raw_signals:
        side = sig["side"]

        # TP dummy: unreachable in the correct direction
        if side == "buy":
            tp_dummy = sig["entry"] * 5       # unreachable high
        else:
            tp_dummy = sig["entry"] * 0.01    # unreachable low

        # Regime label
        if side == "sell":
            grade = "BEAR"
            regime_label = "BEAR"
        else:
            grade = "UPTREND"
            regime_label = "UPTREND"

        paper_sig = {
            "symbol": "BTCUSDT",
            "side": side,
            "interval": "4h",
            "entry": sig["entry"],
            "sl": sig["sl"],
            "tp1": tp_dummy,
            "tp2": tp_dummy,
            "tp3": tp_dummy,
            "strategy_name": sig["strategy"],
            "method": sig["strategy"],
            "probability": 0.35,
            "risk_pct": sig["risk_pct"] * 100,
            "srs_score": 80,
            "srs_grade": grade,
            "regime": regime_label,
            "atr": sig["atr"],
            "trail_atr": sig["trail_atr"],
            "time_stop_hours": sig["time_stop_bars"] * 4,
            "reason": sig["reason"],
        }

        print("[4H] SIGNAL: [%s] %s BTC @ $%.0f | SL=$%.0f | trail=%.1fx ATR | regime=%s | %s" % (
            sig["strategy"], sig["side"].upper(), sig["entry"],
            sig["sl"], sig["trail_atr"], regime, sig["reason"]))

        paper_signals.append(paper_sig)

    # Heartbeat with signal info (pipe raw engine signals, not paper_sig wrappers)
    try:
        _price = float(df_4h.iloc[-1]["close"])
    except Exception:
        _price = 0.0
    _safe_heartbeat(regime=regime, price=_price,
                    atr=_engine.get_current_atr(), fg=fg,
                    reasons=[], signals=raw_signals)

    return _mtf_signals + paper_signals


def get_engine():
    """Return engine instance for trailing stop updates."""
    return _engine


def record_result(strategy: str, won: bool):
    """Call from paper_trader close handler to update circuit breaker."""
    _engine.record_trade_result(strategy, won)


def update_trailing_stops(open_trades: list, current_price: float) -> list:
    """
    Update trailing stops for all open 4H strategy trades.
    Called from paper_monitor every 2 minutes.

    Handles both LONG and SHORT trades:
      LONG:  trail = price - trail_atr * ATR, moves UP only
      SHORT: trail = price + trail_atr * ATR, moves DOWN only

    Returns list of (trade_id, new_sl) tuples for trades that were updated.
    """
    updates = []

    atr = _engine.get_current_atr()
    if atr <= 0:
        return updates

    for trade in open_trades:
        strategy = trade.get("strategy", trade.get("method", ""))
        if strategy not in _4H_STRATEGIES:
            continue

        side = trade.get("side", "buy")
        trail_atr = trade.get("trail_atr", 3.0)
        current_sl = trade.get("sl_current", trade.get("sl_original", 0))

        if side == "buy":
            # LONG: trail moves UP
            new_trail = current_price - trail_atr * atr
            if new_trail > current_sl:
                trade["sl_current"] = round(new_trail, 2)
                updates.append((trade.get("id", "?"), round(new_trail, 2)))

        elif side == "sell":
            # SHORT: trail moves DOWN
            new_trail = current_price + trail_atr * atr
            if new_trail < current_sl:
                trade["sl_current"] = round(new_trail, 2)
                updates.append((trade.get("id", "?"), round(new_trail, 2)))

    return updates


# ── Standalone test ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  4H STRATEGY SCANNER — Manual Test")
    print("=" * 60)

    _last_4h_check = ""

    try:
        df_4h = market_data.download("BTCUSDT", "4h", total_candles=500)
        df_1d = market_data.download("BTCUSDT", "1d", total_candles=200)
    except Exception as e:
        print("Data download failed: %s" % e)
        sys.exit(1)

    _engine.update_data(df_4h, df_1d)
    regime = _engine.get_regime()
    signals = _engine.check_signals()

    print("\nRegime: %s" % _engine.get_trend_status())

    if signals:
        for sig in signals:
            print("\n--- SIGNAL ---")
            print("  Strategy: %s" % sig["strategy"])
            print("  Side:     %s" % sig["side"].upper())
            print("  Entry:    $%.2f" % sig["entry"])
            print("  SL:       $%.2f" % sig["sl"])
            print("  Trail:    %.1fx ATR" % sig["trail_atr"])
            print("  ATR:      $%.0f" % sig["atr"])
            print("  Risk:     %.1f%%" % (sig["risk_pct"] * 100))
            print("  Reason:   %s" % sig["reason"])
    else:
        print("\nNo signals.")
        for r in _engine.last_reasons:
            print("  - %s" % r)

    print("\nATR: $%.0f" % _engine.get_current_atr())
    print("Status: %s" % _engine.status())
