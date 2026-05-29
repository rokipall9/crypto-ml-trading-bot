"""performance_heatmap.py — by-hour and by-day-of-week PNG chart."""
from __future__ import annotations
import io
import json
import os
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

LEDGER = "/home/ubuntu/common/forward_results.jsonl"
BG = "#0F1115"; PANEL = "#16191F"; GRID = "#222731"; TEXT = "#E5E7EB"


def _load_with_times():
    if not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("event") != "close":
                    continue
                ts_str = r.get("opened_at") or r.get("exit_time")
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                out.append((ts, float(r.get("r", 0))))
            except Exception:
                pass
    return out


def render() -> Optional[bytes]:
    rows = _load_with_times()
    if len(rows) < 5:
        return None  # not enough data

    # Build 7x24 matrix of mean R per (day-of-week, hour)
    matrix = [[0.0] * 24 for _ in range(7)]
    counts = [[0] * 24 for _ in range(7)]
    for ts, r in rows:
        dow = ts.weekday()  # 0=Mon
        hr = ts.hour
        matrix[dow][hr] += r
        counts[dow][hr] += 1
    # Convert sums to means
    for d in range(7):
        for h in range(24):
            if counts[d][h] > 0:
                matrix[d][h] /= counts[d][h]
            else:
                matrix[d][h] = float("nan")

    arr = np.array(matrix)
    fig, ax = plt.subplots(figsize=(11, 4.5))
    fig.patch.set_facecolor(BG); ax.set_facecolor(PANEL)

    # Diverging colormap centered at 0
    cmap = plt.cm.RdYlGn
    vmax = max(abs(np.nanmin(arr)) if not np.isnan(np.nanmin(arr)) else 1,
               abs(np.nanmax(arr)) if not np.isnan(np.nanmax(arr)) else 1, 1)
    im = ax.imshow(arr, cmap=cmap, vmin=-vmax, vmax=vmax,
                   aspect="auto", interpolation="nearest")

    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    ax.set_yticks(range(7)); ax.set_yticklabels(days, color=TEXT, fontsize=9)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f"{h:02d}" for h in range(0, 24, 2)],
                       color=TEXT, fontsize=9)
    ax.set_xlabel("Hour (UTC)", color=TEXT, fontsize=10)
    ax.set_title("Performance Heatmap  ·  Mean R by (Day-of-Week × Hour)",
                 color=TEXT, fontsize=12, fontweight="bold", loc="left", pad=10)

    # Annotate cells with count
    for d in range(7):
        for h in range(24):
            n = counts[d][h]
            if n > 0:
                val = matrix[d][h]
                txt_color = "#000" if abs(val) > vmax * 0.4 else TEXT
                ax.text(h, d, f"{n}", ha="center", va="center",
                        color=txt_color, fontsize=7)

    cbar = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02)
    cbar.set_label("Mean R", color=TEXT, fontsize=9)
    cbar.ax.yaxis.set_tick_params(color=TEXT)
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color=TEXT, fontsize=8)

    fig.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110, facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
