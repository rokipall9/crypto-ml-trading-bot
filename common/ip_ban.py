"""
ip_ban.py — fail2ban-style IP auto-ban for status_server.

Tracks repeated 429 responses per IP. After N rate-limited hits in M minutes,
the IP gets banned for B hours. Bans persisted to disk (atomic).

Default policy: 30 × 429 in 10 min → 1h ban.

Use:
    if ip_ban.is_banned(ip): return 403
    if rate_check_failed: ip_ban.record_429(ip)
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from typing import Dict

BAN_FILE     = "/home/ubuntu/common/ip_bans.json"
THRESHOLD    = 30           # 429s in window → ban
WINDOW       = 600          # 10 min
BAN_DURATION = 3600         # 1 hour

# Whitelist — never ban these (loopback, link-local, RFC1918 internal)
WHITELIST_PREFIXES = ("127.", "10.", "192.168.", "172.16.", "172.17.",
                      "172.18.", "172.19.", "172.20.", "172.21.", "172.22.",
                      "172.23.", "172.24.", "172.25.", "172.26.", "172.27.",
                      "172.28.", "172.29.", "172.30.", "172.31.", "::1")

_429_LOG: Dict[str, deque] = defaultdict(deque)
_BANS: Dict[str, float] = {}
_LOCK = threading.Lock()


def _is_whitelisted(ip: str) -> bool:
    return any(ip.startswith(p) for p in WHITELIST_PREFIXES)


def _load_bans() -> None:
    if not os.path.exists(BAN_FILE):
        return
    try:
        with open(BAN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        with _LOCK:
            for ip, exp in data.items():
                if float(exp) > now:
                    _BANS[ip] = float(exp)
    except Exception:
        pass


def _save_bans() -> None:
    with _LOCK:
        snapshot = dict(_BANS)
    tmp = BAN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f)
    os.replace(tmp, BAN_FILE)


def is_banned(ip: str) -> bool:
    if _is_whitelisted(ip):
        return False
    now = time.time()
    with _LOCK:
        exp = _BANS.get(ip)
        if exp is None:
            return False
        if exp <= now:
            _BANS.pop(ip, None)
            return False
        return True


def record_429(ip: str) -> bool:
    """Record a 429. Returns True if this triggered a new ban."""
    if _is_whitelisted(ip):
        return False
    now = time.time()
    do_save = False
    with _LOCK:
        log = _429_LOG[ip]
        while log and log[0] < now - WINDOW:
            log.popleft()
        log.append(now)
        if len(log) >= THRESHOLD:
            _BANS[ip] = now + BAN_DURATION
            log.clear()
            do_save = True
    if do_save:
        _save_bans()
        return True
    return False


def get_active_bans() -> Dict[str, int]:
    """Return {ip: seconds_remaining}."""
    now = time.time()
    with _LOCK:
        return {ip: int(exp - now)
                for ip, exp in _BANS.items() if exp > now}


def manual_unban(ip: str) -> bool:
    do_save = False
    with _LOCK:
        if ip in _BANS:
            _BANS.pop(ip)
            do_save = True
    if do_save:
        _save_bans()
    return do_save


# Load persisted bans at import
_load_bans()


if __name__ == "__main__":
    bans = get_active_bans()
    if not bans:
        print("(no active bans)")
    else:
        for ip, secs in bans.items():
            print(f"{ip}  unban in {secs//60}m{secs%60}s")
