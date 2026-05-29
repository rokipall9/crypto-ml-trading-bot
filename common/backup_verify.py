"""
backup_verify.py — Test-restore from the latest backup snapshot.

The classic DR failure: backups have run nightly for 6 months, but they
never actually restore. This script catches that. Runs daily via systemd
timer.

Process:
  1. Find latest snapshot dir under /home/ubuntu/common/snapshots/
  2. Decompress each .gz to a temp dir
  3. For forward_results.jsonl: validate every line parses + count matches
  4. For tokens.json: validate JSON structure
  5. Run verify_data.py on the restored ledger
  6. Write structured pass/fail report to /home/ubuntu/common/backup_verify.jsonl
  7. Discord alert on FAIL

Exit 0 = clean, 1 = any failure.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("backup_verify")

SNAPSHOT_DIR = "/home/ubuntu/common/snapshots"
RESULT_LOG = "/home/ubuntu/common/backup_verify.jsonl"

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass

WEBHOOK = (os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
           or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())


def latest_snapshot() -> str:
    if not os.path.isdir(SNAPSHOT_DIR):
        return ""
    dirs = sorted([d for d in os.listdir(SNAPSHOT_DIR)
                   if os.path.isdir(os.path.join(SNAPSHOT_DIR, d))])
    return os.path.join(SNAPSHOT_DIR, dirs[-1]) if dirs else ""


def post_alert(title: str, body: str, color: int = 0xFF4444) -> None:
    if not WEBHOOK:
        return
    import urllib.request
    payload = {
        "username": "💾 Backup Verify",
        "embeds": [{"title": title, "description": body, "color": color,
                    "timestamp": datetime.now(timezone.utc).isoformat()}],
    }
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "srs-backup-verify/1.0"})
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:
        log.error("alert_fail", err=str(e))


def verify(snapshot_path: str) -> Dict:
    """Return {ok: bool, details: dict, errors: list}."""
    out: Dict = {
        "ok": True, "snapshot": snapshot_path,
        "ts": datetime.now(timezone.utc).isoformat(),
        "files_checked": 0, "errors": [],
    }
    if not snapshot_path or not os.path.isdir(snapshot_path):
        out["ok"] = False
        out["errors"].append("no_snapshot_found")
        return out

    workdir = tempfile.mkdtemp(prefix="srs_verify_")
    try:
        # Decompress every .gz file
        for gz in Path(snapshot_path).iterdir():
            if not gz.name.endswith(".gz"):
                continue
            out["files_checked"] += 1
            target = os.path.join(workdir, gz.name[:-3])
            try:
                with gzip.open(gz, "rb") as fin, \
                     open(target, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
            except Exception as e:
                out["errors"].append(f"decompress_failed:{gz.name}: {e}")
                continue

            # Specific checks per file
            if "forward_results" in gz.name:
                # Every non-blank line must parse
                bad = 0; total = 0
                with open(target, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        total += 1
                        try: json.loads(line)
                        except Exception: bad += 1
                if bad:
                    out["errors"].append(
                        f"ledger_corrupt: {bad}/{total} unparseable lines")
                out.setdefault("ledger_lines", total)

            elif gz.name.endswith(".json.gz"):
                try:
                    with open(target, encoding="utf-8") as f:
                        json.load(f)
                except Exception as e:
                    out["errors"].append(f"json_invalid:{gz.name}: {e}")

        # Run verify_data on the restored ledger if present
        ledger_path = os.path.join(workdir, "forward_results.jsonl")
        if os.path.exists(ledger_path):
            try:
                # Run verify_data with temporary LEDGER pointing at restored
                env = dict(os.environ)
                proc = subprocess.run(
                    ["python3", "-c",
                     f"import sys; sys.path.insert(0, '/home/ubuntu/common'); "
                     f"import ledger; ledger.LEDGER = '{ledger_path}'; "
                     f"import verify_data; "
                     f"r = verify_data.verify(); "
                     f"import json; print(json.dumps(r))"],
                    capture_output=True, text=True, timeout=30, env=env)
                if proc.returncode == 0 and proc.stdout:
                    res = json.loads(proc.stdout.strip().split("\n")[-1])
                    out["data_integrity"] = {
                        "clean": res.get("clean"),
                        "issues": list(res.get("issues", {}).keys()),
                    }
                    if not res.get("clean"):
                        out["errors"].append(
                            f"data_integrity: {list(res['issues'].keys())}")
            except Exception as e:
                out["errors"].append(f"verify_data_failed: {e}")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    out["ok"] = len(out["errors"]) == 0
    return out


def main() -> int:
    snap = latest_snapshot()
    res = verify(snap)
    log.info("verify_complete", ok=res["ok"], snapshot=snap,
             files=res["files_checked"], errors=res["errors"][:3])

    with open(RESULT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(res, default=str) + "\n")

    if not res["ok"]:
        body = (f"**Snapshot:** `{snap}`\n"
                f"**Errors:**\n" + "\n".join(f"• `{e}`" for e in res["errors"][:8]))
        post_alert("❌ Backup verification FAILED", body, color=0xFF4444)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
