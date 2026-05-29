"""
subscriber_lifecycle.py — Self-serve token issuance via payment webhook.

When Stripe/Lemon Squeezy/Paddle confirms a successful payment, the
provider's webhook hits POST /api/webhooks/<provider>. We validate the
signature, issue a tier-1 token, audit the event, and notify the
operator's admin Discord channel.

Provider-agnostic: signature validators are pluggable. Stripe and
Lemon Squeezy are included as reference implementations.

Integration:
  status_server.py routes POST /api/webhooks/<provider> here.
  Set provider secrets in /home/ubuntu/bot/.env:
    STRIPE_WEBHOOK_SECRET=whsec_...
    LEMON_WEBHOOK_SECRET=...
    DISCORD_ADMIN_WEBHOOK=https://discord.com/api/webhooks/...
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from typing import Dict, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")

import api_auth
import audit_log
import webhook_queue
from srs_logger import get_logger

log = get_logger("subscriber_lifecycle")


# ─── Signature validators ───
def validate_stripe(payload: bytes, sig_header: str,
                    secret: str) -> bool:
    """Stripe sig: t=TS,v1=SIG. HMAC-SHA256 of '<ts>.<payload>'."""
    if not sig_header or not secret:
        return False
    parts = {}
    for piece in sig_header.split(","):
        if "=" in piece:
            k, v = piece.split("=", 1)
            parts[k.strip()] = v.strip()
    ts = parts.get("t"); sig = parts.get("v1")
    if not ts or not sig:
        return False
    msg = f"{ts}.{payload.decode('utf-8', errors='replace')}".encode()
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def validate_lemon_squeezy(payload: bytes, sig_header: str,
                           secret: str) -> bool:
    """Lemon Squeezy: raw HMAC-SHA256 of payload. Header is hex digest."""
    if not sig_header or not secret:
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header.strip())


def validate_generic(payload: bytes, sig_header: str,
                     secret: str) -> bool:
    """Generic HMAC-SHA256 of body, hex header. For custom integrations."""
    return validate_lemon_squeezy(payload, sig_header, secret)


VALIDATORS = {
    "stripe": validate_stripe,
    "lemon":  validate_lemon_squeezy,
    "generic": validate_generic,
}


# ─── Provider-specific event extractors ───
def extract_stripe(payload: dict) -> Tuple[Optional[str], Optional[str]]:
    """Returns (event_type, customer_email) or (None, None)."""
    et = payload.get("type", "")
    obj = payload.get("data", {}).get("object", {})
    email = (obj.get("customer_email")
             or obj.get("receipt_email")
             or obj.get("billing_details", {}).get("email"))
    return (et if et else None, email)


def extract_lemon(payload: dict) -> Tuple[Optional[str], Optional[str]]:
    meta = payload.get("meta", {})
    et = meta.get("event_name")
    data = payload.get("data", {}).get("attributes", {})
    email = data.get("user_email")
    return (et, email)


EXTRACTORS = {
    "stripe": extract_stripe,
    "lemon":  extract_lemon,
}


# ─── Onboarding flow ───
PAYMENT_SUCCESS_EVENTS = {
    "stripe": {"checkout.session.completed", "payment_intent.succeeded",
               "invoice.paid"},
    "lemon":  {"order_created", "subscription_created"},
    "generic": set(),  # caller decides
}


def issue_subscriber(label: str, source: str = "manual") -> Dict[str, str]:
    """Issue a tier-1 token. Returns {token, label, source}."""
    token = api_auth.issue_token(label=label, tier=1)
    audit_log.record("subscriber_onboarded",
                     actor=f"webhook:{source}",
                     label=label, token_prefix=token[:8])
    log.info("subscriber_onboarded", label=label, source=source,
             token_prefix=token[:8])
    return {"token": token, "label": label, "source": source}


def notify_admin(token: str, label: str, source: str) -> None:
    """Post to admin Discord channel via durable queue."""
    admin_webhook = os.environ.get("DISCORD_ADMIN_WEBHOOK", "").strip()
    if not admin_webhook:
        admin_webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not admin_webhook:
        log.warn("no_admin_webhook_configured")
        return
    payload = {
        "username": "💰 Payments",
        "embeds": [{
            "title": "✅ New Subscriber Onboarded",
            "description": (f"**Source:** `{source}`\n"
                            f"**Label:** `{label}`\n"
                            f"**Token (prefix):** `{token[:8]}...`\n\n"
                            f"_Full token retrievable via `/token list`. "
                            f"Send to subscriber via your usual channel._"),
            "color": 0x00C851,
        }],
    }
    webhook_queue.enqueue(admin_webhook, payload)


def handle_webhook(provider: str, body: bytes,
                   sig_header: str) -> Tuple[int, Dict]:
    """Main entry. Returns (http_status, response_body)."""
    provider = provider.lower()
    secret_env = {
        "stripe": "STRIPE_WEBHOOK_SECRET",
        "lemon":  "LEMON_WEBHOOK_SECRET",
        "generic": "GENERIC_WEBHOOK_SECRET",
    }.get(provider)
    if not secret_env:
        return 404, {"error": "unknown_provider"}

    secret = os.environ.get(secret_env, "").strip()
    if not secret:
        log.warn("provider_secret_missing", provider=provider, env=secret_env)
        return 500, {"error": "provider_not_configured"}

    validator = VALIDATORS.get(provider)
    if not validator(body, sig_header, secret):
        log.warn("signature_mismatch", provider=provider)
        audit_log.record("webhook_signature_failed",
                         actor=f"webhook:{provider}")
        return 401, {"error": "invalid_signature"}

    try:
        payload = json.loads(body)
    except Exception as e:
        return 400, {"error": "bad_json", "detail": str(e)}

    extractor = EXTRACTORS.get(provider)
    et, email = extractor(payload) if extractor else (None, None)

    if et not in PAYMENT_SUCCESS_EVENTS.get(provider, set()):
        log.info("event_ignored", provider=provider, event=et)
        return 200, {"status": "ignored", "event": et}

    label = f"{provider}:{email}" if email else f"{provider}:unknown"
    rec = issue_subscriber(label=label, source=provider)
    notify_admin(rec["token"], rec["label"], rec["source"])
    return 200, {"status": "issued", "token_prefix": rec["token"][:8]}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "manual":
        if len(sys.argv) < 3:
            print("Usage: subscriber_lifecycle.py manual <label>")
            sys.exit(1)
        label = sys.argv[2]
        rec = issue_subscriber(label=label, source="manual_cli")
        notify_admin(rec["token"], rec["label"], "manual_cli")
        print(f"Issued: {rec['token']}")
    else:
        print(json.dumps({
            "validators": list(VALIDATORS.keys()),
            "events": {k: list(v) for k, v
                       in PAYMENT_SUCCESS_EVENTS.items()},
        }, indent=2))
