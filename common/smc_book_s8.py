"""
smc_book_s8.py — S8 BISI/SIBI + CE + Kill Zone (ICT method).

Verified on Bybit (4H ONLY — 1H + Daily failed):
  • 4H: 48 trades, 52.1% WR, +0.60 net avgR, PF 2.04, $1k→$1,323, 6.3% DD
  • Walk-forward OOS ROBUST (test ratio 0.89)
  • 4/4 quarters profitable
  • Top-5 dependency: 41% (borderline, acceptable)

1H FAILED: top-5 dep = 89% (without those 5 trades it loses)
1D FAILED: only 28 trades, top-5 dep = 147% (negative without them)

Setup (LONG):
  1. Bullish 3-candle FVG forms (BISI: high[i-1] < low[i+1])
  2. Within 50 bars, price retraces to FVG midpoint (CE = Consequent Encroachment)
  3. The retest bar must occur during a Kill Zone (London 06-10 UTC, NY 11-15 UTC)
  4. HTF agreement: close > SMA50
  5. Entry at CE
  6. Stop: FVG low - 0.3 ATR (wide structural stop)
  7. Target: 2.5R

Mirror for SHORT (SIBI: low[i-1] > high[i+1]).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List

import pandas as pd
import numpy as np


@dataclass
class Trade:
    side: str = ""
    entry_idx: int = 0
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    R: Optional[float] = None
    outcome: Optional[str] = None
    strategy: str = "s8_bisi"


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()],
                   axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _simulate(df: pd.DataFrame, entry_idx: int, side: str,
              entry: float, stop: float, target: float, max_bars: int = 80):
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


def _in_kill_zone(ts) -> bool:
    """London Open KZ (06-10 UTC) + NY KZ (11-15 UTC) — institutional flow hours."""
    h = ts.hour
    return (6 <= h < 10) or (11 <= h < 15)


def s8_bisi(df: pd.DataFrame, *,
            fvg_min_atr: float = 0.3,
            max_wait: int = 50,
            target_R: float = 2.5,
            htf_filter: bool = True,
            kill_zone_only: bool = True,
            stop_buffer_atr: float = 0.3,
            require_unfilled_first: bool = True) -> List[Trade]:
    """ICT BISI/SIBI + CE entry during Kill Zones. 4H-recommended only."""
    a = _atr(df, 14)
    sma50 = df["close"].rolling(50).mean()
    trades: List[Trade] = []

    for i in range(2, len(df) - 30):
        atr_now = a.iloc[i]
        if pd.isna(atr_now) or atr_now <= 0:
            continue

        # ── BULLISH FVG (BISI) ──
        if df["high"].iloc[i - 1] < df["low"].iloc[i + 1]:
            fvg_low = df["high"].iloc[i - 1]
            fvg_high = df["low"].iloc[i + 1]
            fvg_width = fvg_high - fvg_low
            if fvg_width < fvg_min_atr * atr_now:
                continue
            ce = (fvg_low + fvg_high) / 2

            for j in range(i + 2, min(i + max_wait + 2, len(df))):
                if require_unfilled_first and df["low"].iloc[j] < fvg_low:
                    break
                if df["low"].iloc[j] <= ce:
                    if kill_zone_only and not _in_kill_zone(df.index[j]):
                        continue
                    if htf_filter and (pd.isna(sma50.iloc[j])
                                       or df["close"].iloc[j] < sma50.iloc[j]):
                        continue
                    entry = ce
                    stop = fvg_low - atr_now * stop_buffer_atr
                    if entry <= stop:
                        break
                    risk = entry - stop
                    target = entry + risk * target_R
                    R, oc = _simulate(df, j, "long", entry, stop, target)
                    trades.append(Trade("long", j, entry, stop, target, R, oc))
                    break

        # ── BEARISH FVG (SIBI) ──
        if df["low"].iloc[i - 1] > df["high"].iloc[i + 1]:
            fvg_high = df["low"].iloc[i - 1]
            fvg_low = df["high"].iloc[i + 1]
            fvg_width = fvg_high - fvg_low
            if fvg_width < fvg_min_atr * atr_now:
                continue
            ce = (fvg_low + fvg_high) / 2

            for j in range(i + 2, min(i + max_wait + 2, len(df))):
                if require_unfilled_first and df["high"].iloc[j] > fvg_high:
                    break
                if df["high"].iloc[j] >= ce:
                    if kill_zone_only and not _in_kill_zone(df.index[j]):
                        continue
                    if htf_filter and (pd.isna(sma50.iloc[j])
                                       or df["close"].iloc[j] > sma50.iloc[j]):
                        continue
                    entry = ce
                    stop = fvg_high + atr_now * stop_buffer_atr
                    if entry >= stop:
                        break
                    risk = stop - entry
                    target = entry - risk * target_R
                    R, oc = _simulate(df, j, "short", entry, stop, target)
                    trades.append(Trade("short", j, entry, stop, target, R, oc))
                    break

    return trades
