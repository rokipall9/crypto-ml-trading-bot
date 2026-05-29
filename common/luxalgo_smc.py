"""
luxalgo_smc.py — Faithful Python port of LuxAlgo's "Smart Money Concepts"
indicator (CC BY-NC-SA 4.0). Used for zone detection that matches what
the operator sees on TradingView.

Implements:
  • Swing pivot detection      (leg() + getCurrentStructure)
  • Fair Value Gaps            (drawFairValueGaps)
  • Swing Order Blocks         (storeOrdeBlock, drawOrderBlocks)
  • BOS / CHoCH events         (displayStructure)
  • Trailing extremes          (Strong/Weak High/Low)

All functions are pure: take a DataFrame with columns
[open, high, low, close, volume], return lists of zone/event dicts.
No side effects, no plotting, just data.

Default parameters mirror LuxAlgo defaults:
  swingsLengthInput = 50
  equalHighsLowsLengthInput = 3
  internalsLengthInput = 5
  ATR period = 200
  Order block filter = 'Atr'
  Order block mitigation source = 'High/Low'
  FVG threshold = auto (cum_avg(|bar_delta_pct|)*2)
"""
from __future__ import annotations

from typing import List, Dict, Optional, Tuple
import pandas as pd
import numpy as np


# ─── ATR (Wilder, period 200 to match LuxAlgo) ─────────────────────────

def atr_series(df: pd.DataFrame, period: int = 200) -> pd.Series:
    """Wilder ATR; series aligned to df index."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    # Wilder: SMA of first period, then RMA
    return tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()


# ─── Parsed high/low (high-vol bar filter) ────────────────────────────
# LuxAlgo: if (high - low) >= 2 * ATR(200): parsedHigh = low ; parsedLow = high
# Inverts the bar so it gets DESELECTED as an OB candidate.

def parsed_highs_lows(df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    atr = atr_series(df, 200)
    # When ATR is undefined (early bars) treat as not-high-vol
    high_vol = (high - low) >= (2 * atr.fillna(np.inf))
    parsed_high = high.where(~high_vol, low)
    parsed_low = low.where(~high_vol, high)
    return parsed_high, parsed_low


# ─── Swing pivot detection ────────────────────────────────────────────
# LuxAlgo's leg() function. Returns a Series of leg values:
#   1 (bullish) means we're in a bullish leg
#   0 (bearish) means we're in a bearish leg
#
# Pivot HIGH is confirmed at bar (i - size) on the tick where leg
#   flips from bullish (1) to bearish (0).
# Pivot LOW is confirmed at bar (i - size) on the tick where leg
#   flips from bearish (0) to bullish (1).

def detect_pivots(df: pd.DataFrame, size: int = 50) -> List[Dict]:
    """Detect confirmed swing pivot highs and lows.

    Returns list of pivot dicts:
      {"type": "high"/"low", "price": float, "idx": int, "ts": ts}
    sorted by bar idx ascending.

    A pivot at bar idx p is confirmed at bar idx (p + size) — i.e. it
    takes `size` bars to confirm. For real-time use, the most recent
    `size` bars cannot have confirmed pivots yet.
    """
    n = len(df)
    if n <= size:
        return []
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    pivots: List[Dict] = []
    BULLISH_LEG = 1
    BEARISH_LEG = 0
    cur_leg = 0
    prev_leg = 0
    for i in range(size, n):
        # Pine: high[size] > ta.highest(size)
        # high[size] = high `size` bars ago = high[i - size]
        # ta.highest(size) = highest of last `size` bars (includes current)
        #                  = max(high[i-size+1 : i+1])
        prior_high = high[i - size]
        recent_max = high[i - size + 1: i + 1].max()
        prior_low = low[i - size]
        recent_min = low[i - size + 1: i + 1].min()

        new_leg_high = prior_high > recent_max
        new_leg_low = prior_low < recent_min

        prev_leg = cur_leg
        if new_leg_high:
            cur_leg = BEARISH_LEG
        elif new_leg_low:
            cur_leg = BULLISH_LEG

        if cur_leg != prev_leg:
            # Pivot confirmed at i - size
            pivot_idx = i - size
            if cur_leg == BEARISH_LEG:
                # Just flipped to bearish → swing HIGH at pivot_idx
                pivots.append({
                    "type": "high",
                    "price": float(high[pivot_idx]),
                    "idx": int(pivot_idx),
                    "confirmed_at": int(i),
                    "ts": df.index[pivot_idx] if hasattr(df.index, "__getitem__") else None,
                })
            else:
                pivots.append({
                    "type": "low",
                    "price": float(low[pivot_idx]),
                    "idx": int(pivot_idx),
                    "confirmed_at": int(i),
                    "ts": df.index[pivot_idx] if hasattr(df.index, "__getitem__") else None,
                })
    return pivots


# ─── Fair Value Gaps ──────────────────────────────────────────────────
# LuxAlgo: same-timeframe FVG with auto-threshold filter.
#
# bullishFairValueGap: bar[i+2].low > bar[i].high
#   AND bar[i+1].close > bar[i].high
#   AND bar_delta_pct[i+1] > threshold
# bearishFairValueGap: mirror
# threshold = cum_avg(|bar_delta_pct|) * 2  (rolling cumulative)
#
# Mitigation:
#   bullish FVG removed when any later low  < FVG.bottom
#   bearish FVG removed when any later high > FVG.top

def find_fair_value_gaps(df: pd.DataFrame,
                         use_threshold: bool = True
                         ) -> List[Dict]:
    """Find all FVGs in df, return UNMITIGATED ones.

    Returns list of FVG dicts:
      {"type": "bullish"/"bearish",
       "top": float, "bottom": float,
       "idx": int (bar index of the gap-creating middle bar),
       "ts": timestamp}
    """
    n = len(df)
    if n < 4:
        return []
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    open_ = df["open"].astype(float).values
    close = df["close"].astype(float).values

    # bar_delta_pct as in LuxAlgo: (close - open) / (open * 100)
    # NB: original is divided by 100, treating result as already-percent
    bar_delta_pct = (close - open_) / np.where(open_ > 0, open_ * 100, 1)
    # Threshold: cumulative mean of |bar_delta_pct| × 2
    abs_delta = np.abs(bar_delta_pct)
    cum_avg = np.cumsum(abs_delta) / np.arange(1, n + 1)
    threshold = cum_avg * 2 if use_threshold else np.zeros(n)

    fvgs: List[Dict] = []
    # FVG involves bars i, i+1, i+2 — gap is between bar i and bar i+2.
    for i in range(n - 2):
        a_high = high[i]
        a_low = low[i]
        b_close = close[i + 1]
        b_delta = bar_delta_pct[i + 1]
        c_high = high[i + 2]
        c_low = low[i + 2]
        thr = threshold[i + 1]

        # Bullish FVG
        if c_low > a_high and b_close > a_high and b_delta > thr:
            fvgs.append({
                "type": "bullish",
                "bottom": float(a_high),
                "top": float(c_low),
                "idx": int(i + 1),
                "created_at_idx": int(i + 2),
                "ts": df.index[i + 1] if hasattr(df.index, "__getitem__") else None,
            })
        # Bearish FVG
        if c_high < a_low and b_close < a_low and -b_delta > thr:
            fvgs.append({
                "type": "bearish",
                "top": float(a_low),
                "bottom": float(c_high),
                "idx": int(i + 1),
                "created_at_idx": int(i + 2),
                "ts": df.index[i + 1] if hasattr(df.index, "__getitem__") else None,
            })

    # Mitigation pass — drop FVGs that have been filled
    unmitigated: List[Dict] = []
    for fvg in fvgs:
        start_idx = fvg["created_at_idx"] + 1
        if start_idx >= n:
            unmitigated.append(fvg)
            continue
        post_low = low[start_idx:]
        post_high = high[start_idx:]
        if fvg["type"] == "bullish":
            if (post_low < fvg["bottom"]).any():
                continue   # mitigated
        else:
            if (post_high > fvg["top"]).any():
                continue
        unmitigated.append(fvg)
    return unmitigated


# ─── Swing Order Blocks ────────────────────────────────────────────────
# LuxAlgo logic:
#   Walk through pivots. For each pivot:
#     If it's a HIGH and price later closes ABOVE it → BULLISH break
#       → find bar in [pivot_idx, break_idx] with min parsedLow
#       → bullish OB at that bar's [parsedLow, parsedHigh]
#     If it's a LOW and price later closes BELOW it → BEARISH break
#       → find bar with max parsedHigh
#       → bearish OB at [parsedLow, parsedHigh]
#
# Mitigation (HIGHLOW mode, default for swing OBs):
#   bullish OB removed when low  < OB.barLow
#   bearish OB removed when high > OB.barHigh

def find_swing_order_blocks(df: pd.DataFrame,
                            swing_size: int = 50
                            ) -> Tuple[List[Dict], List[Dict]]:
    """Find untested swing order blocks. Returns (untested_obs, all_breaks).

    untested_obs is what we'd plot as the BLUE/RED boxes.
    all_breaks is the BOS/CHoCH events for context.
    """
    n = len(df)
    if n < swing_size + 5:
        return [], []
    parsed_high, parsed_low = parsed_highs_lows(df)
    p_high = parsed_high.values
    p_low = parsed_low.values
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    close = df["close"].astype(float).values

    pivots = detect_pivots(df, swing_size)
    if not pivots:
        return [], []

    obs: List[Dict] = []
    breaks: List[Dict] = []
    trend_bias = 0   # 0 unknown, 1 bullish, -1 bearish

    for piv in pivots:
        # Walk forward from confirmation bar looking for the break.
        # Break occurs at the first bar k where close crosses the level.
        start = piv["confirmed_at"] + 1
        if start >= n:
            continue
        broken_at = None
        if piv["type"] == "high":
            # bullish break: close crosses above pivot.price
            for k in range(start, n):
                if close[k] > piv["price"] and close[k - 1] <= piv["price"]:
                    broken_at = k
                    break
            if broken_at is None:
                continue
            # Find OB candidate: bar in [piv.idx, broken_at] with min p_low
            window = p_low[piv["idx"]: broken_at + 1]
            ob_offset = int(np.argmin(window))
            ob_idx = piv["idx"] + ob_offset
            zone_low = float(p_low[ob_idx])
            zone_high = float(p_high[ob_idx])
            # parsed values can be inverted for high-vol bars; ensure low<high
            if zone_low > zone_high:
                zone_low, zone_high = zone_high, zone_low
            tag = "CHoCH" if trend_bias == -1 else "BOS"
            trend_bias = 1
            obs.append({
                "type": "bullish",
                "bar_low": zone_low,
                "bar_high": zone_high,
                "ob_idx": int(ob_idx),
                "break_idx": int(broken_at),
                "pivot_idx": int(piv["idx"]),
                "pivot_price": float(piv["price"]),
                "ts": df.index[ob_idx] if hasattr(df.index, "__getitem__") else None,
            })
            breaks.append({
                "type": "bullish",
                "tag": tag,
                "level": float(piv["price"]),
                "break_idx": int(broken_at),
                "pivot_idx": int(piv["idx"]),
            })
        else:
            # bearish break: close crosses below pivot.price
            for k in range(start, n):
                if close[k] < piv["price"] and close[k - 1] >= piv["price"]:
                    broken_at = k
                    break
            if broken_at is None:
                continue
            window = p_high[piv["idx"]: broken_at + 1]
            ob_offset = int(np.argmax(window))
            ob_idx = piv["idx"] + ob_offset
            zone_low = float(p_low[ob_idx])
            zone_high = float(p_high[ob_idx])
            if zone_low > zone_high:
                zone_low, zone_high = zone_high, zone_low
            tag = "CHoCH" if trend_bias == 1 else "BOS"
            trend_bias = -1
            obs.append({
                "type": "bearish",
                "bar_low": zone_low,
                "bar_high": zone_high,
                "ob_idx": int(ob_idx),
                "break_idx": int(broken_at),
                "pivot_idx": int(piv["idx"]),
                "pivot_price": float(piv["price"]),
                "ts": df.index[ob_idx] if hasattr(df.index, "__getitem__") else None,
            })
            breaks.append({
                "type": "bearish",
                "tag": tag,
                "level": float(piv["price"]),
                "break_idx": int(broken_at),
                "pivot_idx": int(piv["idx"]),
            })

    # Mitigation: drop OBs that have been violated since formation
    untested: List[Dict] = []
    for ob in obs:
        start_idx = ob["break_idx"] + 1
        if start_idx >= n:
            untested.append(ob)
            continue
        post_low = low[start_idx:]
        post_high = high[start_idx:]
        if ob["type"] == "bullish":
            if (post_low < ob["bar_low"]).any():
                continue  # broken
        else:
            if (post_high > ob["bar_high"]).any():
                continue
        untested.append(ob)
    return untested, breaks


# ─── Trailing extremes (Strong/Weak High/Low) ──────────────────────────
# trailing.top    = highest high since last swing structure event
# trailing.bottom = lowest  low  since last swing structure event
# In bearish trend: top = "Strong High", bottom = "Weak Low"
# In bullish trend: top = "Weak High",   bottom = "Strong Low"

def trailing_extremes(df: pd.DataFrame, swing_size: int = 50) -> Dict:
    """Return current trailing high/low + whether they're Strong or Weak."""
    n = len(df)
    if n == 0:
        return {}
    _, breaks = find_swing_order_blocks(df, swing_size)
    last_bias = 0
    last_event_idx = 0
    if breaks:
        last_break = breaks[-1]
        last_bias = 1 if last_break["type"] == "bullish" else -1
        last_event_idx = last_break["break_idx"]
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    trailing_top = float(high[last_event_idx:].max())
    trailing_bottom = float(low[last_event_idx:].min())
    return {
        "top": trailing_top,
        "bottom": trailing_bottom,
        "bias": last_bias,
        "top_label": "Strong High" if last_bias == -1 else "Weak High",
        "bottom_label": "Strong Low" if last_bias == 1 else "Weak Low",
    }


# ─── Convenience: get all "active" zones for proximity alerter ────────

def all_active_zones(df: pd.DataFrame,
                     swing_size: int = 50,
                     internal_size: int = 5,
                     include_internal: bool = True,
                     fvg_threshold: bool = True,
                     max_swing_obs: int = 5,
                     max_internal_obs: int = 5,
                     ) -> List[Dict]:
    """One-shot scan: return every active (untested/unmitigated) zone.

    swing_size=50 → big "swing OBs" (the wider blue/red boxes on TV)
    internal_size=5 → "internal OBs" (the smaller, closer boxes — these
                       are what's typically near current price and were
                       missing from the original port)

    Each zone dict has:
      kind:   "OB_bullish"|"OB_bearish"|"FVG_bullish"|"FVG_bearish"
              (with "INT_" prefix for internal OBs to differentiate)
      low, high, idx, ts, source
    """
    out: List[Dict] = []

    # Swing OBs (length 50 — the BIG zones).
    # LuxAlgo keeps only the most recent max_swing_obs (default 5).
    swing_obs, _ = find_swing_order_blocks(df, swing_size)
    swing_obs = sorted(swing_obs, key=lambda o: -o["ob_idx"])[:max_swing_obs]
    for ob in swing_obs:
        out.append({
            "kind": "OB_bullish" if ob["type"] == "bullish" else "OB_bearish",
            "low": ob["bar_low"],
            "high": ob["bar_high"],
            "idx": ob["ob_idx"],
            "ts": ob.get("ts"),
            "source": "swing_OB",
        })

    # Internal OBs (length 5 — the SMALLER, closer zones).
    # Mitigation: HIGH/LOW (LuxAlgo default for both internal & swing).
    # Earlier port mistakenly used CLOSE for internals, which kept OBs
    # that LuxAlgo had already mitigated → bot reported zones that
    # didn't exist on the chart. find_swing_order_blocks already does
    # high/low mitigation, so we just dedupe vs swing OBs.
    if include_internal:
        internal_obs, _ = find_swing_order_blocks(df, internal_size)
        swing_idxs = {ob["ob_idx"] for ob in swing_obs}
        # LuxAlgo skips internal pivots that coincide with swing pivots
        internal_obs = [ob for ob in internal_obs
                        if ob["ob_idx"] not in swing_idxs]
        # Keep most recent max_internal_obs (default 5)
        internal_obs = sorted(internal_obs, key=lambda o: -o["ob_idx"])[:max_internal_obs]
        for ob in internal_obs:
            out.append({
                "kind": "INT_OB_bullish" if ob["type"] == "bullish" else "INT_OB_bearish",
                "low": ob["bar_low"],
                "high": ob["bar_high"],
                "idx": ob["ob_idx"],
                "ts": ob.get("ts"),
                "source": "internal_OB",
            })

    fvgs = find_fair_value_gaps(df, use_threshold=fvg_threshold)
    for fvg in fvgs:
        out.append({
            "kind": "FVG_bullish" if fvg["type"] == "bullish" else "FVG_bearish",
            "low": fvg["bottom"],
            "high": fvg["top"],
            "idx": fvg["idx"],
            "ts": fvg.get("ts"),
            "source": "FVG",
        })

    # Final dedupe — same kind + same bounds (rounded to $1) =
    # same zone. find_swing_order_blocks can occasionally emit
    # the same OB twice via different pivots resolving to the
    # same bar.
    seen = set()
    deduped: List[Dict] = []
    for z in out:
        key = (z["kind"], round(z["low"], 0), round(z["high"], 0))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(z)
    return deduped


# ─── CLI for visual sanity-check vs TradingView ───────────────────────

if __name__ == "__main__":
    import sys, json
    sys.path.insert(0, "/home/ubuntu/bot")
    sys.path.insert(0, "/home/ubuntu/common")
    import market_data
    tf = sys.argv[1] if len(sys.argv) > 1 else "1h"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 500
    print(f"Loading {n} {tf} candles...")
    df = market_data.download("BTCUSDT", tf, total_candles=n)
    df = df.reset_index(drop=True)
    zones = all_active_zones(df)
    cur = float(df["close"].iloc[-1])
    print(f"\nCurrent BTC: ${cur:,.0f}    {len(zones)} active zones on {tf}\n")
    print("Kind            Range                      Distance  Direction")
    print("-" * 76)
    for z in sorted(zones, key=lambda x: -((x["high"] + x["low"]) / 2)):
        mid = (z["high"] + z["low"]) / 2
        dist_pct = (mid - cur) / cur * 100
        direction = "above" if mid > cur else "below"
        print(f"{z['kind']:14}  ${z['low']:>9,.0f} - ${z['high']:<9,.0f}   "
              f"{abs(dist_pct):5.2f}%   {direction}")
