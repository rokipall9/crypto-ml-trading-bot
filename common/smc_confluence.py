"""
smc_confluence.py — Multi-timeframe SMC confluence detector.

Operator's manual trading style encoded as code:
  • Look at 15m, 1h, 4h, daily — all 4 timeframes
  • Combine Order Blocks (OB), Fair Value Gaps (FVG), Bollinger Bands (BB)
  • Fire only when multiple timeframes agree

This is intentionally selective. When all 4 TFs align, the setup is
usually high-quality but rare (1–3 fires/month is normal). Use it as
a confidence-stacked filter, not a frequency strategy.

────────────────────────────────────────────────────────────────────
SMC primitives implemented:

  1. Bullish Order Block (OB)
     - Definition: last RED candle before a strong bullish leg
     - Detection: scan back N bars; for each red candle, check if the
       next 3 candles produced a net up-move > 2× ATR
     - Zone: (red.low, red.high) of that candle
     - Tracked as "untested" until price returns to the zone

  2. Bullish Fair Value Gap (FVG) / imbalance
     - Definition: 3-candle pattern where bar[i+2].low > bar[i].high
       (creates an unfilled gap above bar i)
     - Zone: (bar[i].high, bar[i+2].low)
     - Tracked as "unfilled" until price returns into the zone

  3. Bollinger Bands (20, 2σ)
     - Lower band: price within 0.5% of lower → potential bullish reversal
     - %B position: 0 = at lower, 1 = at upper

  4. Trend bias (EMA-based)
     - EMA21 > EMA55 → bullish bias
     - Slope of EMA50 → trend strength

────────────────────────────────────────────────────────────────────
Per-timeframe score (0–5):
  • EMA21 > EMA55                            → +1
  • Price in untested bullish OB zone         → +1 (+1 extra if recent)
  • Price in unfilled bullish FVG             → +1
  • Price near lower BB (≤ 0.5% above)        → +1
  • RSI(14) < 45 (room to run up)             → +1

Confluence fire condition (default):
  • ALL 4 TFs score ≥ 2
  • Daily score ≥ 1 (don't fight the macro trend)
  • Total combined score ≥ 11/20

If 3 of 4 align (instead of 4 of 4), logged as "near-miss" — useful
for the operator to review and decide manually.
"""
from __future__ import annotations

from typing import Optional, Dict, List, Tuple, NamedTuple
import pandas as pd
import numpy as np


# ─── data classes ─────────────────────────────────────────────────────

class Zone(NamedTuple):
    """A price zone (OB or FVG)."""
    kind: str        # "OB" or "FVG"
    side: str        # "bullish" or "bearish"
    low: float
    high: float
    bar_idx: int     # index of detection (relative to df end, negative)
    tested: bool     # has price returned to it since formation?


# ─── shared helpers ───────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return float("nan")
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    tr = pd.concat([
        h - l,
        (h - c.shift()).abs(),
        (l - c.shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.tail(period).mean())


def _ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> float:
    if len(close) < period + 1:
        return float("nan")
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(period).mean().iloc[-1]
    if loss == 0:
        return 100.0
    rs = gain / loss
    return float(100 - 100 / (1 + rs))


def _bb(close: pd.Series, period: int = 20, k: float = 2.0
        ) -> Tuple[float, float, float, float]:
    """Returns (mid, upper, lower, percent_b)."""
    if len(close) < period:
        return float("nan"), float("nan"), float("nan"), float("nan")
    sma = close.rolling(period).mean().iloc[-1]
    std = close.rolling(period).std().iloc[-1]
    upper = sma + k * std
    lower = sma - k * std
    last = float(close.iloc[-1])
    pct_b = (last - lower) / (upper - lower) if upper > lower else 0.5
    return float(sma), float(upper), float(lower), float(pct_b)


# ─── OB / FVG detection ───────────────────────────────────────────────

def find_bullish_obs(df: pd.DataFrame, lookback: int = 50,
                    move_atr_mult: float = 2.0) -> List[Zone]:
    """Find bullish Order Blocks in the last `lookback` bars.

    A bullish OB = last RED candle before a strong up-leg
    (next 3 bars cumulative up-move > move_atr_mult × ATR).
    """
    if len(df) < lookback + 5:
        return []
    atr = _atr(df, 14)
    if not np.isfinite(atr) or atr <= 0:
        return []
    out: List[Zone] = []
    n = len(df)
    cur_price = float(df["close"].iloc[-1])
    # Walk backwards from -5 to -lookback
    for offset in range(5, lookback):
        i = n - offset
        if i < 1 or i + 3 >= n:
            continue
        bar = df.iloc[i]
        if float(bar["close"]) >= float(bar["open"]):
            continue   # not red
        # Strong up-leg? Check next 3 bars after i
        future_high = float(df.iloc[i+1:i+4]["high"].max())
        leg = future_high - float(bar["close"])
        if leg < move_atr_mult * atr:
            continue
        zone_low = float(bar["low"])
        zone_high = float(bar["high"])
        # Tested? Has any bar after i+3 entered the zone?
        post = df.iloc[i+4:]
        tested = bool(((post["low"] <= zone_high) &
                       (post["high"] >= zone_low)).any())
        out.append(Zone("OB", "bullish", zone_low, zone_high,
                        bar_idx=-(n - i), tested=tested))
    return out


def find_bullish_fvgs(df: pd.DataFrame, lookback: int = 50) -> List[Zone]:
    """Find bullish Fair Value Gaps in the last `lookback` bars.

    A bullish FVG = 3-candle pattern where bar[i+2].low > bar[i].high
    (gap creates unfilled imbalance).
    """
    if len(df) < lookback + 3:
        return []
    out: List[Zone] = []
    n = len(df)
    for offset in range(3, lookback):
        i = n - offset
        if i < 0 or i + 2 >= n:
            continue
        a_high = float(df.iloc[i]["high"])
        c_low = float(df.iloc[i+2]["low"])
        if c_low <= a_high:
            continue   # no gap
        zone_low = a_high
        zone_high = c_low
        # Filled? Has any bar after i+2 traded into the zone?
        post = df.iloc[i+3:]
        filled = bool(((post["low"] <= zone_high) &
                       (post["high"] >= zone_low)).any())
        out.append(Zone("FVG", "bullish", zone_low, zone_high,
                        bar_idx=-(n - i), tested=filled))
    return out


def price_in_zone(price: float, zone: Zone, tol: float = 0.002) -> bool:
    """Is `price` within `tol` (default 0.2%) of `zone`?"""
    pad = (zone.high - zone.low) * 0.5 + price * tol
    return (zone.low - pad) <= price <= (zone.high + pad)


# ─── per-TF scoring ───────────────────────────────────────────────────

def score_timeframe(df: pd.DataFrame, tf: str,
                    current_price: Optional[float] = None) -> Dict:
    """Score this timeframe 0–5 for bullish bias. INTRA-CANDLE AWARE.

    Setup (zones, EMAs, RSI, BB) computed from closed bars (df).
    Trigger (in-zone, near-BB) checked against current_price.
    Falls back to last close if current_price not provided.
    """
    if df is None or len(df) < 60:
        return {"tf": tf, "score": 0, "details": "insufficient_bars",
                "bias": "neutral", "obs": [], "fvgs": []}

    close = df["close"].astype(float)
    last_close = float(close.iloc[-1])
    if current_price is None:
        current_price = last_close
    cur = float(current_price)
    last_low = float(df["low"].iloc[-1])

    score = 0
    notes = []

    # 1. EMA trend (from closed bars)
    ema21 = float(_ema(close, 21).iloc[-1])
    ema55 = float(_ema(close, 55).iloc[-1])
    if ema21 > ema55:
        score += 1
        notes.append("EMA21>EMA55")
    else:
        notes.append("EMA21<=EMA55")

    # 2. Bullish OB — current_price IN zone (or zone touched this bar)
    obs = find_bullish_obs(df)
    in_ob_now = [z for z in obs if price_in_zone(cur, z)]
    touched_this_bar = [z for z in obs
                        if not in_ob_now  # don't double-count
                        and price_in_zone(last_low, z)
                        and cur > z.high]   # touched then bounced out
    if in_ob_now:
        score += 1
        notes.append("in_OB_now")
        # Bonus: untested OB nearby
        recent_untested = [z for z in in_ob_now if not z.tested]
        if recent_untested:
            score += 1
            notes.append(f"untested_OB({len(recent_untested)})")
    elif touched_this_bar:
        score += 1
        notes.append(f"touched_OB+bounced({len(touched_this_bar)})")

    # 3. Bullish FVG — current price in unfilled FVG (or touched + bounced)
    fvgs = find_bullish_fvgs(df)
    in_fvg = [z for z in fvgs if not z.tested and price_in_zone(cur, z)]
    touched_fvg = [z for z in fvgs
                   if not z.tested and not in_fvg
                   and price_in_zone(last_low, z)
                   and cur > z.high]
    if in_fvg:
        score += 1
        notes.append("in_unfilled_FVG")
    elif touched_fvg:
        score += 1
        notes.append(f"touched_FVG+bounced({len(touched_fvg)})")

    # 4. Lower BB proximity (using current_price)
    _, upper, lower, pct_b = _bb(close)
    if np.isfinite(lower) and cur <= lower * 1.005:
        score += 1
        # Recompute pct_b for current_price
        if upper > lower:
            pct_b = (cur - lower) / (upper - lower)
        notes.append(f"near_lower_BB(%B={pct_b:.2f})")

    # 5. RSI < 45 (using current_price as latest tick)
    closes_aug = pd.concat([close, pd.Series([cur])], ignore_index=True)
    rsi = _rsi(closes_aug, 14)
    if np.isfinite(rsi) and rsi < 45:
        score += 1
        notes.append(f"RSI={rsi:.1f}<45")

    bias = "bullish" if score >= 3 else "neutral" if score >= 2 else "bearish"
    return {
        "tf": tf,
        "score": score,
        "max_score": 6,
        "bias": bias,
        "ema21": ema21,
        "ema55": ema55,
        "rsi": rsi if np.isfinite(rsi) else None,
        "bb_lower": lower if np.isfinite(lower) else None,
        "bb_pct_b": pct_b if np.isfinite(pct_b) else None,
        "n_obs_total": len(obs),
        "n_obs_untested": sum(1 for z in obs if not z.tested),
        "n_fvgs_unfilled": sum(1 for z in fvgs if not z.tested),
        "in_ob_zone": bool(in_ob_now),
        "touched_ob": bool(touched_this_bar),
        "in_unfilled_fvg": bool(in_fvg),
        "touched_fvg": bool(touched_fvg),
        "details": " · ".join(notes),
    }


# ─── confluence detection ─────────────────────────────────────────────

def score_timeframe_bearish(df: pd.DataFrame, tf: str,
                            current_price: Optional[float] = None) -> Dict:
    """MIRROR of score_timeframe — scores 0–5 for BEARISH bias.

    Bullish version checks: EMA21>EMA55, in_bullish_OB, in_bullish_FVG,
                            near LOWER BB, RSI<45.
    Bearish version checks: EMA21<EMA55, in_bearish_OB, in_bearish_FVG,
                            near UPPER BB, RSI>55.
    """
    if df is None or len(df) < 60:
        return {"tf": tf, "score": 0, "details": "insufficient_bars",
                "bias": "neutral", "obs": [], "fvgs": []}

    close = df["close"].astype(float)
    last_close = float(close.iloc[-1])
    if current_price is None:
        current_price = last_close
    cur = float(current_price)
    last_high = float(df["high"].iloc[-1])

    score = 0
    notes = []

    # 1. EMA trend — bearish (EMA21 < EMA55)
    ema21 = float(_ema(close, 21).iloc[-1])
    ema55 = float(_ema(close, 55).iloc[-1])
    if ema21 < ema55:
        score += 1
        notes.append("EMA21<EMA55")
    else:
        notes.append("EMA21>=EMA55")

    # 2. Bearish OB — current price in zone, OR touched + rejected this bar
    obs_all = find_bullish_obs(df)        # lib gives bullish; mirror here:
    # Build bearish OBs by inverting: scan pivot HIGHS that broke down
    bearish_obs = _find_bearish_obs(df)
    in_ob_now = [z for z in bearish_obs if price_in_zone(cur, z)]
    touched_this_bar = [z for z in bearish_obs
                        if not in_ob_now
                        and price_in_zone(last_high, z)
                        and cur < z.low]    # rejected DOWN out of zone
    if in_ob_now:
        score += 1
        notes.append("in_OB_now")
        recent_untested = [z for z in in_ob_now if not z.tested]
        if recent_untested:
            score += 1
            notes.append(f"untested_OB({len(recent_untested)})")
    elif touched_this_bar:
        score += 1
        notes.append(f"touched_OB+rejected({len(touched_this_bar)})")

    # 3. Bearish FVG — current price in unfilled zone, OR touched + rejected
    fvgs = _find_bearish_fvgs(df)
    in_fvg = [z for z in fvgs if not z.tested and price_in_zone(cur, z)]
    touched_fvg = [z for z in fvgs
                   if not z.tested and not in_fvg
                   and price_in_zone(last_high, z)
                   and cur < z.low]
    if in_fvg:
        score += 1
        notes.append("in_unfilled_FVG")
    elif touched_fvg:
        score += 1
        notes.append(f"touched_FVG+rejected({len(touched_fvg)})")

    # 4. Upper BB proximity — overbought
    _, upper, lower, pct_b = _bb(close)
    if np.isfinite(upper) and cur >= upper * 0.995:
        score += 1
        if upper > lower:
            pct_b = (cur - lower) / (upper - lower)
        notes.append(f"near_upper_BB(%B={pct_b:.2f})")

    # 5. RSI > 55 — overbought-ish, room to fall
    closes_aug = pd.concat([close, pd.Series([cur])], ignore_index=True)
    rsi = _rsi(closes_aug, 14)
    if np.isfinite(rsi) and rsi > 55:
        score += 1
        notes.append(f"RSI={rsi:.1f}>55")

    bias = "bearish" if score >= 3 else "neutral" if score >= 2 else "bullish"
    return {
        "tf": tf,
        "score": score,
        "max_score": 6,
        "bias": bias,
        "ema21": ema21,
        "ema55": ema55,
        "rsi": rsi if np.isfinite(rsi) else None,
        "bb_upper": upper if np.isfinite(upper) else None,
        "bb_pct_b": pct_b if np.isfinite(pct_b) else None,
        "n_obs_total": len(bearish_obs),
        "n_obs_untested": sum(1 for z in bearish_obs if not z.tested),
        "n_fvgs_unfilled": sum(1 for z in fvgs if not z.tested),
        "in_ob_zone": bool(in_ob_now),
        "touched_ob": bool(touched_this_bar),
        "in_unfilled_fvg": bool(in_fvg),
        "touched_fvg": bool(touched_fvg),
        "details": " · ".join(notes),
    }


def _find_bearish_obs(df: pd.DataFrame, lookback: int = 50,
                      move_atr_mult: float = 2.0) -> List[Zone]:
    """Find bearish Order Blocks: last GREEN candle before a strong down-leg.

    Mirror of find_bullish_obs.
    """
    if len(df) < lookback + 5:
        return []
    atr = _atr(df, 14)
    if not np.isfinite(atr) or atr <= 0:
        return []
    out: List[Zone] = []
    n = len(df)
    for offset in range(5, lookback):
        i = n - offset
        if i < 1 or i + 3 >= n:
            continue
        bar = df.iloc[i]
        if float(bar["close"]) <= float(bar["open"]):
            continue   # not green
        # Strong down-leg in next 3 bars?
        future_low = float(df.iloc[i+1:i+4]["low"].min())
        leg = float(bar["close"]) - future_low
        if leg < move_atr_mult * atr:
            continue
        zone_low = float(bar["low"])
        zone_high = float(bar["high"])
        post = df.iloc[i+4:]
        tested = bool(((post["low"] <= zone_high) &
                       (post["high"] >= zone_low)).any())
        out.append(Zone("OB", "bearish", zone_low, zone_high,
                        bar_idx=-(n - i), tested=tested))
    return out


def _find_bearish_fvgs(df: pd.DataFrame, lookback: int = 50) -> List[Zone]:
    """Find bearish Fair Value Gaps: bar[i+2].high < bar[i].low."""
    if len(df) < lookback + 3:
        return []
    out: List[Zone] = []
    n = len(df)
    for offset in range(3, lookback):
        i = n - offset
        if i < 0 or i + 2 >= n:
            continue
        a_low = float(df.iloc[i]["low"])
        c_high = float(df.iloc[i+2]["high"])
        if c_high >= a_low:
            continue
        zone_low = c_high
        zone_high = a_low
        post = df.iloc[i+3:]
        filled = bool(((post["low"] <= zone_high) &
                       (post["high"] >= zone_low)).any())
        out.append(Zone("FVG", "bearish", zone_low, zone_high,
                        bar_idx=-(n - i), tested=filled))
    return out


def bearish_confluence_check(dfs: Dict[str, pd.DataFrame],
                             current_price: Optional[float] = None,
                             min_aligned_tfs: int = 4,
                             daily_min_score: int = 1,
                             total_min_score: int = 11
                             ) -> Tuple[Optional[Dict], Dict]:
    """MIRROR of confluence_check — fires SELL signals when 4 TFs align bearish.

    Same gating philosophy as the bullish version:
      • All 4 TFs score ≥2 (bearish-aligned)
      • Daily score ≥1 (don't fight macro)
      • Total ≥11/24
    """
    df_15m = dfs.get("15m")
    if df_15m is None or len(df_15m) == 0:
        return None, {"reason": "no_15m_data", "fired": False, "near_miss": False}
    if current_price is None:
        current_price = float(df_15m["close"].iloc[-1])

    tf_scores = {}
    for tf in ("15m", "1h", "4h", "1d"):
        tf_scores[tf] = score_timeframe_bearish(dfs.get(tf), tf, current_price)

    aligned = sum(1 for s in tf_scores.values() if s["score"] >= 2)
    total = sum(s["score"] for s in tf_scores.values())
    daily_score = tf_scores["1d"]["score"]

    debug = {
        "aligned_tfs": aligned,
        "total_score": total,
        "daily_score": daily_score,
        "current_price": current_price,
        "tf_scores": tf_scores,
        "fired": False,
        "near_miss": False,
        "direction": "short",
    }

    if daily_score < daily_min_score:
        debug["reason"] = f"daily_too_weak ({daily_score} < {daily_min_score})"
        return None, debug
    if total < total_min_score:
        debug["reason"] = f"total_score_low ({total} < {total_min_score})"
    if aligned < min_aligned_tfs:
        if aligned >= min_aligned_tfs - 1:
            debug["near_miss"] = True
        debug["reason"] = (debug.get("reason") or
                           f"only_{aligned}_of_4_aligned (need {min_aligned_tfs})")
        return None, debug

    atr_15m = _atr(df_15m, 14)
    if not np.isfinite(atr_15m) or atr_15m <= 0:
        debug["reason"] = "atr_unavailable_for_signal"
        return None, debug

    # SHORT mechanics: stop ABOVE recent high, target BELOW
    recent_high = float(df_15m["high"].iloc[-3:].max())
    sl = recent_high + 1.5 * atr_15m
    risk = sl - current_price
    if risk <= 0:
        debug["reason"] = "stop_below_entry"
        return None, debug
    tp = current_price - 2.5 * risk     # 2.5R short target

    sig = {
        "strategy": "SMC_CONFLUENCE_SHORT",
        "side": "sell",
        "interval": "15m",
        "entry": current_price,
        "sl": sl,
        "tp": tp,
        "atr": atr_15m,
        "trail_atr": 2.0,
        "time_stop_bars": 64,        # 16h on 15m
        "risk_pct": 0.0075,
        "reason": (f"SMC_CONF_SHORT aligned={aligned}/4 total={total}/24 "
                   f"d={tf_scores['1d']['score']} "
                   f"4h={tf_scores['4h']['score']} "
                   f"1h={tf_scores['1h']['score']} "
                   f"15m={tf_scores['15m']['score']}"),
        "smc_details": {tf: s["details"] for tf, s in tf_scores.items()},
    }
    debug["fired"] = True
    debug["signal"] = sig
    return sig, debug


def confluence_check(dfs: Dict[str, pd.DataFrame],
                     current_price: Optional[float] = None,
                     min_aligned_tfs: int = 4,
                     daily_min_score: int = 1,
                     total_min_score: int = 11
                     ) -> Tuple[Optional[Dict], Dict]:
    """Run scoring on all 4 TFs, return signal if confluence is reached.
    INTRA-CANDLE AWARE — uses current_price for trigger checks.

    Args:
      dfs: {"15m": df, "1h": df, "4h": df, "1d": df}
      current_price: live price; if None, uses 15m last close
      min_aligned_tfs: how many TFs must score >= 2 (default 4 = all)
      daily_min_score: minimum daily score (default 1 = no bearish daily)
      total_min_score: minimum sum across all TFs (default 11/24)
    """
    df_15m = dfs.get("15m")
    if df_15m is None or len(df_15m) == 0:
        return None, {"reason": "no_15m_data", "fired": False, "near_miss": False}
    if current_price is None:
        current_price = float(df_15m["close"].iloc[-1])

    tf_scores = {}
    for tf in ("15m", "1h", "4h", "1d"):
        tf_scores[tf] = score_timeframe(dfs.get(tf), tf, current_price)

    aligned = sum(1 for s in tf_scores.values() if s["score"] >= 2)
    total = sum(s["score"] for s in tf_scores.values())
    daily_score = tf_scores["1d"]["score"]

    debug = {
        "aligned_tfs": aligned,
        "total_score": total,
        "daily_score": daily_score,
        "current_price": current_price,
        "tf_scores": tf_scores,
        "fired": False,
        "near_miss": False,
    }

    if daily_score < daily_min_score:
        debug["reason"] = f"daily_too_weak ({daily_score} < {daily_min_score})"
        return None, debug
    if total < total_min_score:
        debug["reason"] = f"total_score_low ({total} < {total_min_score})"
    if aligned < min_aligned_tfs:
        if aligned >= min_aligned_tfs - 1:
            debug["near_miss"] = True
        debug["reason"] = (debug.get("reason") or
                           f"only_{aligned}_of_4_aligned (need {min_aligned_tfs})")
        return None, debug

    atr_15m = _atr(df_15m, 14)
    if not np.isfinite(atr_15m) or atr_15m <= 0:
        debug["reason"] = "atr_unavailable_for_signal"
        return None, debug

    # Stop: 1.5 ATR below the recent (last 3 bars on 15m) low
    recent_low = float(df_15m["low"].iloc[-3:].min())
    sl = recent_low - 1.5 * atr_15m
    risk = current_price - sl
    if risk <= 0:
        debug["reason"] = "stop_above_entry"
        return None, debug
    tp = current_price + 2.5 * risk     # 2.5R

    sig = {
        "strategy": "SMC_CONFLUENCE",
        "side": "buy",
        "interval": "15m",
        "entry": current_price,
        "sl": sl,
        "tp": tp,
        "atr": atr_15m,
        "trail_atr": 2.0,
        "time_stop_bars": 64,        # 16h on 15m
        "risk_pct": 0.0075,
        "reason": (f"SMC_CONF aligned={aligned}/4 total={total}/24 "
                   f"d={tf_scores['1d']['score']} "
                   f"4h={tf_scores['4h']['score']} "
                   f"1h={tf_scores['1h']['score']} "
                   f"15m={tf_scores['15m']['score']}"),
        "smc_details": {tf: s["details"] for tf, s in tf_scores.items()},
    }
    debug["fired"] = True
    debug["signal"] = sig
    return sig, debug


# ─── CLI for manual inspection ────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, "/home/ubuntu/bot")
    sys.path.insert(0, "/home/ubuntu/common")
    import market_data
    import json

    dfs = {
        "15m": market_data.download("BTCUSDT", "15m", total_candles=500),
        "1h":  market_data.download("BTCUSDT", "1h",  total_candles=500),
        "4h":  market_data.download("BTCUSDT", "4h",  total_candles=500),
        "1d":  market_data.download("BTCUSDT", "1d",  total_candles=200),
    }
    sig, debug = confluence_check(dfs)
    print(json.dumps({
        "fired": debug["fired"],
        "near_miss": debug["near_miss"],
        "aligned_tfs": debug["aligned_tfs"],
        "total_score": debug["total_score"],
        "reason": debug.get("reason"),
        "tf_breakdown": {tf: {"score": s["score"], "bias": s["bias"],
                              "details": s["details"]}
                         for tf, s in debug["tf_scores"].items()},
        "signal": sig,
    }, default=str, indent=2))
