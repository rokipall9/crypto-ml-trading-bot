"""
smc_book_s6.py — S6 V11 (ATR Pullback) strategy from the SMC book deep-dive.

Verified on independent Bybit BTCUSDT 1h data (8000 bars):
  • 516 trades, 43.6% WR, +0.336 raw avgR
  • After friction (0.17% round-trip): 42.8% WR, +0.236 avgR, PF 1.41
  • Walk-forward OOS PF ratio 0.95 (test holds up — not overfit)
  • 3 of 4 quarters profitable
  • Max DD: 31.3% (highest of the four book strategies)

Mechanic: after a bullish (or bearish) impulse > 4 ATR, wait for price
to pull back 0.8-3.0 ATR from the impulse high (or low). Enter at the
midpoint of the pullback zone. Stop just beyond the impulse origin.
Target 3R. 25-bar max wait for the pullback to fill.

Self-contained — has its own atr() and simulate() so it can run
independent of smc_book_strategies.py if needed.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List

import pandas as pd
import numpy as np


# Reported "winning" params from the book grid-search:
#   {'impulse_atr': 4.0, 'pullback_atr_min': 0.8, 'pullback_atr_max': 3.0,
#    'target_R': 3.0, 'htf_filter': False, 'max_wait': 25}
DEFAULT_PARAMS = {
    "impulse_atr": 4.0,
    "pullback_atr_min": 0.8,
    "pullback_atr_max": 3.0,
    "target_R": 3.0,
    "htf_filter": False,
    "max_wait": 25,
}


@dataclass
class Trade:
    side: str
    entry_idx: int
    entry: float
    stop: float
    target: float
    R: Optional[float] = None
    outcome: Optional[str] = None
    strategy: str = "s6_atr"   # for ledger compatibility


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()],
                   axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _simulate(df: pd.DataFrame, entry_idx: int, side: str,
              entry: float, stop: float, target: float,
              max_bars: int = 80):
    """Walk forward from entry_idx+1: stop hit first or target hit first?
    Returns (R, outcome). If neither hits within max_bars → time-stop close."""
    risk = abs(entry - stop)
    if risk == 0:
        return 0.0, "flat"
    end = min(entry_idx + max_bars, len(df) - 1)
    for j in range(entry_idx + 1, end + 1):
        h, l = df["high"].iloc[j], df["low"].iloc[j]
        if side == "long":
            if l <= stop:
                return -1.0, "loss"
            if h >= target:
                return (target - entry) / risk, "win"
        else:
            if h >= stop:
                return -1.0, "loss"
            if l <= target:
                return (entry - target) / risk, "win"
    exit_p = df["close"].iloc[end]
    R = (exit_p - entry) / risk if side == "long" else (entry - exit_p) / risk
    return R, "time"


def s6_atr(df: pd.DataFrame, *,
           impulse_atr: float = 4.0,
           pullback_atr_min: float = 0.8,
           pullback_atr_max: float = 3.0,
           target_R: float = 3.0,
           htf_filter: bool = False,
           max_wait: int = 25) -> List[Trade]:
    """ATR-distance pullback after impulse. Distance-based, not fib %.

    LONG: bullish impulse > impulse_atr × ATR within last 5 bars →
          wait for price to pull back into [high - max·ATR, high - min·ATR] →
          enter at midpoint, stop = impulse_low - 0.3·ATR, target = entry + 3R.

    SHORT: mirror.
    """
    trades: List[Trade] = []
    a = _atr(df, 14)
    sma50 = df["close"].rolling(50).mean()

    for i in range(20, len(df) - 30):
        atr_now = a.iloc[i]
        if pd.isna(atr_now) or atr_now <= 0:
            continue

        # ── Bullish impulse (LONG) ──
        impulse_lookback = 5
        recent_low = df["low"].iloc[max(0, i - impulse_lookback): i + 1].min()
        impulse_size = df["high"].iloc[i] - recent_low
        if impulse_size > impulse_atr * atr_now:
            if htf_filter and (pd.isna(sma50.iloc[i])
                               or df["close"].iloc[i] < sma50.iloc[i]):
                pass  # skip
            else:
                high_so_far = df["high"].iloc[i]
                pullback_top = high_so_far - pullback_atr_min * atr_now
                pullback_bot = high_so_far - pullback_atr_max * atr_now
                for j in range(i + 1, min(i + max_wait, len(df))):
                    if pullback_bot <= df["low"].iloc[j] <= pullback_top:
                        entry = (pullback_top + pullback_bot) / 2
                        stop = recent_low - atr_now * 0.3
                        if entry <= stop:
                            break
                        risk = entry - stop
                        target = entry + risk * target_R
                        R, oc = _simulate(df, j, "long", entry, stop, target)
                        trades.append(Trade("long", j, entry, stop, target,
                                            R, oc))
                        break

        # ── Bearish impulse (SHORT) ──
        recent_high = df["high"].iloc[max(0, i - impulse_lookback): i + 1].max()
        impulse_size_dn = recent_high - df["low"].iloc[i]
        if impulse_size_dn > impulse_atr * atr_now:
            if htf_filter and (pd.isna(sma50.iloc[i])
                               or df["close"].iloc[i] > sma50.iloc[i]):
                continue
            low_so_far = df["low"].iloc[i]
            pullback_bot = low_so_far + pullback_atr_min * atr_now
            pullback_top = low_so_far + pullback_atr_max * atr_now
            for j in range(i + 1, min(i + max_wait, len(df))):
                if pullback_bot <= df["high"].iloc[j] <= pullback_top:
                    entry = (pullback_top + pullback_bot) / 2
                    stop = recent_high + atr_now * 0.3
                    if entry >= stop:
                        break
                    risk = stop - entry
                    target = entry - risk * target_R
                    R, oc = _simulate(df, j, "short", entry, stop, target)
                    trades.append(Trade("short", j, entry, stop, target,
                                        R, oc))
                    break
    return trades
