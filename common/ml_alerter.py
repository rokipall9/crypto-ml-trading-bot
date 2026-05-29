"""
ml_alerter.py — Discord posting for the ML filter room (#ai-trade-review).

Reads DISCORD_WEBHOOK_URL_ML from env. If unset, returns False (no-op).
Reads NOTIFY_USER_ID for the operator @ tag.
"""
from __future__ import annotations

import os
import json
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Optional


WEBHOOK_URL = (os.environ.get("DISCORD_WEBHOOK_URL_ML")
               or os.environ.get("NOTIFY_ML_WEBHOOK") or "").strip()


def _tier_color(tier: str) -> int:
    return {
        "HIGH": 0x00C853,   # green
        "MED":  0xFFB300,   # amber
        "LOW":  0xF44336,   # red
        "SKIP": 0x6B7280,   # gray
    }.get(tier.upper(), 0x5B8DEF)


def _tier_emoji(tier: str) -> str:
    return {
        "HIGH": "🟢 HIGH",
        "MED":  "🟡 MED",
        "LOW":  "🔴 LOW",
        "SKIP": "⚫ SKIP",
    }.get(tier.upper(), tier)


def _notify_tag():
    uid = (os.environ.get("NOTIFY_USER_ID") or "").strip()
    if uid.isdigit():
        return ("<@%s>" % uid,
                {"users": [uid], "parse": []})
    return ("", {})


def _post(payload: Dict, url: str = "") -> bool:
    target = url or WEBHOOK_URL
    if not target:
        return False
    try:
        req = urllib.request.Request(
            target, data=json.dumps(payload, default=str).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "ml-alerter/1.0"})
        urllib.request.urlopen(req, timeout=8).read()
        return True
    except Exception as e:
        print(f"[ml_alerter] post fail: {e}")
        return False


# ─── Public API ──────────────────────────────────────────────────────

def post_signal_review(score: Dict, sig: Dict) -> bool:
    """Post a pre-launch ML review embed to the ML channel.

    Called from paper_trader / book_paper_runner right after score_signal()
    and BEFORE the trade is recorded.
    """
    if not WEBHOOK_URL:
        return False

    f = score.get("features", {}) or {}
    tier = score.get("tier", "?")
    strategy = sig.get("strategy") or sig.get("method") or "?"
    side = (sig.get("side") or "?").upper()
    side_word = "BUY" if side in ("BUY", "LONG") else "SELL"
    entry = float(sig.get("entry", 0) or 0)
    sl = float(sig.get("sl") or sig.get("stop", 0) or 0)
    tp = float(sig.get("tp") or sig.get("target") or sig.get("tp1", 0) or 0)
    rr = f.get("target_R") or 0.0
    mode = score.get("mode", "?")

    # Reasoning bullets
    reasons_block = "\n".join(score.get("reasons", []) or ["—"])

    # Sub-score breakdown
    bd = score.get("breakdown", {}) or {}
    bd_rows = []
    for k, v in bd.items():
        bar = "█" * int(v * 10) + "░" * (10 - int(v * 10))
        bd_rows.append(f"`{k:18s}` `{bar}` `{v:.2f}`")
    breakdown_block = "\n".join(bd_rows) if bd_rows else "—"

    # Recent strategy performance
    perf_block = (
        f"`samples={f.get('strategy_n_samples', 0)}` "
        f"`wr={(f.get('strategy_winrate_10') or 0) * 100:.0f}%` "
        f"`avgR={f.get('strategy_avg_R_10') or 0:+.2f}` "
        f"`last3R={f.get('strategy_last_3_R') or []}`"
    )

    decision_note = ("✅ **WOULD ALLOW**" if score.get("allow")
                     else "⛔ **WOULD SKIP**")
    if mode == "shadow":
        decision_note += "  _(shadow mode — paper still opens regardless)_"

    embed = {
        "title": f"{_tier_emoji(tier)} · {strategy} · {side_word} review",
        "color": _tier_color(tier),
        "description": (
            f"**Score:** `{score.get('score', 0):.3f}`  "
            f"**Tier:** `{tier}`  "
            f"**Model:** `{score.get('model', '?')}`\n"
            f"{decision_note}\n"
        ),
        "fields": [
            {"name": "Entry", "value": f"${entry:,.2f}", "inline": True},
            {"name": "SL", "value": f"${sl:,.2f}", "inline": True},
            {"name": "TP", "value": f"${tp:,.2f}", "inline": True},
            {"name": "R:R", "value": f"{rr:.2f}R", "inline": True},
            {"name": "Regime", "value": str(f.get("regime", "?")), "inline": True},
            {"name": "ATR× norm",
             "value": (f"{f.get('atr_norm'):.2f}×" if f.get("atr_norm")
                       else "—"),
             "inline": True},
            {"name": "Funding",
             "value": (f"{f.get('funding_rate') * 100:+.3f}%"
                       if f.get("funding_rate") is not None else "—"),
             "inline": True},
            {"name": "Bybit pos",
             "value": (f"{f.get('bybit_position_size'):.3f} "
                       f"{f.get('bybit_position_side')}"
                       if f.get("bybit_has_position") else "none"),
             "inline": True},
            {"name": "Hour UTC",
             "value": f"{f.get('hour_utc', '?'):02d}:00",
             "inline": True},
            {"name": "Reasoning", "value": reasons_block or "—",
             "inline": False},
            {"name": "Score breakdown", "value": breakdown_block,
             "inline": False},
            {"name": f"{strategy} recent performance", "value": perf_block,
             "inline": False},
        ],
        "footer": {"text": f"ML filter · mode={mode} · {score.get('latency_ms', 0)}ms"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = {"username": "🤖 ML Trade Review", "embeds": [embed]}
    tag, am = _notify_tag()
    if tag and tier in ("HIGH", "MED"):   # only ping on real candidates
        payload["content"] = tag
        payload["allowed_mentions"] = am
    return _post(payload)


def post_outcome(score: Dict, outcome: Dict) -> bool:
    """When a trade resolves, post the outcome so the room can compare
    the ML prediction vs reality.
    """
    if not WEBHOOK_URL:
        return False
    tier = score.get("tier", "?")
    R = outcome.get("realized_R")
    pnl = outcome.get("realized_pnl_usdt") or outcome.get("realized_pnl")
    if R is None or pnl is None:
        return False
    win = pnl > 0
    color = 0x00C853 if win else 0xF44336
    strategy = score.get("features", {}).get("strategy", "?")
    embed = {
        "title": (f"{'✅ WIN' if win else '❌ LOSS'} · {strategy} · "
                  f"predicted {tier} (score {score.get('score', 0):.2f})"),
        "color": color,
        "fields": [
            {"name": "P&L", "value": f"${pnl:+.2f}", "inline": True},
            {"name": "R", "value": f"{R:+.2f}R", "inline": True},
            {"name": "Exit", "value": str(outcome.get("exit_price",
                                                       outcome.get("exit_price_approx", "?"))),
             "inline": True},
            {"name": "ML was right?",
             "value": ("✅ yes" if ((tier == "HIGH" and win) or
                                    (tier in ("LOW", "SKIP") and not win))
                       else "❌ no"),
             "inline": True},
        ],
        "footer": {"text": f"Outcome recorded · ML score {score.get('score', 0):.3f}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return _post({"username": "🤖 ML Trade Review", "embeds": [embed]})


# CLI smoke test
if __name__ == "__main__":
    print("WEBHOOK_URL set?", bool(WEBHOOK_URL))
    print("Tag set?", _notify_tag())
