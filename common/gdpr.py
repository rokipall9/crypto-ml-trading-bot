"""
gdpr.py — Subscriber data export + deletion (GDPR / CCPA compliance).

For any subscriber who asks for their data ("right to access") or asks
for it to be deleted ("right to erasure"):

  export_subscriber_data(token_prefix) → dict (all data we have on them)
  delete_subscriber_data(token_prefix, dry_run=True)  → list what would change
  delete_subscriber_data(token_prefix, dry_run=False) → actually delete + redact

What's keyed by subscriber:
  - tokens.json          : their token record (revoked on delete)
  - subscriber_prefs.json: their preferences (removed on delete)
  - audit.jsonl          : action records mentioning their token prefix
                           (REDACTED on delete — entry kept, prefix replaced)

What's NOT subscriber-specific (untouched):
  - forward_results.jsonl: global track record, contains no PII
  - bot logs: no subscriber data
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List

sys.path.insert(0, "/home/ubuntu/common")
import api_auth
import audit_log
import subscriber_prefs

TOKENS_FILE = "/home/ubuntu/common/tokens.json"
AUDIT_FILE  = "/home/ubuntu/common/audit.jsonl"
PREFS_FILE  = "/home/ubuntu/common/subscriber_prefs.json"


def _find_token_by_prefix(prefix: str) -> str:
    """Locate the full token whose prefix matches. Returns '' if not found."""
    if not os.path.exists(TOKENS_FILE):
        return ""
    try:
        with open(TOKENS_FILE, encoding="utf-8") as f:
            tokens = json.load(f)
    except Exception:
        return ""
    for tok in tokens:
        if tok.startswith(prefix):
            return tok
    return ""


def export_subscriber_data(token_prefix: str) -> Dict:
    """
    Return all data we hold on this subscriber, keyed by source.
    Does not modify anything. Suitable for serving via /api/v1/me/export.
    """
    full = _find_token_by_prefix(token_prefix)
    out: Dict = {
        "subject_token_prefix": token_prefix,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "found": bool(full),
    }
    if not full:
        return out

    # Token record
    with open(TOKENS_FILE, encoding="utf-8") as f:
        all_tokens = json.load(f)
    rec = all_tokens.get(full, {})
    out["token_record"] = {
        "label": rec.get("label"),
        "tier": rec.get("tier"),
        "issued_at": rec.get("issued_at"),
        "last_used": rec.get("last_used"),
        "uses": rec.get("uses"),
        "expires_at": rec.get("expires_at"),
        "rate_limit_rpm": rec.get("rate_limit_rpm"),
        # Note: actual token string not included in export (already known to subscriber)
    }

    # Preferences (keyed by prefix[:12])
    out["preferences"] = subscriber_prefs.get(full)

    # Audit entries mentioning this prefix
    audit_entries: List[dict] = []
    if os.path.exists(AUDIT_FILE):
        with open(AUDIT_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if (r.get("token_prefix", "").startswith(token_prefix[:8])
                        or r.get("label", "") == rec.get("label", "")):
                    audit_entries.append(r)
    out["audit_entries"] = audit_entries

    return out


def delete_subscriber_data(token_prefix: str,
                           dry_run: bool = True) -> Dict:
    """
    Delete (or list what would be deleted, in dry-run mode):
      1. Revoke token from tokens.json
      2. Remove preferences entry
      3. Redact audit-log entries (replace token_prefix with REDACTED, label with REDACTED)

    Returns a structured report.
    """
    full = _find_token_by_prefix(token_prefix)
    out: Dict = {
        "subject_token_prefix": token_prefix,
        "ts": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(dry_run),
        "found": bool(full),
        "actions": [],
    }
    if not full:
        return out

    # Capture label before any mutation
    with open(TOKENS_FILE, encoding="utf-8") as f:
        rec = json.load(f).get(full, {})
    label = rec.get("label", "")

    # 1. Revoke token
    if dry_run:
        out["actions"].append({"op": "revoke_token", "would_revoke": True})
    else:
        ok = api_auth.revoke_token(full)
        out["actions"].append({"op": "revoke_token", "result": ok})

    # 2. Remove preferences
    if dry_run:
        prefs = subscriber_prefs.get(full)
        out["actions"].append({"op": "delete_prefs", "current": prefs})
    else:
        ok = subscriber_prefs.reset(full)
        out["actions"].append({"op": "delete_prefs", "result": ok})

    # 3. Redact audit log
    redacted_count = 0
    if os.path.exists(AUDIT_FILE):
        rewritten: List[str] = []
        with open(AUDIT_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    rewritten.append(line)
                    continue
                changed = False
                if r.get("token_prefix", "").startswith(token_prefix[:8]):
                    r["token_prefix"] = "REDACTED"
                    changed = True
                if r.get("label") == label and label:
                    r["label"] = "REDACTED"
                    changed = True
                if changed:
                    redacted_count += 1
                    r["redacted_at"] = datetime.now(timezone.utc).isoformat()
                rewritten.append(json.dumps(r, default=str) + "\n")

        if not dry_run and redacted_count > 0:
            tmp = AUDIT_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(rewritten)
            os.replace(tmp, AUDIT_FILE)

        out["actions"].append({"op": "redact_audit",
                               "matches": redacted_count,
                               "redacted": (not dry_run)})

    # 4. Audit the deletion itself (so we have a record that we processed it)
    if not dry_run:
        audit_log.record("gdpr_deletion_processed",
                         actor="gdpr_module",
                         token_prefix=token_prefix,
                         entries_redacted=redacted_count)

    return out


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "export":
        print(json.dumps(export_subscriber_data(sys.argv[2]),
                         indent=2, default=str))
    elif len(sys.argv) >= 3 and sys.argv[1] == "delete":
        dry = "--apply" not in sys.argv
        print(json.dumps(delete_subscriber_data(sys.argv[2], dry_run=dry),
                         indent=2, default=str))
    else:
        print("Usage:")
        print("  gdpr.py export <token_prefix>")
        print("  gdpr.py delete <token_prefix> [--apply]")
