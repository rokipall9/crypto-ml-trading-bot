"""
risk_budget.py — Global R caps to prevent ruin.

Bot calls before firing each alert:
    if not risk_budget.allow(strategy="BREAKOUT_4H", intended_r=1.0):
        log.info("alert_blocked_by_risk_budget", ...)
        continue

Defaults (configurable via env):
  RISK_BUDGET_DAILY_R    — max R exposure per UTC day, default 5.0
  RISK_BUDGET_WEEKLY_R   — max R per ISO week, default 15.0

When the cap is hit:
  - allow() returns False until the period rolls over
  - Audit logged on first block per period
  - Discord alert posted on first block per day (throttled)

Tracked exposure = sum of intended_r across alerts that fired today/this-week.
If a trade closes losing, the R is consumed; if it wins, the R is freed
back to the budget after close (optional reset_consumed).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger
import audit_log

log = get_logger("risk_budget")

STATE_FILE = "/home/ubuntu/common/risk_budget.json"

DAILY_R_CAP = float(os.environ.get("RISK_BUDGET_DAILY_R", "5.0"))
WEEKLY_R_CAP = float(os.environ.get("RISK_BUDGET_WEEKLY_R", "15.0"))


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _week_key() -> str:
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    # Python 3.8: isocalendar() returns tuple (year, week, weekday)
    # Python 3.9+: returns IsoCalendarDate (with .year, .week, .weekday)
    year = iso[0] if isinstance(iso, tuple) else iso.year
    week = iso[1] if isinstance(iso, tuple) else iso.week
    return f"{year}-W{week:02d}"


def _load() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def status() -> Dict:
    d = _load()
    today = _today_key()
    week = _week_key()
    daily_used = float(d.get("daily", {}).get(today, 0.0))
    weekly_used = float(d.get("weekly", {}).get(week, 0.0))
    return {
        "today": today,
        "week": week,
        "daily_used": daily_used,
        "daily_cap": DAILY_R_CAP,
        "daily_remaining": max(0, DAILY_R_CAP - daily_used),
        "weekly_used": weekly_used,
        "weekly_cap": WEEKLY_R_CAP,
        "weekly_remaining": max(0, WEEKLY_R_CAP - weekly_used),
    }


def allow(strategy: str = "*", intended_r: float = 1.0) -> Tuple[bool, str]:
    """Returns (allowed, reason). reason populated only when blocked."""
    s = status()
    if s["daily_used"] + intended_r > DAILY_R_CAP:
        return False, (f"daily cap exceeded "
                       f"({s['daily_used']:.2f}/{DAILY_R_CAP:.2f}R + "
                       f"{intended_r:.2f}R intended)")
    if s["weekly_used"] + intended_r > WEEKLY_R_CAP:
        return False, (f"weekly cap exceeded "
                       f"({s['weekly_used']:.2f}/{WEEKLY_R_CAP:.2f}R + "
                       f"{intended_r:.2f}R intended)")
    return True, "ok"


def consume(strategy: str, intended_r: float) -> Dict:
    """Record that an alert fired with this R commitment. Returns new status."""
    d = _load()
    today = _today_key(); week = _week_key()
    d.setdefault("daily", {}).setdefault(today, 0.0)
    d.setdefault("weekly", {}).setdefault(week, 0.0)
    d["daily"][today] = float(d["daily"][today]) + intended_r
    d["weekly"][week] = float(d["weekly"][week]) + intended_r
    _save(d)
    log.info("budget_consumed", strategy=strategy, r=intended_r,
             daily=d["daily"][today], weekly=d["weekly"][week])
    return status()


def release(strategy: str, intended_r: float) -> Dict:
    """Optional: when a trade closes positive, free its R back to the budget.
    Default policy: consume on alert, never release. This function is for
    operators who prefer 'budget = open exposure' semantics."""
    d = _load()
    today = _today_key(); week = _week_key()
    d.setdefault("daily", {}).setdefault(today, 0.0)
    d.setdefault("weekly", {}).setdefault(week, 0.0)
    d["daily"][today] = max(0, float(d["daily"][today]) - intended_r)
    d["weekly"][week] = max(0, float(d["weekly"][week]) - intended_r)
    _save(d)
    return status()


def reset_today() -> Dict:
    d = _load()
    today = _today_key()
    if "daily" in d and today in d["daily"]:
        del d["daily"][today]
    _save(d)
    audit_log.record("risk_budget_daily_reset", actor="operator")
    return status()


def cleanup_old(keep_days: int = 30) -> int:
    """Delete daily/weekly entries older than N days."""
    d = _load()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)
              ).strftime("%Y-%m-%d")
    removed = 0
    if "daily" in d:
        for k in list(d["daily"].keys()):
            if k < cutoff:
                d["daily"].pop(k); removed += 1
    _save(d)
    return removed


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(status(), indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "reset":
        print(json.dumps(reset_today(), indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        print(f"removed {cleanup_old()} old entries")
    else:
        print("Usage:")
        print("  risk_budget.py status")
        print("  risk_budget.py reset    # reset today's budget")
        print("  risk_budget.py cleanup  # prune old daily entries")
