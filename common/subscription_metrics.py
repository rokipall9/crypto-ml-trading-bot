"""
subscription_metrics.py — Operator BI for the subscription business.

Single endpoint giving you the numbers you actually want to see daily:
  - Active subscribers (by tier)
  - Trial conversion candidates (expiring in <3 days)
  - Recent issuances + revocations (last 30d)
  - Average uses per token (engagement)
  - Custom rate-limit overrides count
  - Revenue estimate (if PRICING configured)

Used by /api/admin/subscriptions and the operator dashboard.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict

sys.path.insert(0, "/home/ubuntu/common")

TOKENS_FILE = "/home/ubuntu/common/tokens.json"
AUDIT_FILE  = "/home/ubuntu/common/audit.jsonl"

# Optional pricing config — adjust to your plans, set in env or here
TIER_PRICING_USD = {
    1: float(os.environ.get("PRICE_TIER_1_USD", "29")),
    2: float(os.environ.get("PRICE_TIER_2_USD", "99")),
}


def _load_tokens() -> Dict[str, dict]:
    if not os.path.exists(TOKENS_FILE):
        return {}
    try:
        with open(TOKENS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _parse(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _is_expired(rec: dict) -> bool:
    exp = _parse(rec.get("expires_at"))
    return exp is not None and exp < datetime.now(timezone.utc)


def compute() -> Dict:
    tokens = _load_tokens()
    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)
    cutoff_3d = now + timedelta(days=3)

    by_tier: Counter = Counter()
    expiring_soon = 0
    expired_count = 0
    custom_rpm_count = 0
    use_counts = []
    last_used_recent = 0

    for tok, rec in tokens.items():
        tier = rec.get("tier", 0)
        if _is_expired(rec):
            expired_count += 1
            continue   # don't count expired in active
        by_tier[tier] += 1
        exp = _parse(rec.get("expires_at"))
        if exp is not None and exp <= cutoff_3d:
            expiring_soon += 1
        if rec.get("rate_limit_rpm"):
            custom_rpm_count += 1
        use_counts.append(int(rec.get("uses", 0)))
        last = _parse(rec.get("last_used"))
        if last and last >= cutoff_30d:
            last_used_recent += 1

    # MRR estimate
    mrr_usd = sum(TIER_PRICING_USD.get(t, 0) * c for t, c in by_tier.items())

    # Recent issuances + revocations from audit log
    issued_30d = 0
    revoked_30d = 0
    if os.path.exists(AUDIT_FILE):
        with open(AUDIT_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                ts = _parse(r.get("ts"))
                if ts is None or ts < cutoff_30d:
                    continue
                action = r.get("action", "")
                if action == "token_issue":
                    issued_30d += 1
                elif action == "token_revoke":
                    revoked_30d += 1

    avg_uses = round(sum(use_counts) / len(use_counts), 1) if use_counts else 0

    return {
        "ts": now.isoformat(),
        "active_total": sum(by_tier.values()),
        "by_tier": {str(k): v for k, v in by_tier.items()},
        "expired_total": expired_count,
        "expiring_within_3d": expiring_soon,
        "custom_rate_limit_count": custom_rpm_count,
        "avg_uses_per_token": avg_uses,
        "active_in_last_30d": last_used_recent,
        "issuances_30d": issued_30d,
        "revocations_30d": revoked_30d,
        "net_growth_30d": issued_30d - revoked_30d,
        "mrr_usd_estimate": round(mrr_usd, 2),
        "pricing_config": {str(k): v for k, v in TIER_PRICING_USD.items()},
    }


if __name__ == "__main__":
    print(json.dumps(compute(), indent=2, default=str))
