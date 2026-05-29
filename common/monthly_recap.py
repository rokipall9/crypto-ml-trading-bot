"""
monthly_recap.py — first of every month, post comprehensive performance recap.

Includes equity curve PNG, by-strategy attribution, win/loss table.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/home/ubuntu/common")
import pro_format
import equity_curve
import track_record
try:
    import risk_metrics
except Exception:
    risk_metrics = None
try:
    import performance_heatmap
except Exception:
    performance_heatmap = None

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass


# MONTHLY_WEBHOOK_v1: prefer dedicated monthly channel
WATCH_URL = (os.environ.get("DISCORD_WEBHOOK_URL_MONTHLY", "").strip()
             or os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
             or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())


def main():
    if not WATCH_URL:
        print("[monthly] no webhook"); return

    now = datetime.now(timezone.utc)
    month_label = now.strftime("%B %Y")
    period_start = (now - timedelta(days=30)).strftime("%b %d")
    period_end = now.strftime("%b %d, %Y")

    # Aggregate stats
    summary = track_record.all_summary()
    per_strat = track_record.per_strategy_table()

    # Build attribution lines
    if per_strat:
        sorted_strats = sorted(per_strat.items(),
                               key=lambda kv: -kv[1]["total_r"])
        attribution = "\n".join(
            f"`{k:<20}` {v['label']}" for k, v in sorted_strats)
    else:
        attribution = "_no resolved trades this month yet_"

    # Verdict
    if summary["n"] == 0:
        verdict = "📊 **Patient month** — no trades resolved. Bots scanning, edge waiting for setups."
    elif summary["total_r"] >= 5:
        verdict = f"🟢 **Strong month** — total **{summary['total_r']:+.2f}R** across {summary['n']} trades."
    elif summary["total_r"] >= 0:
        verdict = f"🟡 **Positive month** — total **{summary['total_r']:+.2f}R**, ground out."
    else:
        verdict = f"🔴 **Negative month** — total **{summary['total_r']:+.2f}R**. Review forward-test data."

    # Risk-adjusted metrics
    risk_field_value = "_no data yet_"
    if risk_metrics:
        try:
            m = risk_metrics.compute_metrics()
            if m["n"] > 0:
                sharpe = "—" if m["sharpe"] is None else f"{m['sharpe']:.2f}"
                sortino = "—" if m["sortino"] is None else f"{m['sortino']:.2f}"
                calmar = "—" if m["calmar"] is None else f"{m['calmar']:.2f}"
                risk_field_value = (
                    f"Sharpe: **{sharpe}**\n"
                    f"Sortino: **{sortino}**\n"
                    f"Calmar: **{calmar}**\n"
                    f"Max DD: **{m['max_dd']:.2f}R**")
        except Exception as e:
            print("[monthly] risk_metrics failed: %s" % e)

    embed = {
        "title": f"📅  Monthly Recap  ·  {month_label}",
        "description": f"_Comprehensive performance for {period_start} – {period_end}_",
        "color": pro_format.COLOR_WEEKLY,
        "fields": [
            {"name": "📊  Summary",
             "value": (f"Trades: **{summary['n']}**\n"
                       f"Wins: **{summary.get('tp', 0)}**  ·  Losses: **{summary.get('sl', 0)}**\n"
                       f"Win rate: **{summary['wr']:.1f}%**\n"
                       f"Total R: **{summary['total_r']:+.2f}**"),
             "inline": True},
            {"name": "📐  Risk-Adjusted",
             "value": risk_field_value,
             "inline": True},
            {"name": "🏆  By Strategy",
             "value": f"```\n{attribution[:900]}\n```" if attribution != "_no resolved trades this month yet_" else attribution,
             "inline": False},
            {"name": "📈  Verdict", "value": verdict, "inline": False},
        ],
        "footer": {"text": f"Monthly recap · auto-generated 1st of month · educational"},
        "timestamp": now.isoformat(),
    }

    # Render equity curve
    chart_bytes = equity_curve.render_equity_curve(period_label=month_label)

    ok = pro_format.send_embed(WATCH_URL, embed, username="📅 Monthly Recap",
                                image_bytes=chart_bytes)
    print(f"[monthly] posted: {ok}")


if __name__ == "__main__":
    main()
