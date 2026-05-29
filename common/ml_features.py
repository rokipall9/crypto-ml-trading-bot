"""
ml_features.py — Extract setup features from a trade signal + market context.

Pure functions, no side effects (except read-only file loads for past trade
history). Used by ml_filter.score_signal() to build the feature vector
that drives both Phase 0 (heuristic) and Phase 1 (LightGBM) scoring.

Feature list and rationale documented inline so future-Claude can audit.
"""
from __future__ import annotations

import os
import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Optional, List, Tuple

PAPER_PATH_SMC = "/home/ubuntu/bot/logs/paper_trades.json"
PAPER_PATH_BOOK = "/home/ubuntu/bot/logs/paper_trades_book.json"
LIVE_PATH = "/home/ubuntu/bot/logs/live_trades.json"


def _safe_load_json(path: str) -> Optional[Dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


# ─── Market context fetchers (small, cached, cheap) ──────────────────

_TICKER_CACHE = {"ts": 0.0, "data": None}


def _get_ticker(symbol: str = "BTCUSDT") -> Optional[Dict]:
    """Bybit ticker (cached 5s)."""
    now = time.time()
    if _TICKER_CACHE["data"] and now - _TICKER_CACHE["ts"] < 5:
        return _TICKER_CACHE["data"]
    try:
        url = (f"https://api.bybit.com/v5/market/tickers?"
               f"category=linear&symbol={symbol}")
        with urllib.request.urlopen(url, timeout=4) as r:
            j = json.loads(r.read())
        d = j["result"]["list"][0]
        _TICKER_CACHE["data"] = d
        _TICKER_CACHE["ts"] = now
        return d
    except Exception:
        return None


_FUNDING_CACHE = {"ts": 0.0, "data": None}


def funding_rate(symbol: str = "BTCUSDT") -> Optional[float]:
    """Recent funding rate from Bybit (cached 60s).
    +ve = longs pay shorts; -ve = shorts pay longs. Extremes precede squeezes.
    """
    now = time.time()
    if _FUNDING_CACHE["data"] is not None and now - _FUNDING_CACHE["ts"] < 60:
        return _FUNDING_CACHE["data"]
    try:
        url = (f"https://api.bybit.com/v5/market/funding/history?"
               f"category=linear&symbol={symbol}&limit=1")
        with urllib.request.urlopen(url, timeout=4) as r:
            j = json.loads(r.read())
        rate = float(j["result"]["list"][0]["fundingRate"])
        _FUNDING_CACHE["data"] = rate
        _FUNDING_CACHE["ts"] = now
        return rate
    except Exception:
        return None


_KLINE_CACHE = {"ts": 0.0, "data": None}


def _get_15m_klines(symbol: str = "BTCUSDT",
                    limit: int = 200) -> Optional[List[List]]:
    """200 bars of 15m klines (cached 30s)."""
    now = time.time()
    if _KLINE_CACHE["data"] is not None and now - _KLINE_CACHE["ts"] < 30:
        return _KLINE_CACHE["data"]
    try:
        url = (f"https://api.bybit.com/v5/market/kline?"
               f"category=linear&symbol={symbol}&interval=15&limit={limit}")
        with urllib.request.urlopen(url, timeout=5) as r:
            j = json.loads(r.read())
        bars = j["result"]["list"]
        _KLINE_CACHE["data"] = bars
        _KLINE_CACHE["ts"] = now
        return bars
    except Exception:
        return None


def atr_15m_pair(symbol: str = "BTCUSDT") -> Tuple[Optional[float],
                                                    Optional[float]]:
    """(current_atr_14, median_atr_over_200_bars).

    Used to flag "we're 2× hotter than normal" days where slippage spikes.
    """
    bars = _get_15m_klines(symbol)
    if not bars or len(bars) < 30:
        return None, None
    # Bybit returns newest-first; reverse for chronological
    chrono = list(reversed(bars))
    trs = []
    prev_close = None
    for b in chrono:
        high, low, close = float(b[2]), float(b[3]), float(b[4])
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close),
                     abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    # 14-period ATR over the *latest* 14 bars
    cur_atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else None
    # Median ATR across all 200 (rolling 14-period)
    if len(trs) < 28:
        return cur_atr, None
    rolling = []
    for i in range(14, len(trs)):
        rolling.append(sum(trs[i-14:i]) / 14)
    rolling.sort()
    med = rolling[len(rolling) // 2]
    return cur_atr, med


# ─── Strategy-history features ───────────────────────────────────────

def strategy_recent_outcomes(strategy: str, limit: int = 10) -> Dict:
    """Last N closed outcomes for `strategy` across paper + live.

    Returns:
        {"n": int, "wins": int, "losses": int, "winrate": float,
         "avg_R": float, "last_3_R": [R,R,R]}
    """
    out = {"n": 0, "wins": 0, "losses": 0, "winrate": 0.5,
           "avg_R": 0.0, "last_3_R": []}
    # Pull all candidate closes
    rows: List[Tuple[str, float]] = []  # (closed_at, R)
    for path in (PAPER_PATH_SMC, PAPER_PATH_BOOK):
        d = _safe_load_json(path)
        if not d:
            continue
        for t in d.get("closed_trades", []):
            t_strat = t.get("strategy") or t.get("method") or ""
            if t_strat != strategy:
                continue
            closed_at = t.get("closed_at") or ""
            R = t.get("realized_r")
            if R is None:
                # derive from pnl / risk_amount
                risk = float(t.get("risk_amount") or 0)
                pnl = float(t.get("realized_pnl") or t.get("pnl") or 0)
                R = (pnl / risk) if risk > 0 else 0.0
            rows.append((closed_at, float(R)))
    # Live records also indexed by strategy
    ld = _safe_load_json(LIVE_PATH)
    if ld:
        for o in ld.get("closed_orders", []):
            if o.get("strategy") != strategy:
                continue
            R = o.get("realized_R")
            if R is None:
                continue
            rows.append((o.get("closed_at", ""), float(R)))
    if not rows:
        return out
    rows.sort(key=lambda x: x[0], reverse=True)
    recent = rows[:limit]
    out["n"] = len(recent)
    wins = sum(1 for _, R in recent if R > 0)
    out["wins"] = wins
    out["losses"] = out["n"] - wins
    out["winrate"] = round(wins / out["n"], 3) if out["n"] else 0.5
    out["avg_R"] = round(sum(R for _, R in recent) / out["n"], 3)
    out["last_3_R"] = [round(R, 2) for _, R in recent[:3]]
    return out


def bybit_position_concentration() -> Dict:
    """Current Bybit position state (for the concentration penalty).

    Returns {"has_position": bool, "size_btc": float, "side": str}.
    Imports BybitClient lazily so this module stays importable without creds.
    """
    out = {"has_position": False, "size_btc": 0.0, "side": ""}
    try:
        import sys
        sys.path.insert(0, "/home/ubuntu/common")
        from bybit_client import BybitClient  # type: ignore
        c = BybitClient()
        pos = c.get_position("BTCUSDT")
        if pos and float(pos.get("size", 0)) > 0:
            out["has_position"] = True
            out["size_btc"] = float(pos["size"])
            out["side"] = pos["side"]
    except Exception:
        pass
    return out


# ─── Top-level feature extractor ─────────────────────────────────────

def extract_features(sig: Dict) -> Dict:
    """Build the feature dict consumed by ml_filter.score_signal().

    `sig` must contain at minimum: strategy/method, side, entry, sl, tp/target.
    Returns a dict ready to JSON-log alongside the signal.
    """
    strategy = sig.get("strategy") or sig.get("method") or "unknown"
    side = (sig.get("side") or "").lower()
    entry = float(sig.get("entry") or 0)
    sl = float(sig.get("sl") or sig.get("stop") or 0)
    tp = float(sig.get("tp") or sig.get("target") or sig.get("tp1") or 0)

    sl_dist = abs(entry - sl) if entry and sl else 0
    tp_dist = abs(tp - entry) if entry and tp else 0
    rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0

    cur_atr, med_atr = atr_15m_pair()
    atr_norm = None
    if cur_atr is not None and med_atr is not None and med_atr > 0:
        atr_norm = round(cur_atr / med_atr, 3)

    now = datetime.now(timezone.utc)
    funding = funding_rate()

    track = strategy_recent_outcomes(strategy)
    conc = bybit_position_concentration()

    # Regime — read book runner's regime cache if available
    regime = sig.get("macro") or sig.get("regime") or "UNKNOWN"

    return {
        "strategy": strategy,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "sl_dist_pct": round(sl_dist / entry * 100, 4) if entry else 0.0,
        "tp_dist_pct": round(tp_dist / entry * 100, 4) if entry else 0.0,
        "target_R": round(rr, 3),
        "atr_15m": round(cur_atr, 2) if cur_atr else None,
        "atr_norm": atr_norm,           # >1.5 = "hot"
        "hour_utc": now.hour,
        "dow": now.weekday(),           # 0=Mon, 6=Sun
        "regime": regime,
        "funding_rate": funding,
        "strategy_winrate_10": track["winrate"],
        "strategy_avg_R_10": track["avg_R"],
        "strategy_last_3_R": track["last_3_R"],
        "strategy_n_samples": track["n"],
        "bybit_has_position": conc["has_position"],
        "bybit_position_side": conc["side"],
        "bybit_position_size": conc["size_btc"],
        "extracted_at": now.isoformat(),
    }
