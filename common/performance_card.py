"""performance_card.py — render Twitter-ready PNG with monthly performance stats."""
from __future__ import annotations
import io, json, os, sys
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches

sys.path.insert(0, "/home/ubuntu/common")
import track_record

BG = "#0F1115"; PANEL = "#16191F"; GRID = "#222731"
GREEN = "#00C851"; RED = "#FF4444"; GOLD = "#FFD700"
TEXT = "#E5E7EB"; DIM = "#9CA3AF"


def render_card(period_label: str = None) -> bytes:
    if period_label is None:
        period_label = datetime.now(timezone.utc).strftime("%B %Y")

    summary = track_record.all_summary()
    n = summary["n"]
    wr = summary["wr"]
    total_r = summary["total_r"]

    # Card is 1200x675 (Twitter / X recommended)
    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=120)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 12); ax.set_ylim(0, 6.75)
    ax.set_xticks([]); ax.set_yticks([])
    for spine in ax.spines.values(): spine.set_visible(False)

    # Title
    ax.text(0.5, 6.0, "Pro Signal Bot",
            color=GOLD, fontsize=32, fontweight="bold", family="sans-serif")
    ax.text(0.5, 5.4, f"Performance Recap · {period_label}",
            color=TEXT, fontsize=16, family="sans-serif")

    # Three stat cards
    card_y = 2.5
    card_w = 3.5
    card_h = 2.0
    card_gap = 0.3
    card_x_start = 0.5

    cards = [
        ("Trades", f"{n}", TEXT),
        ("Win Rate", f"{wr:.1f}%", GREEN if wr >= 40 else (RED if wr < 30 and n > 5 else GOLD)),
        ("Total R", f"{total_r:+.2f}R", GREEN if total_r > 0 else (RED if total_r < 0 else DIM)),
    ]

    for i, (label, val, color) in enumerate(cards):
        x = card_x_start + i * (card_w + card_gap)
        # Card background
        rect = patches.FancyBboxPatch(
            (x, card_y), card_w, card_h,
            boxstyle="round,pad=0.05,rounding_size=0.15",
            facecolor=PANEL, edgecolor=GRID, linewidth=1.5)
        ax.add_patch(rect)
        # Label
        ax.text(x + card_w/2, card_y + card_h - 0.4, label,
                color=DIM, fontsize=14, ha="center", family="sans-serif")
        # Value
        ax.text(x + card_w/2, card_y + 0.6, val,
                color=color, fontsize=42, ha="center",
                fontweight="bold", family="sans-serif")

    # Bottom strip
    ax.text(0.5, 1.3, "BTCUSDT · 4H trend / 1H entry · walk-forward validated",
            color=DIM, fontsize=12, family="sans-serif")
    ax.text(0.5, 0.8, "Educational · paper-traded · not financial advice",
            color=DIM, fontsize=10, style="italic", family="sans-serif")
    ax.text(11.5, 0.8, datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            color=DIM, fontsize=10, ha="right", family="sans-serif")

    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=120, facecolor=BG, edgecolor="none",
                bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


if __name__ == "__main__":
    img = render_card()
    out = "/home/ubuntu/common/performance_card_latest.png"
    with open(out, "wb") as f:
        f.write(img)
    print(f"[performance_card] wrote {len(img):,} bytes to {out}")
