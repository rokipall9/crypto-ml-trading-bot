"""
daily_summary.py — daily recap fired by systemd timer at 23:00 UTC.

Aggregates the last 24h of audit data from all bots and posts ONE
compact embed to the watch channel. Different from weekly — this is
a quick daily heartbeat so the channel always has activity.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/home/ubuntu/common")
import pro_format


try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass


WATCH_URL = os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip() or \
            os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


SOURCES = [
    {"path": "/home/ubuntu/daily_signal/state/audit.jsonl", "bot": "daily_signal"},
    {"path": "/home/ubuntu/pro_signal/state/audit.jsonl",   "bot": "pro_signal"},
]


def fetch_btc_quote():
    try:
        with urllib.request.urlopen(
            "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=10
        ) as r:
            data = json.loads(r.read().decode())
            return float(data["lastPrice"])
    except Exception:
        return 0


def fetch_fg():
    try:
        with urllib.request.urlopen("https://api.alternative.me/fng/?limit=1", timeout=5) as r:
            return int(json.loads(r.read().decode())["data"][0]["value"])
    except Exception:
        return 50


def aggregate_last_24h():
    """Walk audit logs, count last 24h activity per bot."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    totals = {"scans": 0, "qualifying": 0, "alerts": 0, "max_score": 0,
              "regime_seen": "?", "closest_factor": ""}
    closest_pct = -1.0
    closest_label = ""

    for src in SOURCES:
        path = src["path"]
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                ts_str = r.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                if r.get("kind") == "scan":
                    totals["scans"] += 1
                    s = r.get("score_total", 0)
                    if r.get("qualifies"):
                        totals["qualifying"] += 1
                    if s > totals["max_score"]:
                        totals["max_score"] = s
                    if r.get("regime") and r["regime"] != "?":
                        totals["regime_seen"] = r["regime"]
                    bd = r.get("breakdown") or []
                    if bd:
                        # Find best (highest pct) factor for "closest to firing"
                        for b in bd:
                            pts = b.get("pts", 0)
                            if pts > 0:
                                # Approximate proximity: pts/2 * 100
                                pct = (pts / 2.0) * 100
                                if pct > closest_pct:
                                    closest_pct = pct
                                    closest_label = "%s: %s" % (
                                        b.get("factor", "?"), (b.get("reason") or "")[:60])
                elif r.get("kind") == "alert":
                    totals["alerts"] += 1

    if closest_label:
        totals["closest_factor"] = closest_label
    return totals


def main():
    if not WATCH_URL:
        print("[daily_summary] no watch webhook — skip"); return

    stats = aggregate_last_24h()
    btc_price = fetch_btc_quote()
    fg = fetch_fg()
    date_label = datetime.now(timezone.utc).strftime("%a %b %d, %Y")

    embed = pro_format.build_daily_summary_embed(
        date_label=date_label,
        scans=stats["scans"],
        qualifying=stats["qualifying"],
        alerts=stats["alerts"],
        max_score=stats["max_score"],
        closest_factor=stats["closest_factor"],
        regime=stats["regime_seen"],
        price=btc_price, fg=fg,
    )
    ok = pro_format.send_embed(WATCH_URL, embed, username="📊 Daily Recap")
    print("[daily_summary] posted: %s" % ok)


if __name__ == "__main__":
    main()
