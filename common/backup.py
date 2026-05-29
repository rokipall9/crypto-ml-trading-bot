"""
backup.py — Daily snapshot of critical state with optional off-site upload.

Always:
  - Gzip-snapshots forward_results.jsonl + tokens.json + bankroll_store.json
    to /home/ubuntu/common/snapshots/YYYYMMDD/
  - Prunes snapshots >30 days old.

If env vars are set, also uploads to S3-compatible storage:
  BACKUP_S3_ENDPOINT     (e.g. https://s3.us-west-002.backblazeb2.com)
  BACKUP_S3_BUCKET
  BACKUP_S3_ACCESS_KEY
  BACKUP_S3_SECRET_KEY

Recommend Backblaze B2 (free tier: 10GB storage, 1GB/day egress).
"""
from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("backup")

LOCAL_SNAPSHOT_DIR = "/home/ubuntu/common/snapshots"
BACKUP_FILES = [
    "/home/ubuntu/common/forward_results.jsonl",
    "/home/ubuntu/common/tokens.json",
    "/home/ubuntu/common/bankroll_store.json",
    "/home/ubuntu/common/dm_subscribers.json",
]
RETAIN_DAYS = 30


def _gzip_copy(src: str, dst: str) -> int:
    if not os.path.exists(src):
        return 0
    with open(src, "rb") as fin, gzip.open(dst, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout)
    return os.path.getsize(dst)


def make_local_snapshot() -> str:
    """Create today's snapshot dir with gzipped copies of all critical files."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_dir = os.path.join(LOCAL_SNAPSHOT_DIR, stamp)
    os.makedirs(out_dir, exist_ok=True)
    total = 0
    for src in BACKUP_FILES:
        if not os.path.exists(src):
            continue
        name = os.path.basename(src) + ".gz"
        dst = os.path.join(out_dir, name)
        size = _gzip_copy(src, dst)
        if size:
            log.info("snapshot_file", src=src, dst=dst, size=size)
            total += size
    log.info("snapshot_done", dir=out_dir, total_bytes=total)
    return out_dir


def prune_local() -> int:
    if not os.path.isdir(LOCAL_SNAPSHOT_DIR):
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - RETAIN_DAYS * 86400
    removed = 0
    for d in Path(LOCAL_SNAPSHOT_DIR).iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def upload_s3(local_dir: str) -> bool:
    endpoint = os.environ.get("BACKUP_S3_ENDPOINT", "").strip()
    bucket   = os.environ.get("BACKUP_S3_BUCKET", "").strip()
    access   = os.environ.get("BACKUP_S3_ACCESS_KEY", "").strip()
    secret   = os.environ.get("BACKUP_S3_SECRET_KEY", "").strip()
    if not (endpoint and bucket and access and secret):
        log.info("s3_not_configured")
        return False
    env = dict(os.environ)
    env["AWS_ACCESS_KEY_ID"]     = access
    env["AWS_SECRET_ACCESS_KEY"] = secret
    stamp = os.path.basename(local_dir)
    try:
        result = subprocess.run(
            ["aws", "s3", "sync", local_dir,
             f"s3://{bucket}/srs/{stamp}/",
             "--endpoint-url", endpoint],
            capture_output=True, timeout=120, text=True, env=env,
        )
        if result.returncode == 0:
            log.info("s3_upload_ok", dir=local_dir)
            return True
        log.error("s3_upload_fail", stderr=result.stderr[:500])
        return False
    except FileNotFoundError:
        log.warn("aws_cli_not_installed",
                 install="sudo pip3 install awscli")
        return False
    except Exception as e:
        log.error("s3_upload_exception", err=str(e))
        return False


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv("/home/ubuntu/bot/.env")
    except Exception:
        pass

    out_dir = make_local_snapshot()
    pruned = prune_local()
    if pruned:
        log.info("pruned_old", count=pruned)
    upload_s3(out_dir)


if __name__ == "__main__":
    main()
