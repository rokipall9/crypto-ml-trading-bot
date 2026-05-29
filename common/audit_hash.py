"""audit_hash.py — SHA256 of all audit logs + ledger, posted daily for transparency.

Subscribers can verify integrity over time:
- Compare today's hash to yesterday's: if old entries change, it's tampered.
- Build trust through provable immutability.
"""
from __future__ import annotations
import hashlib
import json
import os
import sys
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

FILES_TO_HASH = [
    ("daily_signal audit", "/home/ubuntu/daily_signal/state/audit.jsonl"),
    ("pro_signal audit",   "/home/ubuntu/pro_signal/state/audit.jsonl"),
    ("forward ledger",     "/home/ubuntu/common/forward_results.jsonl"),
    ("paper trades",       "/home/ubuntu/bot/logs/paper_trades.json"),
]


def hash_file(path: str) -> str:
    if not os.path.exists(path):
        return "—"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def file_size(path: str) -> str:
    if not os.path.exists(path):
        return "—"
    sz = os.path.getsize(path)
    if sz < 1024:
        return f"{sz}B"
    if sz < 1024**2:
        return f"{sz/1024:.1f}KB"
    return f"{sz/1024**2:.1f}MB"


def main():
    if not WATCH_URL:
        print("[audit_hash] no webhook"); return

    rows = []
    for label, path in FILES_TO_HASH:
        h = hash_file(path)
        sz = file_size(path)
        # Truncate hash for readability — full hash in description
        short = h[:16] + "…" if h != "—" else "—"
        rows.append(f"  {label:<20} {sz:>8}   `{short}`")

    body = "```\n" + "\n".join(rows) + "\n```"

    # Compose all-data combined hash (for spot-verify)
    combined = hashlib.sha256()
    for _, path in FILES_TO_HASH:
        if os.path.exists(path):
            with open(path, "rb") as f:
                combined.update(f.read())
    combined_hash = combined.hexdigest()

    embed = {
        "title": "🔐  Audit Hash · Transparency Snapshot",
        "description": ("_Daily SHA256 of all audit logs + ledger. "
                        "Compare day-over-day to verify nothing was retroactively edited._"),
        "color": 0x5865F2,
        "fields": [
            {"name": "📋  File Hashes (truncated)", "value": body, "inline": False},
            {"name": "🔑  Combined SHA256",
             "value": f"```{combined_hash}```",
             "inline": False},
        ],
        "footer": {"text": "Audit hash · daily 00:00 UTC · provable integrity"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    pro_format.send_embed(WATCH_URL, embed, username="🔐 Audit Hash")
    print(f"[audit_hash] posted (combined: {combined_hash[:16]}...)")


if __name__ == "__main__":
    main()
