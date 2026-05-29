"""
morning_briefing.py — daily 08:00 UTC market context briefing.

Quick overnight recap so subscribers start their day with situational awareness:
  - BTC overnight move
  - F&G change
  - Regime (BULL/CHOP/BEAR)
  - How close any strategy is to firing
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
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


def fetch_btc_24h():
    try:
        with urllib.request.urlopen(
            "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=10
        ) as r:
            data = json.loads(r.read().decode())
            return {
                "price": float(data["lastPrice"]),
                "chg": float(data["priceChangePercent"]),
                "high": float(data["highPrice"]),
                "low": float(data["lowPrice"]),
                "vol": float(data["volume"]),
            }
    except Exception:
        return None


def fetch_fg():
    try:
        with urllib.request.urlopen("https://api.alternative.me/fng/?limit=2", timeout=5) as r:
            data = json.loads(r.read().decode())["data"]
            return int(data[0]["value"]), int(data[1]["value"]) if len(data) > 1 else int(data[0]["value"])
    except Exception:
        return 50, 50


def latest_regime_from_audit():
    """Look at most recent daily_signal scan for current regime."""
    path = "/home/ubuntu/daily_signal/state/audit.jsonl"
    if not os.path.exists(path):
        return "?"
    try:
        with open(path, encoding="utf-8") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 5000))
            chunk = f.read()
        for line in reversed(chunk.splitlines()):
            try:
                r = json.loads(line)
                if r.get("kind") == "scan" and r.get("regime"):
                    return r["regime"]
            except Exception:
                pass
    except Exception:
        pass
    return "?"


def latest_max_score_24h():
    """Highest score in any bot's last 24h."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    max_score = 0
    for path in ["/home/ubuntu/daily_signal/state/audit.jsonl",
                 "/home/ubuntu/pro_signal/state/audit.jsonl"]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        ts = datetime.fromisoformat(r.get("ts", ""))
                        if ts < cutoff:
                            continue
                        if r.get("kind") == "scan":
                            s = r.get("score_total", 0)
                            if s > max_score:
                                max_score = s
                    except Exception:
                        pass
        except Exception:
            pass
    return max_score


def main():
    if not WATCH_URL:
        print("[morning] no webhook"); return

    btc = fetch_btc_24h() or {}
    fg_now, fg_yest = fetch_fg()
    regime = latest_regime_from_audit()
    max_score = latest_max_score_24h()

    chg = btc.get("chg", 0)
    chg_emoji = "🟢" if chg > 1 else "🟡" if chg > -1 else "🔴"
    fg_change = fg_now - fg_yest
    fg_dir = "↗" if fg_change > 0 else "↘" if fg_change < 0 else "→"

    regime_emoji = {"BULL": "📈", "BEAR": "📉", "CHOP": "⏸️"}.get(regime, "❓")

    embed = {
        "title": "🌅  Morning Briefing  ·  " + datetime.now(timezone.utc).strftime("%a %b %d"),
        "description": f"_Overnight context for the day ahead · BTCUSDT_",
        "color": 0xFFD700,
        "fields": [
            {"name": "💰  BTC overnight",
             "value": f"**${btc.get('price', 0):,.0f}**  {chg_emoji} {chg:+.2f}%",
             "inline": True},
            {"name": "📊  24h range",
             "value": f"${btc.get('low', 0):,.0f} → ${btc.get('high', 0):,.0f}",
             "inline": True},
            {"name": "😨  Fear & Greed",
             "value": f"**{fg_now}**  {fg_dir} ({fg_change:+d})",
             "inline": True},
            {"name": "🎯  Regime",
             "value": f"{regime_emoji}  **{regime}**",
             "inline": True},
            {"name": "📈  Best score (24h)",
             "value": f"**{max_score}/10**" + (" — close to firing" if max_score >= 6 else ""),
             "inline": True},
            {"name": "🤖  Mode",
             "value": "**PAPER**", "inline": True},
        ],
        "footer": {"text": "Morning briefing · 08:00 UTC daily · educational"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Quick interpretation
    if max_score >= 7:
        verdict = "🚨 Setup brewing — score ≥7 in last 24h. Watch alerts."
    elif max_score >= 5:
        verdict = "👀 Approaching qualification — keep an eye."
    elif regime == "BULL":
        verdict = "📈 Trend favorable but no setup yet. Patient mode."
    else:
        verdict = "💤 Quiet. Bots scanning, awaiting trigger."
    embed["fields"].append({"name": "📌  Read", "value": verdict, "inline": False})

    pro_format.send_embed(WATCH_URL, embed, username="🌅 Morning Briefing")
    print("[morning] posted")


if __name__ == "__main__":
    main()
