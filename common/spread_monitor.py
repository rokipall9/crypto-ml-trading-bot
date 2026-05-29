"""spread_monitor.py — alerts on large multi-exchange BTC spreads (every 30 min)."""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/home/ubuntu/common")
import pro_format
import market_intel

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass

WATCH_URL = (os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
             or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())
STATE = "/home/ubuntu/common/spread_state.json"
SPREAD_PCT_THRESHOLD = 0.5   # alert if max - min > 0.5%
COOLDOWN_HOURS = 6


def load_state():
    if os.path.exists(STATE):
        try: return json.load(open(STATE))
        except: pass
    return {"last_alert": None}


def save_state(s):
    json.dump(s, open(STATE, "w"))


def main():
    prices = market_intel.fetch_multi_exchange_btc()
    valid = {k: v for k, v in prices.items() if v is not None}
    if len(valid) < 2:
        print("[spread] insufficient exchanges"); return

    lo = min(valid.values()); hi = max(valid.values())
    spread_pct = (hi - lo) / lo * 100

    if spread_pct < SPREAD_PCT_THRESHOLD:
        print(f"[spread] normal ({spread_pct:.3f}%)"); return

    state = load_state()
    if state["last_alert"]:
        try:
            last = datetime.fromisoformat(state["last_alert"])
            if datetime.now(timezone.utc) - last < timedelta(hours=COOLDOWN_HOURS):
                print("[spread] cooldown active"); return
        except: pass

    if not WATCH_URL:
        print("[spread] no webhook"); return

    rows = "\n".join(
        f"  {ex:<10} ${price:,.2f}" for ex, price in sorted(valid.items()))
    embed = {
        "title": f"⚠  Multi-exchange spread alert  ·  {spread_pct:.3f}%",
        "description": ("_BTC price desync between exchanges — possible illiquidity, "
                        "data-source issue, or rare arbitrage window._"),
        "color": 0xFFA500,
        "fields": [
            {"name": "Prices", "value": f"```\n{rows}\n```", "inline": False},
            {"name": "Min", "value": f"${lo:,.2f}", "inline": True},
            {"name": "Max", "value": f"${hi:,.2f}", "inline": True},
            {"name": "Spread", "value": f"**{spread_pct:.3f}%**", "inline": True},
        ],
        "footer": {"text": "Spread monitor · 30min cadence · alerts on >0.5% spread"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if pro_format.send_embed(WATCH_URL, embed, username="⚠ Spread Monitor"):
        state["last_alert"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        print(f"[spread] alerted {spread_pct:.3f}%")


if __name__ == "__main__":
    main()
