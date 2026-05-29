"""
anomaly_monitor.py — alerts on big BTC moves, F&G crashes, regime shifts.

Runs every 30 minutes. Compares current state to N-hours-ago.
Posts only on threshold breach (deduped via state file).
"""
from __future__ import annotations
import json, os, sys, urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/home/ubuntu/common")
import pro_format

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass

WATCH_URL = (os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
             or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())
STATE = "/home/ubuntu/common/anomaly_state.json"

# Thresholds
PRICE_MOVE_1H_THRESHOLD = 3.0    # % move in 1h that triggers alert
FG_DROP_THRESHOLD = 15            # F&G drop in 24h that triggers
COOLDOWN_HOURS = 4                # min hours between alerts of same type


def load_state():
    if not os.path.exists(STATE):
        return {"last_alerts": {}}
    try: return json.load(open(STATE))
    except Exception: return {"last_alerts": {}}


def save_state(s):
    json.dump(s, open(STATE, "w"), indent=2)


def fetch_klines_1h(n=2):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit={n}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception:
        return []


def fetch_fg_2():
    try:
        with urllib.request.urlopen("https://api.alternative.me/fng/?limit=2", timeout=5) as r:
            d = json.loads(r.read().decode())["data"]
            return int(d[0]["value"]), int(d[1]["value"]) if len(d) > 1 else int(d[0]["value"])
    except Exception:
        return 50, 50


def can_alert(state, kind):
    last = state["last_alerts"].get(kind)
    if not last:
        return True
    try:
        ts = datetime.fromisoformat(last)
        return (datetime.now(timezone.utc) - ts) >= timedelta(hours=COOLDOWN_HOURS)
    except Exception:
        return True


def mark_alert(state, kind):
    state["last_alerts"][kind] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def main():
    state = load_state()
    alerts_to_post = []

    # 1. Big 1H price move
    klines = fetch_klines_1h(2)
    if len(klines) >= 2:
        prev_close = float(klines[-2][4])
        cur_close = float(klines[-1][4])
        move_pct = (cur_close - prev_close) / prev_close * 100
        if abs(move_pct) >= PRICE_MOVE_1H_THRESHOLD and can_alert(state, "price_move"):
            direction = "🚀 SPIKE" if move_pct > 0 else "💥 CRASH"
            color = pro_format.COLOR_LONG if move_pct > 0 else pro_format.COLOR_SHORT
            alerts_to_post.append(("price_move", {
                "title": f"⚡  {direction}  ·  BTC moved {move_pct:+.2f}% in 1h",
                "description": f"_Volatility event detected — bots may behave differently in the next bars_",
                "color": color,
                "fields": [
                    {"name": "1h ago", "value": f"${prev_close:,.0f}", "inline": True},
                    {"name": "Now", "value": f"${cur_close:,.0f}", "inline": True},
                    {"name": "Move", "value": f"**{move_pct:+.2f}%**", "inline": True},
                ],
                "footer": {"text": "Anomaly monitor · 30min cadence · educational"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }))

    # 2. F&G crash
    fg_now, fg_prev = fetch_fg_2()
    fg_change = fg_now - fg_prev
    if abs(fg_change) >= FG_DROP_THRESHOLD and can_alert(state, "fg"):
        direction = "📉 sentiment crash" if fg_change < 0 else "📈 sentiment surge"
        alerts_to_post.append(("fg", {
            "title": f"🌡  Fear & Greed shift  ·  {direction}",
            "description": f"_F&G moved by **{fg_change:+d}** vs yesterday — sentiment regime change_",
            "color": pro_format.COLOR_SHORT if fg_change < 0 else pro_format.COLOR_LONG,
            "fields": [
                {"name": "Yesterday", "value": f"**{fg_prev}**", "inline": True},
                {"name": "Today", "value": f"**{fg_now}**", "inline": True},
                {"name": "Δ", "value": f"**{fg_change:+d}**", "inline": True},
            ],
            "footer": {"text": "Anomaly monitor · F&G threshold breach"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))

    if not alerts_to_post:
        print("[anomaly] no events")
        return

    if not WATCH_URL:
        print("[anomaly] no webhook"); return

    for kind, embed in alerts_to_post:
        if pro_format.send_embed(WATCH_URL, embed, username="⚡ Anomaly Monitor"):
            mark_alert(state, kind)
            print(f"[anomaly] posted {kind}")


if __name__ == "__main__":
    main()
