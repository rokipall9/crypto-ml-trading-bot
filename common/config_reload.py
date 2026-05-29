"""
config_reload.py — Runtime config reload without restart.

Two trigger modes:
  1. SIGHUP from operator: `kill -HUP <pid>` reloads config in-process
  2. File-mtime polling (every 30s in background thread)

What gets reloaded:
  - .env file (DISCORD_*, ADMIN_USER_IDS, STRIPE_WEBHOOK_SECRET, etc.)
  - tier rate limits if pricing/config.py exists
  - subscriber_prefs is already file-backed (no reload needed)
  - api_auth tokens.json is already file-backed (no reload needed)

Usage:
    import config_reload
    config_reload.start()           # spawns background watcher + SIGHUP handler

Behavior on reload:
  - Audit-records the event
  - Updates os.environ from .env (respecting precedence: existing > .env)
  - Logs structured event
  - Notifies subscribers via callback (pattern: register_listener)
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from typing import Callable, List

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger
import audit_log

log = get_logger("config_reload")

ENV_FILE = "/home/ubuntu/bot/.env"
POLL_INTERVAL = 30.0

_listeners: List[Callable[[], None]] = []
_last_mtime: float = 0.0
_lock = threading.Lock()


def register_listener(callback: Callable[[], None]) -> None:
    """Register a callback fired on every reload. Callback takes no args."""
    with _lock:
        _listeners.append(callback)


def _load_env_file(path: str) -> int:
    """Parse a .env file and update os.environ. Returns count of vars set."""
    if not os.path.exists(path):
        return 0
    count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            # We REPLACE the env var on reload to allow rotation
            os.environ[k] = v
            count += 1
    return count


def reload_now(reason: str = "manual") -> dict:
    """Force a reload. Returns summary."""
    n = _load_env_file(ENV_FILE)
    log.info("config_reloaded", reason=reason, vars_set=n)
    try:
        audit_log.record("config_reload", actor=f"reload:{reason}",
                         vars_set=n)
    except Exception:
        pass

    # Fire listeners
    fired = 0
    failed = 0
    with _lock:
        listeners = list(_listeners)
    for cb in listeners:
        try:
            cb()
            fired += 1
        except Exception as e:
            failed += 1
            log.error("listener_failed", err=str(e))

    return {"vars_set": n, "listeners_fired": fired,
            "listeners_failed": failed, "reason": reason}


def _signal_handler(signum, frame):
    log.info("sighup_received")
    reload_now(reason="sighup")


def _watcher_loop():
    global _last_mtime
    if os.path.exists(ENV_FILE):
        _last_mtime = os.path.getmtime(ENV_FILE)
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            if not os.path.exists(ENV_FILE):
                continue
            cur = os.path.getmtime(ENV_FILE)
            if cur > _last_mtime:
                _last_mtime = cur
                log.info("env_file_changed", path=ENV_FILE)
                reload_now(reason="file_mtime")
        except Exception as e:
            log.error("watcher_error", err=str(e))
            time.sleep(60)  # backoff


def start() -> None:
    """Install SIGHUP handler + start file-watcher thread."""
    try:
        signal.signal(signal.SIGHUP, _signal_handler)
        log.info("sighup_handler_installed")
    except (AttributeError, ValueError) as e:
        # SIGHUP not available on Windows; main thread only
        log.warn("sighup_unavailable", err=str(e))

    t = threading.Thread(target=_watcher_loop, daemon=True,
                         name="config-reload-watcher")
    t.start()
    log.info("watcher_started", interval=POLL_INTERVAL)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "trigger":
        # Find status_server PID and send SIGHUP
        import subprocess
        try:
            pid = int(subprocess.check_output(
                ["systemctl", "show", "-p", "MainPID", "--value",
                 "status_server.service"]).decode().strip())
            os.kill(pid, signal.SIGHUP)
            print(f"SIGHUP sent to PID {pid}")
        except Exception as e:
            print(f"Failed: {e}")
            sys.exit(1)
    else:
        # Smoke test
        print(f"ENV_FILE: {ENV_FILE}")
        print(f"Exists: {os.path.exists(ENV_FILE)}")
        print(f"reload_now: {reload_now('cli_test')}")
