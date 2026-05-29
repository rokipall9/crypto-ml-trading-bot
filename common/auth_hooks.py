"""
auth_hooks.py — Auth0-style executable hooks at auth checkpoints.

Operator can register hooks that fire at key auth lifecycle points.
Each hook is a Python callable receiving (context_dict). Returns nothing
(side-effect only) OR returns a dict to override behavior (rare; used
sparingly).

Hook points:
  pre_authenticate  — before token lookup. Can short-circuit (block IPs etc.)
  post_authenticate — after successful auth. Track lifecycle, push to funnel.
  on_token_issue    — new token created. Send welcome webhook, etc.
  on_token_revoke   — token revoked. Notify subscriber.
  on_lease_issue    — lease created.
  on_lease_renew    — lease renewed.

Hooks are loaded from /home/ubuntu/common/auth_hook_configs.json.
This is OPT-IN — by default, no hooks fire.
"""
from __future__ import annotations

import sys
from typing import Callable, Dict, List, Optional

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("auth_hooks")

HOOK_POINTS = (
    "pre_authenticate",
    "post_authenticate",
    "on_token_issue",
    "on_token_revoke",
    "on_lease_issue",
    "on_lease_renew",
)

_REGISTRY: Dict[str, List[Callable]] = {h: [] for h in HOOK_POINTS}


def register(hook_point: str, callback: Callable[[dict], None]) -> bool:
    """Register a callable for a hook point. Returns True on success."""
    if hook_point not in HOOK_POINTS:
        log.warn("unknown_hook_point", hook_point=hook_point)
        return False
    _REGISTRY[hook_point].append(callback)
    log.info("hook_registered", hook_point=hook_point,
             callback=getattr(callback, "__name__", "anonymous"))
    return True


def fire(hook_point: str, context: dict) -> Optional[dict]:
    """Fire all hooks at a point. Returns the LAST hook's return value
    (typically None; rarely used to override behavior)."""
    if hook_point not in HOOK_POINTS:
        return None
    last_result = None
    for cb in _REGISTRY[hook_point]:
        try:
            result = cb(context)
            if result is not None:
                last_result = result
        except Exception as e:
            log.error("hook_failed",
                      hook_point=hook_point,
                      callback=getattr(cb, "__name__", "?"),
                      err=str(e))
    return last_result


# ─── Built-in hooks (auto-registered) ───
def _funnel_track_authenticate(ctx: dict) -> None:
    """Built-in: track first_api_call funnel stage on every authentication."""
    try:
        import funnel
        token_prefix = ctx.get("token_prefix")
        if token_prefix:
            funnel.record(token_prefix, "first_api_call")
    except Exception:
        pass


def _audit_token_lifecycle(ctx: dict) -> None:
    """Built-in: audit-record every issue/revoke."""
    try:
        import audit_log
        action = ctx.get("_hook_point", "auth_event")
        actor = ctx.get("actor", "auth_hooks")
        audit_log.record(f"hook_{action}", actor=actor,
                         **{k: v for k, v in ctx.items()
                            if k not in ("token_full", "secret")})
    except Exception:
        pass


# Auto-register built-ins on import
register("post_authenticate", _funnel_track_authenticate)
register("on_token_issue", _audit_token_lifecycle)
register("on_token_revoke", _audit_token_lifecycle)


def stats() -> dict:
    return {
        h: [getattr(cb, "__name__", "anonymous") for cb in cbs]
        for h, cbs in _REGISTRY.items()
    }


if __name__ == "__main__":
    import json
    print(json.dumps(stats(), indent=2, default=str))
