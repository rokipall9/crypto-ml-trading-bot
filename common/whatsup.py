"""
whatsup.py — "What's up?" snapshot generator.

Returns a comprehensive view of what's happening right now:
  • Live BTC price (Bybit perp by default)
  • Top zones in play across all 4 TFs (closest to current price)
  • Recent alerts that fired
  • Cooldown status (when can the next alert fire?)
  • Service health (cryptobot, alerter, etc.)
  • Bot's strategy stance (what it's watching for)

Used by Discord /whatsup slash command. Pure data — no formatting.
"""
from __future__ import annotations

import os
import sys
import json
import time
import subprocess
from datetime import datetime, timezone
from typing import Dict, List, Any

sys.path.insert(0, "/home/ubuntu/bot")
sys.path.insert(0, "/home/ubuntu/common")


def _service_active(svc: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", svc],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _format_age(seconds: float) -> str:
    """Friendly: '3m ago', '1h 12m ago'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m ago"


def _load_alerter_state() -> dict:
    p = "/home/ubuntu/common/zone_alerter_state.json"
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def snapshot() -> Dict[str, Any]:
    """Build the full 'what's up' snapshot."""
    now_ts = time.time()
    out: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    # ─── Live data via the alerter's own helpers (exchange-aware) ────
    try:
        import zone_alerter as za
        cur = za._fetch_live_price("BTCUSDT")
        out["exchange"] = za.EXCHANGE
        out["current_price"] = cur
    except Exception as e:
        out["error_live_price"] = str(e)
        cur = None

    # ─── Active zones across all 4 TFs ────────────────────────────────
    try:
        import luxalgo_smc as lux
        zones_by_tf: Dict[str, List[dict]] = {}
        for tf in ("15m", "1h", "4h", "1d"):
            try:
                df = za._fetch_klines_for_alerter(tf, 500 if tf != "1d" else 300)
                df = df.reset_index(drop=True)
                zs = lux.all_active_zones(df)
                for z in zs:
                    z["tf"] = tf
                zones_by_tf[tf] = zs
            except Exception as e:
                zones_by_tf[tf] = []
                out.setdefault("tf_errors", {})[tf] = str(e)
        # Flatten + filter close-by zones
        all_zones: List[dict] = []
        for zs in zones_by_tf.values():
            all_zones.extend(zs)
        if cur:
            for z in all_zones:
                z["distance_pct"] = ((z["high"] + z["low"]) / 2 - cur) / cur * 100
            # Top 5 closest above + top 5 closest below
            above = sorted(
                [z for z in all_zones if z["distance_pct"] > 0],
                key=lambda x: x["distance_pct"])[:5]
            below = sorted(
                [z for z in all_zones if z["distance_pct"] < 0],
                key=lambda x: -x["distance_pct"])[:5]
            out["zones_above"] = above
            out["zones_below"] = below
            out["zones_total"] = len(all_zones)
    except Exception as e:
        out["error_zones"] = str(e)

    # ─── Alerter state: last fires + cooldown ─────────────────────────
    state = _load_alerter_state()
    last_fire = state.get("last_fire", {})
    last_alert = state.get("last_alert", {})
    last_bull = float(last_fire.get("bullish", 0))
    last_bear = float(last_fire.get("bearish", 0))
    last_any = float(last_fire.get("any", 0))
    if last_bull:
        out["last_bullish_alert_age"] = _format_age(now_ts - last_bull)
    if last_bear:
        out["last_bearish_alert_age"] = _format_age(now_ts - last_bear)
    out["alerts_in_cooldown"] = len(last_alert)

    # Global gap remaining
    try:
        gap_total = za.MIN_GLOBAL_FIRE_GAP_SEC
        gap_left = max(0, int(gap_total - (now_ts - last_any)))
        out["global_gap_remaining_sec"] = gap_left
        out["global_gap_remaining"] = (
            f"{gap_left // 60}m {gap_left % 60}s" if gap_left else "ready")
    except Exception:
        pass

    # Direction lock status
    try:
        dlock = za.DIRECTION_LOCK_SEC
        bull_lock = max(0, int(dlock - (now_ts - last_bull)))
        bear_lock = max(0, int(dlock - (now_ts - last_bear)))
        if bull_lock > 0:
            out["bearish_locked_remaining"] = f"{bull_lock // 60}m"
        if bear_lock > 0:
            out["bullish_locked_remaining"] = f"{bear_lock // 60}m"
    except Exception:
        pass

    # ─── Regime + strategy stance ─────────────────────────────────────
    try:
        import regime
        reg = regime.current()
        out["regime"] = reg.get("regime", "?")
        out["regime_indicators"] = reg.get("indicators", {})
        gates = reg.get("strategy_gates", {})
        allowed = [s for s, ok in gates.items() if ok]
        out["strategies_allowed_now"] = allowed
    except Exception as e:
        out["error_regime"] = str(e)

    # ─── Track record ─────────────────────────────────────────────────
    try:
        import track_record
        s = track_record.all_summary()
        out["trades_resolved"] = s.get("n", 0)
        out["win_rate_pct"] = s.get("wr", 0)
        out["total_r"] = s.get("total_r", 0)
    except Exception as e:
        out["error_track"] = str(e)

    # ─── Risk budget (R consumed today/week) ─────────────────────────
    try:
        import risk_budget
        rb = risk_budget.status()
        out["risk_daily_used"] = rb.get("daily_used", 0)
        out["risk_weekly_used"] = rb.get("weekly_used", 0)
        out["risk_daily_cap"] = rb.get("daily_cap", 5)
        out["risk_weekly_cap"] = rb.get("weekly_cap", 15)
    except Exception:
        pass

    # ─── Service health ───────────────────────────────────────────────
    services = {
        "cryptobot":      "cryptobot.service",
        "status_server":  "status_server.service",
        "zone_alerter":   "zone_alerter.timer",
        "price_alert":    "price_alert.timer",
        "market_pulse":   "market_pulse.timer",
    }
    out["services"] = {name: _service_active(svc)
                       for name, svc in services.items()}

    return out


def short_verdict(snap: Dict[str, Any]) -> str:
    """One-liner: what should the operator focus on right now?"""
    cur = snap.get("current_price")
    if not cur:
        return "checking…"
    above = (snap.get("zones_above") or [None])[0]
    below = (snap.get("zones_below") or [None])[0]
    if not above and not below:
        return "no zones nearby — quiet"
    # Pick whichever is closer
    above_d = above["distance_pct"] if above else 999
    below_d = abs(below["distance_pct"]) if below else 999
    if above_d < below_d and above_d < 0.5:
        return f"⚠ approaching short zone (~{above_d:.2f}% above)"
    if below_d < above_d and below_d < 0.5:
        return f"⚠ approaching long zone (~{below_d:.2f}% below)"
    if above_d < 1.5:
        return f"watching short @ {above_d:.1f}% above"
    if below_d < 1.5:
        return f"watching long @ {below_d:.1f}% below"
    return "mid-range — wait for move"


def render_text(snap: Dict[str, Any]) -> str:
    """Minimal plain-text rendering — 6 lines max."""
    lines = []
    cur = snap.get("current_price")
    ex = snap.get("exchange", "?")
    reg = snap.get("regime", "?")
    cur_str = f"${cur:,.0f}" if cur else "?"
    lines.append(f"📡 BTC {cur_str} ({ex}) · {reg}")
    lines.append(short_verdict(snap))
    lines.append("")
    above = (snap.get("zones_above") or [None])[0]
    below = (snap.get("zones_below") or [None])[0]
    if above:
        kind = above["kind"].replace("_", " ").replace("INT ", "")
        lines.append(f"🔴 Sell  ${above['low']:,.0f}-${above['high']:,.0f}  "
                     f"({above['tf']} {kind}, +{above['distance_pct']:.2f}%)")
    if below:
        kind = below["kind"].replace("_", " ").replace("INT ", "")
        lines.append(f"🟢 Buy   ${below['low']:,.0f}-${below['high']:,.0f}  "
                     f"({below['tf']} {kind}, {below['distance_pct']:.2f}%)")
    # One status line
    last_b = snap.get("last_bullish_alert_age")
    last_e = snap.get("last_bearish_alert_age")
    last = last_b if last_b and (not last_e or "h" in last_e) else last_e
    n_ok = sum(1 for v in snap.get("services", {}).values() if v)
    n_total = len(snap.get("services", {}))
    lines.append(f"🛎 last alert: {last or '—'}  ·  ⚙ {n_ok}/{n_total} services up")
    return "\n".join(lines)


if __name__ == "__main__":
    snap = snapshot()
    print(render_text(snap))
