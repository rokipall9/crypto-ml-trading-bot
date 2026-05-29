"""
equity_curve.py — render cumulative-R equity curve from forward_results.jsonl.

Returns PNG bytes. Used in weekly + monthly reports.
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


LEDGER = "/home/ubuntu/common/forward_results.jsonl"

BG = "#0F1115"
PANEL = "#16191F"
GRID = "#222731"
GREEN = "#00C851"
RED = "#FF4444"
GOLD = "#FFD700"
TEXT = "#E5E7EB"


def _load_closes():
    if not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("event") == "close":
                    out.append(r)
            except Exception:
                pass
    out.sort(key=lambda r: r.get("exit_time", r.get("opened_at", "")))
    return out


def render_equity_curve(period_label: str = "All time",
                        bot_filter: Optional[str] = None) -> Optional[bytes]:
    """Render equity curve over time. Returns PNG bytes, or None if no trades."""
    closes = _load_closes()
    if bot_filter:
        closes = [r for r in closes if r.get("bot") == bot_filter]
    if not closes:
        return None

    # Build cumulative R
    times = []
    cum_r = []
    running = 0.0
    for r in closes:
        ts_str = r.get("exit_time") or r.get("opened_at")
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        except Exception:
            continue
        running += float(r.get("r", 0))
        times.append(ts)
        cum_r.append(running)

    if not times:
        return None

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)

    # Color line green if up, red if final R < 0
    final_r = cum_r[-1]
    line_color = GREEN if final_r >= 0 else RED

    ax.plot(times, cum_r, color=line_color, linewidth=2.0, marker="o",
            markersize=4, markerfacecolor=line_color, alpha=0.95)
    ax.fill_between(times, 0, cum_r, color=line_color, alpha=0.15)
    ax.axhline(0, color=GRID, linewidth=1.0, alpha=0.7)

    # Highlight max-DD region
    peak_r = 0
    peak_idx = 0
    max_dd = 0
    dd_end_idx = 0
    for i, r in enumerate(cum_r):
        if r > peak_r:
            peak_r = r
            peak_idx = i
        dd = peak_r - r
        if dd > max_dd:
            max_dd = dd
            dd_end_idx = i

    if max_dd > 0:
        ax.fill_between(times[peak_idx:dd_end_idx + 1],
                        cum_r[peak_idx:dd_end_idx + 1], peak_r,
                        color=RED, alpha=0.2, label=f"Max DD: {max_dd:.2f}R")

    # Stats annotation top-right
    n = len(closes)
    wins = sum(1 for r in closes if r.get("status") == "TP")
    wr = wins / n * 100 if n else 0
    stats_text = (f"Trades: {n}  ·  Win rate: {wr:.0f}%\n"
                  f"Total R: {final_r:+.2f}  ·  Max DD: {max_dd:.2f}R")
    ax.text(0.02, 0.97, stats_text, transform=ax.transAxes,
            ha="left", va="top", color=TEXT, fontsize=10, family="monospace",
            bbox=dict(facecolor=PANEL, edgecolor=GRID, boxstyle="round,pad=0.6"))

    ax.set_title(f"Equity Curve  ·  {period_label}",
                 color=TEXT, fontsize=13, fontweight="bold", loc="left", pad=10)
    ax.set_ylabel("Cumulative R", color=TEXT, fontsize=10)
    ax.tick_params(colors=TEXT, labelsize=9)
    ax.spines[:].set_color(GRID)
    ax.grid(True, color=GRID, linewidth=0.5, alpha=0.4)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.autofmt_xdate(rotation=20, ha="right")

    if max_dd > 0:
        ax.legend(loc="lower right", facecolor=PANEL, edgecolor=GRID,
                  labelcolor=TEXT, fontsize=9, framealpha=0.9)

    fig.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110, facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
