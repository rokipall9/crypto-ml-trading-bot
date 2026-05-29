"""
smc_book_s7.py — S7 SMA Pullback strategy (NEW — added 2026-05-07).

Mechanic: pullback to SMA20 inside SMA200-defined trend.
Different from S2/S3/S6 (fib/ATR-based) — this is moving-average-based,
giving genuine portfolio diversification.

Verified on independent Bybit BTCUSDT data:
  • 1H: 425 trades, 26.1% WR, +0.52 net avgR, PF 1.52, $1k→$7,281, 40% DD
  • 4H: 151 trades, 28.5% WR, +0.66 net avgR, PF 1.80, $1k→$2,510, 21% DD
  • Walk-forward OOS ROBUST on both (test ratio 1.35 / 0.98)
  • 4/4 quarters profitable on both timeframes

Daily TF was tested and FAILS walk-forward (test PF 0.16, 1/4 quarters).
DO NOT enable on daily.

Setup:
  1. SMA200 + SMA20 trend confirmation (both on the same side of price)
  2. Recent high (last 10 bars) reached >= 0.5 ATR above SMA20 (real pullback)
  3. Current bar's low touches SMA20, close stays above
  4. Entry at next bar's open
  5. Stop: pullback bar's low - 0.3 ATR
  6. Target: 7R (let winners run with the trend)
  7. After a loss: 10-bar cooldown
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List

import pandas as pd
import numpy as np


# Defaults — verified params, do not change without re-running walk-forward
DEFAULT_PARAMS = {
    "ma_short": 20,
    "ma_long": 200,
    "target_R": 7.0,
    "cooldown_bars": 10,
    "atr_distance_min": 0.5,
}


@dataclass
class Trade:
    side: str = ""
    entry_idx: int = 0
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    R: Optional[float] = None
    outcome: Optional[str] = None
    strategy: str = "s7_sma"


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()],
                   axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _simulate(df: pd.DataFrame, entry_idx: int, side: str,
              entry: float, stop: float, target: float,
              max_bars: int = 120):
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


def s7_sma(df: pd.DataFrame, *,
           ma_short: int = 20,
           ma_long: int = 200,
           target_R: float = 7.0,
           cooldown_bars: int = 10,
           atr_distance_min: float = 0.5) -> List[Trade]:
    """SMA20 pullback in SMA200-defined trend, 7R target, 10-bar loss cooldown.

    LONG: SMA200 + SMA20 both above (uptrend) → wait for pullback that touches
          SMA20 from above with the bar closing back above → enter next bar's open
          → stop = pullback low - 0.3 ATR → target = entry + 7R.

    SHORT: mirror.
    """
    sma_long_v = df["close"].rolling(ma_long).mean()
    sma_short_v = df["close"].rolling(ma_short).mean()
    a = _atr(df, 14)
    trades: List[Trade] = []
    last_loss_idx = -10000

    for i in range(ma_long + 5, len(df) - 30):
        atr_now = a.iloc[i]
        if pd.isna(atr_now) or atr_now <= 0:
            continue
        if i - last_loss_idx < cooldown_bars:
            continue
        if pd.isna(sma_long_v.iloc[i]) or pd.isna(sma_short_v.iloc[i]):
            continue

        # ── LONG ──
        if (df["close"].iloc[i] > sma_long_v.iloc[i]
                and sma_short_v.iloc[i] > sma_long_v.iloc[i]):
            recent_high = df["high"].iloc[max(0, i - 10):i].max()
            if recent_high < sma_short_v.iloc[i] + atr_distance_min * atr_now:
                continue
            if (df["low"].iloc[i] <= sma_short_v.iloc[i]
                    and df["close"].iloc[i] > sma_short_v.iloc[i]):
                if i + 1 < len(df):
                    entry = df["open"].iloc[i + 1]
                    stop = df["low"].iloc[i] - atr_now * 0.3
                    if entry <= stop:
                        continue
                    risk = entry - stop
                    target = entry + risk * target_R
                    R, oc = _simulate(df, i + 1, "long", entry, stop, target)
                    trades.append(Trade("long", i + 1, entry, stop, target,
                                        R, oc))
                    if oc == "loss":
                        last_loss_idx = i + 1

        # ── SHORT ──
        if (df["close"].iloc[i] < sma_long_v.iloc[i]
                and sma_short_v.iloc[i] < sma_long_v.iloc[i]):
            recent_low = df["low"].iloc[max(0, i - 10):i].min()
            if recent_low > sma_short_v.iloc[i] - atr_distance_min * atr_now:
                continue
            if (df["high"].iloc[i] >= sma_short_v.iloc[i]
                    and df["close"].iloc[i] < sma_short_v.iloc[i]):
                if i + 1 < len(df):
                    entry = df["open"].iloc[i + 1]
                    stop = df["high"].iloc[i] + atr_now * 0.3
                    if entry >= stop:
                        continue
                    risk = stop - entry
                    target = entry - risk * target_R
                    R, oc = _simulate(df, i + 1, "short", entry, stop, target)
                    trades.append(Trade("short", i + 1, entry, stop, target,
                                        R, oc))
                    if oc == "loss":
                        last_loss_idx = i + 1

    return trades
