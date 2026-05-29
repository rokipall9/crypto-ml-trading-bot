"""
sparkline.py — Tiny PNG equity-curve renderer for embedding.

Subscribers can embed:
  <img src="https://your-srs.example.com/api/sparkline">

Renders 30-day cumulative R as a small dark-themed sparkline.
"""
from __future__ import annotations

import io
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, "/home/ubuntu/common")
from ledger import closes

BG = "#0F1115"; GRID = "#222731"
GREEN = "#00C851"; RED = "#FF4444"; DIM = "#9CA3AF"


def render_30d(width: int = 600, height: int = 120) -> bytes:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    rows = []
    for r in closes():
        ts_str = r.get("opened_at") or r.get("exit_time") or ""
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            rows.append((ts, float(r.get("r", 0))))
    rows.sort()

    fig, ax = plt.subplots(figsize=(width / 100, height / 100), dpi=100)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xticks([]); ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)

    if not rows:
        ax.text(0.5, 0.5, "No closes in last 30 days",
                ha="center", va="center", color=DIM,
                fontsize=10, transform=ax.transAxes)
    else:
        eq = [0.0]
        for _, r in rows:
            eq.append(eq[-1] + r)
        color = GREEN if eq[-1] >= 0 else RED
        ax.fill_between(range(len(eq)), eq, color=color, alpha=0.2)
        ax.plot(eq, color=color, linewidth=2)
        ax.axhline(0, color=GRID, linewidth=1)
        ax.text(0.99, 0.95, f"{eq[-1]:+.2f}R · 30d",
                ha="right", va="top", color=color, fontsize=11,
                fontweight="bold", transform=ax.transAxes)
        ax.text(0.01, 0.05, f"{len(rows)} trades",
                ha="left", va="bottom", color=DIM, fontsize=9,
                transform=ax.transAxes)

    fig.tight_layout(pad=0.1)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=100, facecolor=BG,
                bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


if __name__ == "__main__":
    out = "/tmp/sparkline_test.png"
    data = render_30d()
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out} ({len(data):,} bytes)")
