"""
regime.py — Bridgewater-style market regime detector.

Classifies current market state into 4 regimes:
  TRENDING_UP   — strong directional uptrend (EMA21 > EMA55, slope > 0.5%/bar)
  TRENDING_DOWN — strong directional downtrend (mirror)
  RANGING       — sideways, low realized vol, EMAs flat
  VOLATILE      — high realized vol, no clear direction

Each strategy can declare which regimes it should be active in.
Strategies disabled in the wrong regime never fire — equivalent to a
soft circuit-breaker that activates BEFORE the strategy degrades.

Reads recent BTCUSDT klines from Binance (rate-limit friendly: 1 call/min).
Cached for 60s. Exposed via:
  - /api/v1/regime  (public)
  - regime.current()  (programmatic)
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("regime")

CACHE_FILE = "/home/ubuntu/common/regime_state.json"
CACHE_TTL = 60.0
KLINES_URL = ("https://api.binance.com/api/v3/klines"
              "?symbol=BTCUSDT&interval=4h&limit=100")

# Strategy → allowed regimes (operator-tunable).
#
# PANIC_DIP_4H also allowed in RANGING (Apr 27 2026 fix): the strategy
# is self-filtering — it requires a real ATR-sized 3% drop + green bounce
# pattern, which means the *price action* gates it, not the regime.
# Without RANGING here, alert_gate.allow() blocks the signal even when
# strategy_engine.py is willing to fire it. The two were out of sync.
#
# Multi-TF strategies (Apr 27 2026 ship):
#   VWAP_DIP_15M   — mean reversion, RANGING is its sweet spot.
#   EMA_CROSS_1H   — trend continuation, needs uptrend or volatile.
#   BREAKOUT_DAILY — daily breakout, needs uptrend.
#   SMC_CONFLUENCE — multi-TF confluence with internal regime scoring;
#                    the 4-TF alignment already filters by regime so
#                    we allow it everywhere (own filter is sufficient).
STRATEGY_REGIME_MAP = {
    # ─── ACTIVE: SMC_CONFLUENCE long + short only (May 4 2026 cleanup) ─
    "SMC_CONFLUENCE":       ("TRENDING_UP", "TRENDING_DOWN", "VOLATILE", "RANGING"),
    "SMC_CONFLUENCE_SHORT": ("TRENDING_UP", "TRENDING_DOWN", "VOLATILE", "RANGING"),

    # ─── DISABLED: archived to operator's PC (strategy_archive/) ──────
    # Empty tuple = alert_gate.allow() returns False → no signals fire.
    # Files removed from VPS; re-enable by restoring file + non-empty tuple.
    "BREAKOUT_4H":      (),
    "VOL_MOMENTUM_4H":  (),
    "PANIC_DIP_4H":     (),
    "EMA_PULLBACK_4H":  (),
    "VWAP_DIP_15M":     (),
    "EMA_CROSS_1H":     (),
    "BREAKOUT_DAILY":   (),
}


def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _stdev(values: List[float]) -> float:
    if len(values) < 2:
        return 0
    m = sum(values) / len(values)
    var = sum((x - m) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(var) if var > 0 else 0


def _classify(closes: List[float]) -> Tuple[str, dict]:
    """Returns (regime, indicators_dict)."""
    if len(closes) < 56:
        return "UNKNOWN", {"reason": "insufficient_data", "n": len(closes)}

    last = closes[-1]
    ema21 = _ema(closes[-21:], 21)
    ema55 = _ema(closes[-55:], 55)

    # Trend: slope of EMA21 over last 8 bars
    ema21_slope = (ema21 - _ema(closes[-29:-8], 21)) / ema21 * 100

    # Realized volatility: stdev of % returns over last 24 bars
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] * 100
               for i in range(-24, 0)]
    rv = _stdev(returns)

    # ATR-like: avg absolute % return
    avg_abs_ret = sum(abs(r) for r in returns) / len(returns)

    indicators = {
        "last": round(last, 2),
        "ema21": round(ema21, 2),
        "ema55": round(ema55, 2),
        "ema21_slope_pct": round(ema21_slope, 3),
        "realized_vol_pct": round(rv, 3),
        "avg_abs_return_pct": round(avg_abs_ret, 3),
    }

    # Classification rules
    if rv > 2.5 and abs(ema21_slope) < 0.3:
        return "VOLATILE", indicators
    if ema21 > ema55 and ema21_slope > 0.5:
        return "TRENDING_UP", indicators
    if ema21 < ema55 and ema21_slope < -0.5:
        return "TRENDING_DOWN", indicators
    if rv < 1.0 and abs(ema21_slope) < 0.2:
        return "RANGING", indicators
    if ema21 > ema55:
        return "TRENDING_UP", indicators
    if ema21 < ema55:
        return "TRENDING_DOWN", indicators
    return "RANGING", indicators


def _fetch_klines() -> Optional[List[float]]:
    try:
        req = urllib.request.Request(KLINES_URL,
                                     headers={"User-Agent": "srs-regime/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        return [float(k[4]) for k in data]   # close prices
    except Exception as e:
        log.warn("klines_fetch_failed", err=str(e))
        return None


def current(force_refresh: bool = False) -> dict:
    """Return current regime + indicators. Cached 60s."""
    now = time.time()
    if not force_refresh and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if now - cached.get("ts", 0) < CACHE_TTL:
                return cached
        except Exception:
            pass

    closes = _fetch_klines()
    if not closes:
        return {"regime": "UNKNOWN", "ts": now,
                "error": "binance_unreachable"}

    regime, indicators = _classify(closes)
    out = {
        "regime": regime,
        "ts": now,
        "as_of": indicators.get("last"),
        "indicators": indicators,
        "strategy_gates": {
            s: regime in allowed
            for s, allowed in STRATEGY_REGIME_MAP.items()
        },
    }
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, default=str)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass
    log.info("regime_classified", regime=regime, indicators=indicators)
    return out


def is_strategy_allowed(strategy: str) -> Tuple[bool, str]:
    """Bot calls before scanning: should this strategy run in current regime?"""
    cur = current()
    regime = cur.get("regime", "UNKNOWN")
    allowed = STRATEGY_REGIME_MAP.get(strategy)
    if allowed is None:
        return True, f"no_regime_filter:{regime}"
    if regime in allowed or regime == "UNKNOWN":
        return True, f"regime_match:{regime}"
    return False, f"regime_mismatch:{regime} not in {allowed}"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        print(json.dumps(current(force_refresh=True),
                         indent=2, default=str))
    else:
        print(json.dumps(current(), indent=2, default=str))
