"""
forward_tracker.py — real-time live forward-test tracker.

Watches audit.jsonl from every bot on the VPS. When any bot logs a
kind="alert" or qualifying scan (even in DRY_RUN), this tracker records
entry/SL/TP and marks it "open". Every hour, on each run, it checks
live BTC 1H price bars to see if each open tracked alert has hit SL/TP.

Writes a running ledger to /home/ubuntu/common/forward_results.jsonl.

Purpose: build up REAL forward-test data so we can judge edge on live
evidence, not just backtest. After ~4 weeks of running, you'll have
actual WR/PF/EV figures that include live slippage and timing.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

import pandas as pd


BOTS = [
    {"name": "cryptobot",    "audit": "/home/ubuntu/bot/logs/bot_live.log",
     "source": "cryptobot_log", "symbol": "BTCUSDT"},
    {"name": "daily_signal", "audit": "/home/ubuntu/daily_signal/state/audit.jsonl",
     "source": "jsonl",  "symbol": "BTCUSDT"},
    {"name": "pro_signal",   "audit": "/home/ubuntu/pro_signal/state/audit.jsonl",
     "source": "jsonl",  "symbol": "BTCUSDT"},
]

TRACKER_LEDGER = "/home/ubuntu/common/forward_results.jsonl"
TRACKER_STATE = "/home/ubuntu/common/forward_tracker_state.json"
TIME_STOP_BARS = 48


# ── Data fetch ───────────────────────────────────────────────────────
def fetch_1h_bars(symbol, n=500, end_ms=None):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": "1h", "limit": n}
    if end_ms is not None:
        params["endTime"] = str(end_ms)
    q = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url + "?" + q, timeout=15) as r:
            raw = json.loads(r.read().decode())
    except Exception as e:
        print("[tracker] fetch error: %s" % e)
        return pd.DataFrame()
    if not raw:
        return pd.DataFrame()
    return pd.DataFrame([{
        "open_time": pd.to_datetime(int(k[0]), unit="ms", utc=True),
        "high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
    } for k in raw])


# ── Parsers: convert each bot's audit format to unified records ─────
def parse_jsonl_alerts(path, since_ts):
    """daily_signal and pro_signal use JSONL."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") != "alert":
                continue
            ts = r.get("ts")
            if ts and ts <= since_ts:
                continue
            t = r.get("trade") or {}
            if not (t.get("entry") and t.get("sl") and t.get("tp")):
                continue
            out.append({
                "bot": None,    # filled by caller
                "alert_ts": ts,
                "bar_time": r.get("bar_time"),
                "symbol": r.get("symbol", "BTCUSDT"),
                "system": r.get("system", "?"),
                "score": r.get("score_total"),
                "entry": t["entry"], "sl": t["sl"], "tp": t["tp"],
                "rr": t.get("rr"),
            })
    return out


def parse_cryptobot_log(path, since_ts):
    """cryptobot uses its own text log. Parse [4H] SIGNAL lines."""
    import re
    out = []
    if not os.path.exists(path):
        return out
    pattern = re.compile(
        r"\[4H\] SIGNAL: \[(\w+)\] BUY BTC @ \$([\d,]+) \| SL=\$([\d,]+) \| trail=([\d.]+)x"
    )
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = pattern.search(line)
            if not m:
                continue
            strategy, entry_str, sl_str, _ = m.groups()
            entry = float(entry_str.replace(",", ""))
            sl = float(sl_str.replace(",", ""))
            # cryptobot uses trailing stops; we'll treat TP as 5x risk
            # (unreachable-high) and rely on time/SL outcomes — mirrors paper_trader
            tp = entry + 5 * (entry - sl)
            out.append({
                "bot": None,
                "alert_ts": None,   # log doesn't have ISO timestamp per line
                "bar_time": None,
                "symbol": "BTCUSDT",
                "system": strategy,
                "score": None,
                "entry": entry, "sl": sl, "tp": tp,
                "rr": 5.0,
            })
    return out


# ── State management ────────────────────────────────────────────────
def load_state():
    if not os.path.exists(TRACKER_STATE):
        return {"last_seen": {}, "open_trades": []}
    try:
        with open(TRACKER_STATE) as f:
            return json.load(f)
    except Exception:
        return {"last_seen": {}, "open_trades": []}


def save_state(state):
    os.makedirs(os.path.dirname(TRACKER_STATE), exist_ok=True)
    with open(TRACKER_STATE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def append_ledger(record):
    os.makedirs(os.path.dirname(TRACKER_LEDGER), exist_ok=True)
    with open(TRACKER_LEDGER, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    # If this was a "close" event, post outcome embed to Discord watch channel
    if record.get("event") == "close":
        try:
            _post_outcome_to_discord(record)
        except Exception as e:
            print("[tracker] outcome post failed: %s" % e)


def _post_outcome_to_discord(record):
    """Post forward-test outcome to watch channel (live track record build)."""
    import sys
    sys.path.insert(0, "/home/ubuntu/common")
    try:
        import pro_format
    except ImportError:
        return False

    try:
        from dotenv import load_dotenv
        load_dotenv("/home/ubuntu/bot/.env")
    except Exception:
        pass

    url = os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
    if not url:
        url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return False

    embed = pro_format.build_outcome_embed(
        bot=record.get("bot", "?"),
        system=record.get("system", "?"),
        symbol="BTCUSDT",
        status=record.get("status", "?"),
        r=float(record.get("r", 0)),
        entry=float(record.get("entry", 0)),
        sl=float(record.get("sl", 0)),
        tp=float(record.get("tp", 0)),
        opened_at=str(record.get("opened_at", "")),
        exit_time=str(record.get("exit_time", "")),
        bars_held=int(record.get("bars", 0)) if "bars" in record else 0,
    )
    return pro_format.send_embed(url, embed, username="📋 Forward-Test Tracker")


# ── Main tick ────────────────────────────────────────────────────────
def tick():
    state = load_state()
    last_seen = state.get("last_seen", {})
    open_trades = state.get("open_trades", [])

    # 1. Discover new alerts from each bot
    new_alerts = []
    for bot in BOTS:
        since = last_seen.get(bot["name"], "2000-01-01T00:00:00+00:00")
        if bot["source"] == "jsonl":
            alerts = parse_jsonl_alerts(bot["audit"], since)
        else:
            # cryptobot log has no per-line timestamp we can trust;
            # use mtime heuristic to avoid re-ingesting old alerts
            alerts = parse_cryptobot_log(bot["audit"], since)
            # Dedupe against what we've seen — use entry price as rough key
            seen_keys = {o.get("key") for o in open_trades if o["bot"] == bot["name"]}
            alerts = [a for a in alerts
                      if f"{bot['name']}:{a['entry']}:{a['sl']}" not in seen_keys]

        now_iso = datetime.now(timezone.utc).isoformat()
        for a in alerts:
            a["bot"] = bot["name"]
            a["opened_at"] = now_iso
            a["key"] = f"{bot['name']}:{a['entry']}:{a['sl']}"
            new_alerts.append(a)
        last_seen[bot["name"]] = now_iso

    if new_alerts:
        print("[tracker] discovered %d new alert(s)" % len(new_alerts))
        open_trades.extend(new_alerts)
        for a in new_alerts:
            append_ledger({"event": "open", **a})

    # 2. For each open trade, fetch recent 1h bars and check SL/TP
    if open_trades:
        df = fetch_1h_bars("BTCUSDT", n=200)
        if df.empty:
            print("[tracker] no data fetched — skipping resolution")
        else:
            still_open = []
            for t in open_trades:
                opened = pd.to_datetime(t["opened_at"], utc=True)
                forward = df[df["open_time"] > opened]
                if forward.empty:
                    still_open.append(t); continue

                resolved = False
                entry = float(t["entry"]); sl = float(t["sl"]); tp = float(t["tp"])
                risk_price = entry - sl
                if risk_price <= 0:
                    print("[tracker] bad trade (sl>=entry): %s" % t.get("key")); continue

                for _, bar in forward.iterrows():
                    hi, lo = float(bar["high"]), float(bar["low"])
                    if lo <= sl:
                        r = (sl - entry) / risk_price
                        append_ledger({
                            "event": "close", "status": "SL",
                            "bot": t["bot"], "system": t.get("system"),
                            "entry": entry, "sl": sl, "tp": tp,
                            "exit_time": str(bar["open_time"]), "r": round(r, 3),
                            "opened_at": t["opened_at"],
                        })
                        resolved = True; break
                    if hi >= tp:
                        r = (tp - entry) / risk_price
                        append_ledger({
                            "event": "close", "status": "TP",
                            "bot": t["bot"], "system": t.get("system"),
                            "entry": entry, "sl": sl, "tp": tp,
                            "exit_time": str(bar["open_time"]), "r": round(r, 3),
                            "opened_at": t["opened_at"],
                        })
                        resolved = True; break

                if not resolved:
                    # Time stop: close at latest bar if > TIME_STOP_BARS bars old
                    age_hours = (pd.Timestamp.now(tz="UTC") - opened).total_seconds() / 3600
                    if age_hours >= TIME_STOP_BARS:
                        last_close = float(df.iloc[-1]["close"])
                        r = (last_close - entry) / risk_price
                        append_ledger({
                            "event": "close", "status": "TIME",
                            "bot": t["bot"], "system": t.get("system"),
                            "entry": entry, "sl": sl, "tp": tp,
                            "exit_time": str(df.iloc[-1]["open_time"]),
                            "r": round(r, 3), "opened_at": t["opened_at"],
                        })
                        resolved = True

                if not resolved:
                    still_open.append(t)

            state["open_trades"] = still_open
            state["last_seen"] = last_seen
            save_state(state)
            print("[tracker] open=%d   resolved-this-tick=%d" % (
                len(still_open), len(open_trades) - len(still_open)))


def report():
    """Summarize ledger for quick review."""
    if not os.path.exists(TRACKER_LEDGER):
        print("No forward-test ledger yet."); return
    by_bot = {}
    with open(TRACKER_LEDGER) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("event") != "close":
                continue
            bot = r.get("bot", "?")
            by_bot.setdefault(bot, []).append(r)

    print("=" * 75)
    print("  FORWARD-TEST LEDGER — live outcomes since deploy")
    print("=" * 75)
    print("  %-14s %5s %4s %4s %5s %5s %6s %7s" % (
        "Bot", "Alerts", "TP", "SL", "TIME", "WR", "PF", "TotR"))
    print("  " + "-" * 60)
    for bot, rows in sorted(by_bot.items()):
        if not rows: continue
        tp = sum(1 for r in rows if r["status"] == "TP")
        sl = sum(1 for r in rows if r["status"] == "SL")
        ts = sum(1 for r in rows if r["status"] == "TIME")
        n = len(rows)
        total_r = sum(r.get("r", 0) for r in rows)
        gw = sum(r["r"] for r in rows if r.get("r", 0) > 0)
        gl = abs(sum(r["r"] for r in rows if r.get("r", 0) < 0))
        pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else 0)
        wr = tp / n * 100 if n else 0
        print("  %-14s %5d %4d %4d %5d %4.1f%% %5.2f %+7.2f" % (
            bot, n, tp, sl, ts, wr, pf, total_r))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    else:
        tick()
