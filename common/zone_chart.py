"""
zone_chart.py — Render a focused chart PNG for each Discord alert.

Style: clean dark theme, candles, OB + FVG boxes overlaid, current price
as a dotted gold line, the alert's cluster highlighted with stronger
opacity, an arrow showing approach direction.

Designed to be small enough to embed in Discord (target 1000x600px,
~50-100KB per PNG).
"""
from __future__ import annotations

import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.patches import FancyArrow
import pandas as pd
from typing import List, Dict, Optional


# ─── LuxAlgo monochrome theme (matches TradingView screenshot) ───────
BG           = "#000000"      # pure black
MONO_BULL    = "#b2b5be"      # LuxAlgo MONO_BULLISH (light grey candle)
MONO_BEAR    = "#5d606b"      # LuxAlgo MONO_BEARISH (dark grey candle)
DIM          = "#777a85"
TICK         = "#5a5d68"
LINE_PRICE   = "#aaaaaa"      # dotted price line color
GOLD         = "#ffcf3d"

# Zone fills — colors match LuxAlgo defaults, low alpha for clean look
OB_BULL      = "#1848cc"
OB_BEAR      = "#b22833"
INT_OB_BULL  = "#3179f5"
INT_OB_BEAR  = "#f77c80"
FVG_BULL     = "#00ff68"
FVG_BEAR     = "#ff0008"


def _zone_style(kind: str, in_cluster: bool) -> tuple:
    """Return (fill_color, fill_alpha, label_color, label_text)."""
    if "INT_OB_bullish" in kind:
        c, label = INT_OB_BULL, "OB"
    elif "INT_OB_bearish" in kind:
        c, label = INT_OB_BEAR, "OB"
    elif kind == "OB_bullish":
        c, label = OB_BULL, "OB"
    elif kind == "OB_bearish":
        c, label = OB_BEAR, "OB"
    elif kind == "FVG_bullish":
        c, label = FVG_BULL, "FVG"
    else:
        c, label = FVG_BEAR, "FVG"
    alpha = 0.30 if in_cluster else 0.10
    return c, alpha, c, label


def render_zone_chart(df: pd.DataFrame,
                      current_price: float,
                      cluster: List[Dict],
                      all_zones: List[Dict],
                      direction: str,
                      tier: float,
                      tfs: List[str],
                      n_bars: int = 120,
                      ) -> bytes:
    """Render the alert chart and return PNG bytes — LuxAlgo monochrome style."""
    if df is None or len(df) == 0:
        return b""

    full_n = len(df)
    df_recent = df.tail(n_bars).reset_index(drop=True)
    visible_offset = full_n - len(df_recent)   # idx in full df → idx in visible

    # Y-axis bounds — focus on cluster + recent action
    cluster_low = min(z["low"] for z in cluster)
    cluster_high = max(z["high"] for z in cluster)
    p_low = min(df_recent["low"].min(), cluster_low, current_price)
    p_high = max(df_recent["high"].max(), cluster_high, current_price)
    pad = max((p_high - p_low) * 0.06, current_price * 0.003)
    y_lo = p_low - pad
    y_hi = p_high + pad

    fig, ax = plt.subplots(figsize=(13, 6.5), facecolor=BG, dpi=100)
    ax.set_facecolor(BG)

    # Visible zones only (within y-range)
    visible_zones = [z for z in all_zones
                     if z["high"] >= y_lo and z["low"] <= y_hi]
    cluster_keys = {(z["kind"], round(z["low"]), round(z["high"]))
                    for z in cluster}

    # ─── Zones — LuxAlgo style: extend from formation bar to right edge ──
    # Right-side extension (so the box sticks past the last candle)
    right_x = n_bars + 8
    for z in visible_zones:
        zk = (z["kind"], round(z["low"]), round(z["high"]))
        in_cluster = zk in cluster_keys
        fill_color, alpha, label_color, label = _zone_style(z["kind"], in_cluster)

        # Map zone formation idx → visible-window x. If formed before
        # the visible window starts, anchor at left edge.
        if z.get("idx") is not None:
            box_x = max(0, int(z["idx"]) - visible_offset)
        else:
            box_x = 0
        box_w = right_x - box_x
        if box_w <= 0:
            continue

        rect = patches.Rectangle(
            (box_x, z["low"]), box_w, z["high"] - z["low"],
            facecolor=fill_color, edgecolor="none",
            alpha=alpha, zorder=1)
        ax.add_patch(rect)

        # Centered "FVG" or "OB" label inside the box (mid x, mid y)
        zone_height = z["high"] - z["low"]
        # Only show label if box is tall enough to fit it
        if zone_height > (y_hi - y_lo) * 0.008:
            mid_x = box_x + box_w * 0.55
            mid_y = (z["low"] + z["high"]) / 2.0
            ax.text(mid_x, mid_y, label,
                    color=label_color,
                    fontsize=10 if in_cluster else 8,
                    fontweight="bold" if in_cluster else "normal",
                    alpha=0.85 if in_cluster else 0.55,
                    ha="center", va="center", zorder=2,
                    fontfamily="sans-serif")

    # ─── Candles — LuxAlgo monochrome ────────────────────────────────
    for i, row in df_recent.iterrows():
        is_up = row["close"] >= row["open"]
        c = MONO_BULL if is_up else MONO_BEAR
        # Wick
        ax.plot([i, i], [row["low"], row["high"]],
                color=c, linewidth=0.9, zorder=3, solid_capstyle="butt")
        # Body
        body_low = min(row["open"], row["close"])
        body_high = max(row["open"], row["close"])
        body_height = max(body_high - body_low, (y_hi - y_lo) * 0.0008)
        body = patches.Rectangle(
            (i - 0.32, body_low), 0.64, body_height,
            facecolor=c, edgecolor=c, linewidth=0.4, zorder=3)
        ax.add_patch(body)

    # ─── Current price — dotted line spanning full width ─────────────
    ax.axhline(current_price, color=LINE_PRICE, linewidth=0.9,
               linestyle=(0, (2, 3)), alpha=0.7, zorder=5)
    # Right-edge price tag
    ax.text(right_x + 1, current_price,
            f"{current_price:,.1f}", color=GOLD,
            fontsize=10, va="center", ha="left",
            fontfamily="monospace", fontweight="bold", zorder=6)

    # ─── Direction arrow — shows EXPECTED MOVE AFTER hitting the OB ───
    # Bullish OB: arrow up from cluster → toward nearest bearish zone above
    # Bearish OB: arrow down from cluster → toward nearest bullish zone below
    arrow_x = n_bars + 2     # right side of chart, after the candles
    if direction == "bullish":
        arrow_color = "#3179f5"            # bright blue (bullish move)
        # Find nearest bearish zone above cluster (likely TP)
        bearish_above = [z for z in visible_zones
                         if "bearish" in z["kind"] and z["low"] > cluster_high]
        if bearish_above:
            target = min(bearish_above, key=lambda z: z["low"])
            target_y = (target["low"] + target["high"]) / 2.0
        else:
            target_y = y_hi - (y_hi - y_lo) * 0.05      # toward top
        ax.annotate("",
                    xy=(arrow_x, target_y),
                    xytext=(arrow_x, cluster_high),
                    arrowprops=dict(arrowstyle="-|>", color=arrow_color,
                                    lw=3.0, alpha=0.9, mutation_scale=28),
                    zorder=6)
    else:
        arrow_color = "#f23645"            # bright red (bearish move)
        # Find nearest bullish zone below cluster (likely TP)
        bullish_below = [z for z in visible_zones
                         if "bullish" in z["kind"] and z["high"] < cluster_low]
        if bullish_below:
            target = max(bullish_below, key=lambda z: z["high"])
            target_y = (target["low"] + target["high"]) / 2.0
        else:
            target_y = y_lo + (y_hi - y_lo) * 0.05      # toward bottom
        ax.annotate("",
                    xy=(arrow_x, target_y),
                    xytext=(arrow_x, cluster_low),
                    arrowprops=dict(arrowstyle="-|>", color=arrow_color,
                                    lw=3.0, alpha=0.9, mutation_scale=28),
                    zorder=6)

    # ─── Top title — clean, monospace ────────────────────────────────
    tier_label = {1: "Tier 1 OB", 1.5: "Tier 1.5 OB+path",
                  2: "Tier 2 Combo", 2.5: "Tier 2.5 Multi-TF",
                  3: "Tier 3 PRIME"}.get(tier, "")
    arrow_t = "↓" if direction == "bullish" else "↑"
    title = f"BTCUSDT {arrow_t} {tier_label} · {' · '.join(tfs)}"
    ax.text(0.5, 1.015, title, transform=ax.transAxes,
            color=GOLD if tier >= 2.5 else "#cccccc",
            fontsize=11, fontweight="bold", ha="center",
            fontfamily="monospace")

    # ─── Axes — clean, no grid, subtle right-side y-ticks ────────────
    ax.set_xlim(-1, right_x + 8)
    ax.set_ylim(y_lo, y_hi)
    ax.tick_params(colors=TICK, labelsize=8, length=2)
    ax.set_xticks([])
    # Move y-ticks to right side (TradingView style)
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    # Hide all spines for clean look
    for spine in ax.spines.values():
        spine.set_visible(False)
    # Format y-axis labels as price
    ax.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    plt.subplots_adjust(left=0.02, right=0.93, top=0.94, bottom=0.04)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


if __name__ == "__main__":
    # CLI: render a sample chart with current Bybit data + a fake cluster
    import os, sys
    sys.path.insert(0, "/home/ubuntu/common")
    sys.path.insert(0, "/home/ubuntu/bot")
    os.environ.setdefault("ZONE_ALERTER_EXCHANGE", "bybit")
    import zone_alerter as za
    import luxalgo_smc as lux

    df = za._fetch_klines_for_alerter("15m", 200)
    cur = float(df["close"].iloc[-1])
    zones = lux.all_active_zones(df)
    for z in zones:
        z["tf"] = "15m"
    # Pick the closest bullish cluster (just for sample)
    bullish_zones = [z for z in zones
                     if "bullish" in z["kind"] and z["high"] < cur]
    if bullish_zones:
        # nearest below
        bullish_zones.sort(key=lambda z: -z["high"])
        cluster = bullish_zones[:3]
        png = render_zone_chart(df, cur, cluster, zones,
                                direction="bullish",
                                tier=2.5, tfs=["15m"])
        with open("/tmp/sample_zone_chart.png", "wb") as f:
            f.write(png)
        print(f"Wrote /tmp/sample_zone_chart.png ({len(png)} bytes)")
    else:
        print("No bullish zones below current price for sample")
