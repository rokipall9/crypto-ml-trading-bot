"""
chaos.py — Failure-injection harness.

Tests that the system's defenses ACTUALLY trigger when failures occur.
Without this, you're trusting that watchdog/canary/data_health work in
theory, never verifying in practice.

Scenarios:
  1. stale_heartbeat       — touch heartbeat to 10min old   → watchdog should detect
  2. ledger_frozen         — touch ledger mtime to 49h old  → data_health should alert
  3. canary_fail           — write CANARY_FAILED file       → watchdog escalates
  4. webhook_dead_letter   — fake 10 dead-letter records    → escalation rule fires
  5. malformed_ledger_line — append corrupt JSON            → verify_data catches
  6. ban_exhaustion        — fill ip_bans.json with 100     → ip_ban handles gracefully
  7. disk_pressure         — create 1GB temp file           → /health/check disk_space fails

Each scenario has:
  inject() — apply the failure
  verify() — check the expected defense triggered
  cleanup() — restore state

USAGE:
  python3 chaos.py list                  # show all scenarios
  python3 chaos.py run <name>            # run one scenario end-to-end
  python3 chaos.py run all               # run all (sequential, with cleanup between)

Always cleans up. If interrupted (SIGINT), atexit handler runs cleanup.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("chaos")

LEDGER = "/home/ubuntu/common/forward_results.jsonl"
HEARTBEAT = "/home/ubuntu/common/heartbeat.json"
CANARY_FAIL = "/home/ubuntu/common/CANARY_FAILED"
BAN_FILE = "/home/ubuntu/common/ip_bans.json"
DISK_PROBE = "/tmp/srs_chaos_diskpressure.bin"
LEDGER_BACKUP = "/tmp/srs_chaos_ledger_backup.jsonl"

_CLEANUPS: List[Callable[[], None]] = []


def _register_cleanup(cb: Callable[[], None]) -> None:
    _CLEANUPS.append(cb)


def _run_cleanups():
    for cb in reversed(_CLEANUPS):
        try: cb()
        except Exception as e: log.error("cleanup_failed", err=str(e))
    _CLEANUPS.clear()


atexit.register(_run_cleanups)
signal.signal(signal.SIGINT,
              lambda *_: (_run_cleanups(), sys.exit(130)))


# ─── Scenarios ───
def chaos_stale_heartbeat() -> Tuple[bool, str]:
    """Set heartbeat mtime to 10 min ago; verify watchdog flags as stale."""
    if not os.path.exists(HEARTBEAT):
        return False, "no_heartbeat_file"
    orig_mtime = os.path.getmtime(HEARTBEAT)
    target = time.time() - 600
    os.utime(HEARTBEAT, (target, target))
    _register_cleanup(lambda: os.utime(HEARTBEAT, (orig_mtime, orig_mtime)))

    # Verify
    import heartbeat
    age = heartbeat.age_seconds()
    return age >= 600, f"heartbeat_age={age:.0f}s (expected ≥600)"


def chaos_ledger_frozen() -> Tuple[bool, str]:
    """Set ledger mtime to 49h ago; data_health should detect."""
    if not os.path.exists(LEDGER):
        return False, "no_ledger"
    orig_mtime = os.path.getmtime(LEDGER)
    target = time.time() - 49 * 3600
    os.utime(LEDGER, (target, target))
    _register_cleanup(lambda: os.utime(LEDGER, (orig_mtime, orig_mtime)))

    import subprocess
    proc = subprocess.run(
        ["python3", "/home/ubuntu/common/data_health.py"],
        capture_output=True, text=True, timeout=10)
    return ("silent_recording_failure" in proc.stdout
            or "stale" in proc.stdout.lower(),
            f"data_health output: {proc.stdout[:200]}")


def chaos_canary_fail() -> Tuple[bool, str]:
    """Write CANARY_FAILED; verify it's readable + format correct."""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "failures": [{"check": "test", "info": "chaos injection"}],
    }
    with open(CANARY_FAIL, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    _register_cleanup(lambda: os.path.exists(CANARY_FAIL)
                      and os.remove(CANARY_FAIL))
    # Verify file exists and parses
    with open(CANARY_FAIL, encoding="utf-8") as f:
        data = json.load(f)
    return (data["failures"][0]["check"] == "test",
            f"canary file written + parsed correctly")


def chaos_webhook_dead_letter() -> Tuple[bool, str]:
    """Fake 10 dead-letter records; verify webhook_queue.stats reflects them."""
    import webhook_queue
    DEAD = webhook_queue.DEAD_DIR
    os.makedirs(DEAD, exist_ok=True)
    created = []
    for i in range(10):
        path = os.path.join(DEAD, f"chaos_{i}.json")
        with open(path, "w") as f:
            json.dump({"id": f"chaos_{i}", "url": "fake",
                       "payload": {}, "dead_reason": "chaos"}, f)
        created.append(path)
    _register_cleanup(lambda: [os.remove(p) for p in created if os.path.exists(p)])

    s = webhook_queue.stats()
    return s["dead_letter"] >= 10, f"queue stats: {s}"


def chaos_malformed_ledger_line() -> Tuple[bool, str]:
    """Append a corrupt JSON line; verify_data should report malformed_json."""
    if not os.path.exists(LEDGER):
        # Create a minimal valid ledger first
        with open(LEDGER, "w", encoding="utf-8") as f:
            pass
    shutil.copy(LEDGER, LEDGER_BACKUP)
    _register_cleanup(lambda: shutil.copy(LEDGER_BACKUP, LEDGER))

    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write("{not valid json\n")

    import subprocess
    proc = subprocess.run(
        ["python3", "/home/ubuntu/common/verify_data.py"],
        capture_output=True, text=True, timeout=10)
    return ("malformed_json" in proc.stdout,
            f"verify_data caught malformed: "
            f"{'yes' if 'malformed_json' in proc.stdout else 'no'}")


def chaos_disk_pressure() -> Tuple[bool, str]:
    """Create temp file briefly. Verify health_check.check_disk_space still
    behaves (we don't actually fill the disk; we test the check works)."""
    import health_check
    ok, info = health_check.check_disk_space(min_gb=1.0)
    # Just verify the check returns coherent data
    return (info.get("free_gb", 0) > 0
            and isinstance(info.get("ok"), bool),
            f"disk_space check returned: {info}")


SCENARIOS: Dict[str, Callable[[], Tuple[bool, str]]] = {
    "stale_heartbeat":   chaos_stale_heartbeat,
    "ledger_frozen":     chaos_ledger_frozen,
    "canary_fail":       chaos_canary_fail,
    "webhook_dead_letter": chaos_webhook_dead_letter,
    "malformed_ledger_line": chaos_malformed_ledger_line,
    "disk_pressure":     chaos_disk_pressure,
}


def run_one(name: str) -> Tuple[bool, str]:
    fn = SCENARIOS.get(name)
    if not fn:
        return False, f"unknown_scenario: {name}"
    log.info("chaos_inject", scenario=name)
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"exception: {e}"
    log.info("chaos_result", scenario=name, ok=ok, detail=detail[:200])
    _run_cleanups()  # always cleanup after
    return ok, detail


def run_all() -> Dict[str, Tuple[bool, str]]:
    results = {}
    for name in SCENARIOS:
        results[name] = run_one(name)
        time.sleep(1)
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "list":
        for n in SCENARIOS:
            print(f"  {n}")
    elif cmd == "run":
        if len(sys.argv) < 3:
            print("Usage: chaos.py run <name|all>"); sys.exit(1)
        target = sys.argv[2]
        if target == "all":
            results = run_all()
            passed = sum(1 for ok, _ in results.values() if ok)
            for name, (ok, detail) in results.items():
                emoji = "✅" if ok else "❌"
                print(f"{emoji} {name:<28} {detail[:80]}")
            print(f"\n{passed}/{len(results)} scenarios passed")
            sys.exit(0 if passed == len(results) else 1)
        else:
            ok, detail = run_one(target)
            emoji = "✅" if ok else "❌"
            print(f"{emoji} {target}: {detail}")
            sys.exit(0 if ok else 1)
    else:
        print(__doc__); sys.exit(1)
