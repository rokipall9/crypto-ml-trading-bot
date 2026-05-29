"""
open_trade_tracker.py — every hour, post live P&L for any currently-open
forward-test trades. Goes silent if nothing's open.
"""
from __future__ import annotations
import json, os, sys, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/home/ubuntu/common")
import pro_format

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass

WATCH_URL = (os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
             or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())
TRACKER_STATE = "/home/ubuntu/common/forward_tracker_state.json"


def fetch_btc_price():
    try:
        with urllib.request.urlopen(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=8
        ) as r:
            return float(json.loads(r.read().decode())["price"])
    except Exception:
        return 0


def main():
    if not os.path.exists(TRACKER_STATE):
        return
    try:
        state = json.load(open(TRACKER_STATE))
    except Exception:
        return
    open_trades = state.get("open_trades", [])
    if not open_trades:
        return  # silent

    price = fetch_btc_price()
    if not price:
        return

    fields = []
    total_unrealized = 0
    for t in open_trades:
        entry = float(t.get("entry", 0))
        sl = float(t.get("sl", 0))
        tp = float(t.get("tp", 0))
        if entry <= 0 or sl >= entry:
            continue
        risk = entry - sl
        unrealized_r = (price - entry) / risk
        total_unrealized += unrealized_r
        progress_pct = max(-100, min(100, (price - entry) / risk * 50 + 50))
        bar = pro_format._bar(progress_pct, 12) if hasattr(pro_format, "_bar") else "—"
        side = "🟢" if unrealized_r > 0 else "🔴"
        bot_sys = "%s/%s" % (t.get("bot", "?"), t.get("system", "?"))
        fields.append({
            "name": f"{side}  {bot_sys}",
            "value": (f"`{bar}`\n"
                      f"Entry: ${entry:,.0f}  ·  Now: ${price:,.0f}  ·  **{unrealized_r:+.2f}R**"),
            "inline": False,
        })

    if not fields:
        return

    color = pro_format.COLOR_LONG if total_unrealized > 0 else pro_format.COLOR_SHORT
    embed = {
        "title": f"📊  Open Positions  ·  {len(open_trades)} live",
        "description": f"_Unrealized total: **{total_unrealized:+.2f}R** at BTC ${price:,.0f}_",
        "color": color,
        "fields": fields,
        "footer": {"text": "Open trade tracker · hourly · educational"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    pro_format.send_embed(WATCH_URL, embed, username="📊 Open Trades")
    print(f"[open_trades] posted {len(open_trades)} open trade(s)")


if __name__ == "__main__":
    main()
