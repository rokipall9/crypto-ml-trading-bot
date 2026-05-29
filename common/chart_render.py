"""
chart_render.py — Discord-attachable PNG charts for signal alerts.

Renders a clean two-panel chart (4H + 1H) with:
  - Candles
  - EMA21 / EMA55
  - Entry / SL / TP horizontal lines
  - Subtle dark theme so it pops in Discord

Returns bytes ready for Discord file attachment.
"""
from __future__ import annotations

import io
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.dates import DateFormatter, HourLocator
import numpy as np
import pandas as pd


# Pro dark palette
BG = "#0F1115"
PANEL = "#16191F"
GRID = "#222731"
GREEN = "#00C851"
RED = "#FF4444"
ORANGE = "#FF8C00"
TEXT = "#E5E7EB"
DIM = "#9CA3AF"


def _draw_candles(ax, df, label):
    df = df.copy().reset_index(drop=True)
    if "open_time" in df.columns:
        x = pd.to_datetime(df["open_time"]).values
    else:
        x = np.arange(len(df))

    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values

    width = (x[1] - x[0]) * 0.7 if hasattr(x[0], 'astype') else 0.7

    for i in range(len(df)):
        is_up = c[i] >= o[i]
        color = GREEN if is_up else RED
        # wick
        ax.plot([x[i], x[i]], [l[i], h[i]], color=color, linewidth=0.7, alpha=0.85)
        # body
        body_low = min(o[i], c[i])
        body_high = max(o[i], c[i])
        ax.add_patch(patches.Rectangle(
            (x[i] - width/2, body_low), width, body_high - body_low,
            facecolor=color, edgecolor=color, alpha=0.85))

    # EMAs
    if len(df) >= 21:
        ema21 = pd.Series(c).ewm(span=21, adjust=False).mean().values
        ax.plot(x, ema21, color="#FFD700", linewidth=1.2, label="EMA21", alpha=0.9)
    if len(df) >= 55:
        ema55 = pd.Series(c).ewm(span=55, adjust=False).mean().values
        ax.plot(x, ema55, color="#9CA3AF", linewidth=1.0, label="EMA55", alpha=0.8)

    ax.set_facecolor(PANEL)
    ax.set_title(label, color=TEXT, fontsize=11, loc="left", pad=8)
    ax.tick_params(colors=DIM, labelsize=8)
    ax.spines[:].set_color(GRID)
    ax.grid(True, color=GRID, linewidth=0.5, alpha=0.4)
    ax.legend(loc="upper left", facecolor=PANEL, edgecolor=GRID,
              labelcolor=TEXT, fontsize=8, framealpha=0.9)
    if hasattr(x[0], 'astype'):
        ax.xaxis.set_major_formatter(DateFormatter("%m-%d %H:%M"))


def _add_levels(ax, entry: Optional[float], sl: Optional[float], tp: Optional[float]):
    if entry is not None:
        ax.axhline(entry, color="#00BFFF", linewidth=1.2, linestyle="--", alpha=0.9)
        ax.text(0.99, entry, f"  ENTRY ${entry:,.0f}", color="#00BFFF",
                ha="right", va="bottom", fontsize=8, transform=ax.get_yaxis_transform())
    if sl is not None:
        ax.axhline(sl, color=RED, linewidth=1.2, linestyle="--", alpha=0.9)
        ax.text(0.99, sl, f"  SL ${sl:,.0f}", color=RED,
                ha="right", va="top", fontsize=8, transform=ax.get_yaxis_transform())
    if tp is not None:
        ax.axhline(tp, color=GREEN, linewidth=1.2, linestyle="--", alpha=0.9)
        ax.text(0.99, tp, f"  TP ${tp:,.0f}", color=GREEN,
                ha="right", va="bottom", fontsize=8, transform=ax.get_yaxis_transform())


def render_signal_chart(symbol: str, df_4h: pd.DataFrame, df_1h: pd.DataFrame,
                        entry: Optional[float] = None,
                        sl: Optional[float] = None,
                        tp: Optional[float] = None,
                        title_suffix: str = "") -> bytes:
    """
    Two-panel chart: 4H (top, last 60 bars) + 1H (bottom, last 100 bars).
    Returns PNG bytes for Discord attachment.
    """
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7),
                                    gridspec_kw={"height_ratios": [1, 1.3]})
    fig.patch.set_facecolor(BG)
    fig.subplots_adjust(left=0.07, right=0.93, top=0.93, bottom=0.07, hspace=0.25)

    # Trim recent bars
    df4 = df_4h.tail(60).copy() if len(df_4h) > 60 else df_4h.copy()
    df1 = df_1h.tail(100).copy() if len(df_1h) > 100 else df_1h.copy()

    _draw_candles(ax1, df4, f"{symbol}  ·  4H trend  ·  {len(df4)} bars")
    _draw_candles(ax2, df1, f"{symbol}  ·  1H entry  ·  {len(df1)} bars")
    _add_levels(ax1, entry, sl, tp)
    _add_levels(ax2, entry, sl, tp)

    # Title
    title = f"{symbol} setup{(' · ' + title_suffix) if title_suffix else ''}"
    fig.suptitle(title, color=TEXT, fontsize=13, fontweight="bold", y=0.98)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110, facecolor=BG, edgecolor="none",
                bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
