"""
convergence_monitor.py — fires "🔥 CONVERGENCE" alert when 2+ bots qualify
on the same trading day. Multi-bot agreement = highest-conviction tier.

Runs hourly. Only posts ONCE per convergence event (deduped via state file).
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
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

SOURCES = [
    {"path": "/home/ubuntu/daily_signal/state/audit.jsonl", "bot": "daily_signal"},
    {"path": "/home/ubuntu/pro_signal/state/audit.jsonl",   "bot": "pro_signal"},
]
CRYPTOBOT_LOG = "/home/ubuntu/bot/logs/bot_live.log"

STATE_FILE = "/home/ubuntu/common/convergence_state.json"
LOOKBACK_HOURS = 24


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            return {"posted_dates": []}
    return {"posted_dates": []}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def collect_recent_qualifiers():
    """Get list of {bot, system, bar_time, score} from last 24h that qualified."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    found = []

    # daily_signal + pro_signal: read JSONL audit
    for src in SOURCES:
        if not os.path.exists(src["path"]):
            continue
        with open(src["path"], encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("kind") != "scan" or not r.get("qualifies"):
                    continue
                ts_str = r.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts < cutoff:
                        continue
                except Exception:
                    continue
                found.append({
                    "bot": src["bot"],
                    "system": r.get("system", "?"),
                    "ts": ts.isoformat(),
                    "score": r.get("score_total", 0),
                    "trade": r.get("trade", {}),
                })

    # cryptobot: parse SIGNAL lines from log (no per-line UTC timestamp; approximate)
    if os.path.exists(CRYPTOBOT_LOG):
        import re
        sig_pat = re.compile(
            r"\[4H\] SIGNAL: \[(\w+)\] BUY BTC @ \$([\d,]+) \| SL=\$([\d,]+)")
        with open(CRYPTOBOT_LOG, encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8000))
            chunk = f.read()
        for line in chunk.splitlines():
            m = sig_pat.search(line)
            if m:
                found.append({
                    "bot": "cryptobot",
                    "system": m.group(1),
                    "ts": datetime.now(timezone.utc).isoformat(),  # approx
                    "score": None,
                    "trade": {"entry": float(m.group(2).replace(",", "")),
                              "sl":   float(m.group(3).replace(",", ""))},
                })
    return found


def main():
    qualifiers = collect_recent_qualifiers()
    if not qualifiers:
        print("[convergence] no qualifying setups in last 24h")
        return

    # Group by calendar day
    by_day = defaultdict(list)
    for q in qualifiers:
        day = q["ts"][:10]
        by_day[day].append(q)

    state = load_state()
    posted = set(state.get("posted_dates", []))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    todays = by_day.get(today, [])

    bots_today = {q["bot"] for q in todays}
    if len(bots_today) >= 2 and today not in posted:
        # CONVERGENCE!
        if not WATCH_URL:
            print("[convergence] would post but no webhook set")
            return

        # Build the embed
        bot_lines = []
        for bot in sorted(bots_today):
            sys_set = set()
            for q in todays:
                if q["bot"] == bot:
                    sys_set.add(q["system"])
            sys_str = ", ".join(sorted(sys_set))
            bot_lines.append(f"`{bot}` → **{sys_str}**")

        embed = {
            "title": "🔥  CONVERGENCE  ·  Multi-bot agreement",
            "description": (f"_**{len(bots_today)} bots qualified today** — "
                            f"highest-conviction setup. Independent edges agreeing._"),
            "color": 0xFFD700,
            "fields": [
                {"name": "🤖 Agreeing bots", "value": "\n".join(bot_lines), "inline": False},
                {"name": "📅 Date", "value": today, "inline": True},
                {"name": "🎯 Conviction", "value": "**HIGH** (2+ bots aligned)", "inline": True},
                {"name": "💡 Interpretation",
                 "value": "When independently-validated bots all flag the same day, statistical edge stacks. Treat with priority.",
                 "inline": False},
            ],
            "footer": {"text": "Convergence Monitor · auto-detected · educational"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        ok = pro_format.send_embed(WATCH_URL, embed, username="🔥 Convergence")
        if ok:
            posted.add(today)
            state["posted_dates"] = list(posted)
            save_state(state)
            print(f"[convergence] posted convergence alert for {today}")
    else:
        print(f"[convergence] today bots={list(bots_today)} (need 2+) · already posted={today in posted}")


if __name__ == "__main__":
    main()
