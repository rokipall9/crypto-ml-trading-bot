"""
circuit_breaker.py — Auto-disable strategies on drawdown.

Bot calls before each scan:
    if circuit_breaker.is_disabled("BREAKOUT_4H"):
        log.info("strategy_disabled_by_breaker", strategy="BREAKOUT_4H")
        continue

Auto-disable triggers (any one fires):
  - Rolling 7-day R < -3.0 (3R drawdown in a week)
  - Last 5 closes all SL (consecutive-losses limit)
  - Last 10 closes worse than -1R total

Auto-re-enable triggers:
  - 24h grace period after auto-disable, then re-evaluate
  - 3 consecutive wins in shadow_mode (manual reactivation)

Manual override:
  - circuit_breaker.disable_manually(strategy, reason)
  - circuit_breaker.enable_manually(strategy)

State persisted to /home/ubuntu/common/circuit_breaker.json (atomic).
Audited via audit_log.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger
from ledger import closes
import audit_log

log = get_logger("circuit_breaker")

STATE_FILE = "/home/ubuntu/common/circuit_breaker.json"
ROLLING_DAYS = 7
ROLLING_R_THRESHOLD = -3.0
CONSECUTIVE_SL_LIMIT = 5
LAST_N_TOTAL_R = -1.0
GRACE_PERIOD_SEC = 24 * 3600


def _load() -> Dict[str, dict]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: Dict[str, dict]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def _strategy_metrics(strategy: str) -> Tuple[float, int, float]:
    """Returns (rolling_7d_r, consecutive_sl, last_10_total_r)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ROLLING_DAYS)
    rolling_r = 0.0
    last_results = []
    for r in closes():
        if r.get("system") != strategy:
            continue
        ts_str = r.get("exit_time") or r.get("opened_at") or ""
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        except Exception:
            continue
        last_results.append((ts, r.get("status"), float(r.get("r", 0))))
        if ts >= cutoff:
            rolling_r += float(r.get("r", 0))
    last_results.sort()

    # consecutive_sl from end
    consec = 0
    for _, status, _ in reversed(last_results):
        if status == "SL":
            consec += 1
        else:
            break

    # last 10 total R
    last10 = last_results[-10:]
    last10_r = sum(rr for _, _, rr in last10)

    return rolling_r, consec, last10_r


def evaluate(strategy: str) -> Optional[str]:
    """Check whether strategy should auto-disable. Returns reason or None."""
    rolling_r, consec, last10_r = _strategy_metrics(strategy)
    if rolling_r < ROLLING_R_THRESHOLD:
        return f"rolling_7d_r={rolling_r:.2f} below {ROLLING_R_THRESHOLD}"
    if consec >= CONSECUTIVE_SL_LIMIT:
        return f"{consec} consecutive SL losses"
    if len(_get_last_n(strategy, 10)) >= 10 and last10_r < LAST_N_TOTAL_R:
        return f"last_10_total_r={last10_r:.2f} below {LAST_N_TOTAL_R}"
    return None


def _get_last_n(strategy: str, n: int) -> list:
    out = [r for r in closes() if r.get("system") == strategy]
    return out[-n:]


def is_disabled(strategy: str) -> bool:
    """Returns True if strategy is currently disabled (manually or by breaker)."""
    d = _load()
    rec = d.get(strategy)
    if not rec:
        return False
    # Check expiration of auto-disable
    if rec.get("expires_at"):
        try:
            exp = datetime.fromisoformat(
                str(rec["expires_at"]).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > exp:
                # Grace period elapsed; remove and re-evaluate fresh
                d.pop(strategy)
                _save(d)
                return False
        except Exception:
            pass
    return True


def check_and_apply(strategy: str) -> Optional[str]:
    """Called by bot before each scan. If breaker should trip, mark
    disabled and return reason; else None."""
    if is_disabled(strategy):
        return _load().get(strategy, {}).get("reason", "previously_disabled")
    reason = evaluate(strategy)
    if reason:
        d = _load()
        exp = (datetime.now(timezone.utc)
               + timedelta(seconds=GRACE_PERIOD_SEC)).isoformat()
        d[strategy] = {
            "reason": reason, "tripped_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": exp, "trigger": "auto",
        }
        _save(d)
        log.warn("circuit_breaker_tripped", strategy=strategy, reason=reason)
        try:
            audit_log.record("circuit_breaker_auto_disable",
                             actor="circuit_breaker",
                             strategy=strategy, reason=reason)
        except Exception:
            pass
        return reason
    return None


def disable_manually(strategy: str, reason: str = "operator manual") -> dict:
    d = _load()
    d[strategy] = {
        "reason": reason, "tripped_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": None, "trigger": "manual",
    }
    _save(d)
    log.info("manual_disable", strategy=strategy, reason=reason)
    try:
        audit_log.record("circuit_breaker_manual_disable",
                         actor="operator", strategy=strategy, reason=reason)
    except Exception:
        pass
    return d[strategy]


def enable_manually(strategy: str) -> bool:
    d = _load()
    if strategy in d:
        d.pop(strategy)
        _save(d)
        log.info("manual_enable", strategy=strategy)
        try:
            audit_log.record("circuit_breaker_manual_enable",
                             actor="operator", strategy=strategy)
        except Exception:
            pass
        return True
    return False


def list_disabled() -> Dict[str, dict]:
    return _load()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        print(json.dumps(list_disabled(), indent=2, default=str))
    elif len(sys.argv) >= 3 and sys.argv[1] == "evaluate":
        reason = evaluate(sys.argv[2])
        print(f"strategy={sys.argv[2]}: " +
              (f"would disable: {reason}" if reason else "OK"))
    elif len(sys.argv) >= 3 and sys.argv[1] == "disable":
        print(json.dumps(disable_manually(sys.argv[2],
                                           " ".join(sys.argv[3:]) or "manual"),
                         indent=2, default=str))
    elif len(sys.argv) >= 3 and sys.argv[1] == "enable":
        print(f"enabled: {enable_manually(sys.argv[2])}")
    else:
        print("Usage:")
        print("  circuit_breaker.py list")
        print("  circuit_breaker.py evaluate <strategy>")
        print("  circuit_breaker.py disable <strategy> [reason]")
        print("  circuit_breaker.py enable <strategy>")
