"""
alert_gate.py — Bug #10 fix: single-call gate for the alert path.

Combines circuit_breaker + risk_budget + dedup into one call so the bot's
alert hot path actually USES the safety modules instead of letting them
sit dormant.

Migration: anywhere the bot does `pro_format.send_embed(WEBHOOK, embed)`
for a strategy alert, replace with:

    from alert_gate import send_signal
    sent = send_signal(
        webhook_url=WEBHOOK,
        embed=embed,
        strategy="BREAKOUT_4H",
        symbol="BTCUSDT",
        opened_at=alert_ts_iso,
        intended_r=1.0,
        image_bytes=chart_png,   # optional
    )
    # Returns False if the alert was blocked by safety; True if delivered.

Provides:
  - allow(strategy, intended_r) → (ok, reason)
  - consume(strategy, intended_r)
  - send_signal(...) — full gated send replacement for pro_format.send_embed
"""
from __future__ import annotations

import sys
from typing import Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")

from srs_logger import get_logger
import circuit_breaker
import risk_budget
import dedup
import audit_log

log = get_logger("alert_gate")


def allow(strategy: str, intended_r: float = 1.0) -> Tuple[bool, str]:
    """Return (ok, reason). reason populated only when blocked.

    Loop #4: regime gating runs FIRST — if the market regime doesn't match
    the strategy's allowed regimes, skip immediately (cheaper than
    breaker/budget which both touch disk)."""
    try:
        import regime
        ok_regime, why = regime.is_strategy_allowed(strategy)
        if not ok_regime:
            return False, f"regime: {why}"
    except Exception:
        pass   # regime unavailable → fall through to other gates
    breaker_reason = circuit_breaker.check_and_apply(strategy)
    if breaker_reason:
        return False, f"circuit_breaker: {breaker_reason}"
    ok, why = risk_budget.allow(strategy, intended_r)
    if not ok:
        return False, f"risk_budget: {why}"
    return True, "ok"


def consume(strategy: str, intended_r: float = 1.0) -> None:
    """Record that an alert fired with this R commitment."""
    risk_budget.consume(strategy, intended_r)


def send_signal(webhook_url: str,
                embed: dict,
                strategy: str,
                symbol: str = "BTCUSDT",
                opened_at: Optional[str] = None,
                intended_r: float = 1.0,
                image_bytes: Optional[bytes] = None,
                use_durable_queue: bool = True) -> bool:
    """Full gated-send replacement for pro_format.send_embed.

    Order of safety checks:
      1. dedup — same (strategy, symbol, opened_at) seen recently? skip.
      2. circuit_breaker — strategy auto-disabled? skip.
      3. risk_budget — would exceed daily/weekly cap? skip.
      4. send via durable webhook_queue (default) or pro_format direct.
      5. consume() the budget.

    Returns True if delivered; False if blocked/failed.
    """
    # 1. Dedup
    key = dedup.make_key(strategy, symbol, opened_at or "no_ts")
    if dedup.seen(key):
        log.info("alert_deduped", strategy=strategy, symbol=symbol,
                 opened_at=opened_at)
        return False

    # QuantConnect-style: snapshot strategy config alongside the alert
    # so any backtest can be reproduced bit-for-bit later.
    try:
        import strategy_loader
        cfg = strategy_loader.load(strategy) or {}
        # Stable hash of config (sorted keys) — bumps when params change
        import hashlib, json as _json
        cfg_hash = hashlib.sha256(
            _json.dumps(cfg, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]
        embed.setdefault("strategy_snapshot", {
            "strategy": strategy, "config_hash": cfg_hash})
    except Exception:
        pass

    # Interactive Brokers-style client_alert_id — subscribers can dedup
    # on this if our delivery retries.
    embed.setdefault("client_alert_id", key)

    # 2 + 3. Safety gates
    ok, reason = allow(strategy, intended_r)
    if not ok:
        log.warn("alert_gated", strategy=strategy, reason=reason)
        try:
            audit_log.record("alert_gated", actor="alert_gate",
                             strategy=strategy, reason=reason)
        except Exception:
            pass
        return False

    # 4. Send (durable queue or direct)
    try:
        if use_durable_queue:
            import webhook_queue
            payload = {"embeds": [embed]}
            files = [("chart.png", image_bytes)] if image_bytes else None
            webhook_queue.enqueue(webhook_url, payload, files=files)
        else:
            import pro_format
            pro_format.send_embed(webhook_url, embed,
                                  image_bytes=image_bytes)
    except Exception as e:
        log.error("alert_send_failed", strategy=strategy, err=str(e))
        return False

    # 5. Consume budget only after successful enqueue/send
    consume(strategy, intended_r)
    log.info("alert_sent", strategy=strategy, symbol=symbol,
             opened_at=opened_at, intended_r=intended_r)
    return True


if __name__ == "__main__":
    import json as _json
    # Dry-run smoke
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        ok, reason = allow(sys.argv[2] if len(sys.argv) > 2 else "BREAKOUT_4H")
        print(_json.dumps({"allow": ok, "reason": reason}, indent=2))
    else:
        print("Usage:")
        print("  alert_gate.py check [strategy]")
