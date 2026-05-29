"""r_histogram.py — R-multiple distribution chart from forward_results.jsonl"""
from __future__ import annotations
import io, json, os
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

LEDGER = "/home/ubuntu/common/forward_results.jsonl"
BG = "#0F1115"; PANEL = "#16191F"; GRID = "#222731"
GREEN = "#00C851"; RED = "#FF4444"; TEXT = "#E5E7EB"


def render(period_label: str = "All time") -> Optional[bytes]:
    if not os.path.exists(LEDGER):
        return None
    rs = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("event") == "close":
                    rs.append(float(r.get("r", 0)))
            except Exception:
                pass
    if not rs:
        return None

    fig, ax = plt.subplots(figsize=(9, 4.5))
    fig.patch.set_facecolor(BG); ax.set_facecolor(PANEL)

    bins = np.arange(min(min(rs), -1.5), max(max(rs), 4.5) + 0.5, 0.5)
    n, edges, patches = ax.hist(rs, bins=bins, edgecolor=GRID, linewidth=0.5)
    for i, p in enumerate(patches):
        center = (edges[i] + edges[i+1]) / 2
        p.set_facecolor(GREEN if center > 0 else RED)
        p.set_alpha(0.85)

    avg = np.mean(rs)
    ax.axvline(avg, color="#FFD700", linestyle="--", linewidth=1.5,
               label=f"Mean R: {avg:+.2f}")
    ax.axvline(0, color=TEXT, linewidth=0.8, alpha=0.5)

    ax.set_title(f"R-Multiple Distribution  ·  {period_label}",
                 color=TEXT, fontsize=12, fontweight="bold", loc="left", pad=10)
    ax.set_xlabel("R-multiple", color=TEXT, fontsize=10)
    ax.set_ylabel("Frequency", color=TEXT, fontsize=10)
    ax.tick_params(colors=TEXT, labelsize=9)
    ax.spines[:].set_color(GRID)
    ax.grid(True, color=GRID, axis="y", linewidth=0.5, alpha=0.4)
    ax.legend(loc="upper right", facecolor=PANEL, edgecolor=GRID,
              labelcolor=TEXT, fontsize=9, framealpha=0.9)

    fig.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110, facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
