"""
regression_monitor.py — Detect strategy performance decay.

For each strategy, compare rolling 30-day Sharpe to all-time Sharpe.
Alert if: 30d Sharpe < 0 AND 30d Sharpe < (all_time_Sharpe - 0.5)
       AND at least 5 trades in last 30d.

Throttled: max 1 alert per strategy per 7 days.

Run via systemd timer daily.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import List, Optional

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger
from ledger import closes

log = get_logger("regression_monitor")

STATE_FILE = "/home/ubuntu/common/regression_state.json"
WINDOW_DAYS = 30
MIN_TRADES_30D = 5
SHARPE_DROP_THRESHOLD = 0.5

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass

WEBHOOK = (os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
           or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())


def _sharpe(rs: List[float]) -> Optional[float]:
    n = len(rs)
    if n < 2:
        return None
    m = sum(rs) / n
    var = sum((x - m) ** 2 for x in rs) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 0
    return (m / sd) if sd > 0 else None


def post_alert(title: str, body: str, color: int = 0xFF4444) -> None:
    if not WEBHOOK:
        return
    payload = {
        "username": "🚨 Regression Monitor",
        "embeds": [{
            "title": title,
            "description": body,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Auto-monitored · regression_monitor"},
        }],
    }
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "srs-regression/1.0"})
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:
        log.error("webhook_fail", err=str(e))


def main():
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass

    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    by = defaultdict(list)
    for r in closes():
        sys_ = r.get("system") or "UNKNOWN"
        ts_str = r.get("opened_at") or r.get("exit_time") or ""
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        except Exception:
            ts = None
        rr = float(r.get("r", 0))
        by[sys_].append((ts, rr))

    findings = []
    for strat, trades in by.items():
        all_rs    = [r for _, r in trades]
        recent_rs = [r for ts, r in trades if ts and ts >= cutoff_30d]
        if len(recent_rs) < MIN_TRADES_30D:
            continue
        all_sharpe    = _sharpe(all_rs)
        recent_sharpe = _sharpe(recent_rs)
        if all_sharpe is None or recent_sharpe is None:
            continue
        if (recent_sharpe < 0 and
                recent_sharpe < all_sharpe - SHARPE_DROP_THRESHOLD):
            findings.append({
                "strategy": strat,
                "all_sharpe": round(all_sharpe, 3),
                "recent_sharpe": round(recent_sharpe, 3),
                "n_recent": len(recent_rs),
                "n_all": len(all_rs),
            })
            log.warn("regression_detected", **findings[-1])

    if not findings:
        log.info("all_strategies_healthy")
        return

    last_alerts = state.get("last_alerts", {})
    actionable = []
    new_alerts = dict(last_alerts)
    for f in findings:
        s = f["strategy"]
        last = last_alerts.get(s, 0)
        if time.time() - last > 7 * 86400:
            actionable.append(f)
            new_alerts[s] = time.time()
    state["last_alerts"] = new_alerts

    if actionable:
        lines = [
            f"`{f['strategy']:<20}` "
            f"all-time: **{f['all_sharpe']:+.2f}**  ·  "
            f"30d: **{f['recent_sharpe']:+.2f}**  "
            f"({f['n_recent']} trades)"
            for f in actionable
        ]
        post_alert(
            "📉  Strategy Regression Detected",
            ("Rolling 30-day Sharpe is significantly worse than all-time:\n\n"
             + "\n".join(lines)
             + "\n\n_Investigate strategy decay — consider pausing the "
               "affected strategy._"),
            color=0xFF4444,
        )

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, default=str)


if __name__ == "__main__":
    main()
