"""
bot/discord_webhook.py — Professional Discord signal delivery.

Sends beautifully formatted embeds to your Discord channel via webhook.
Supports both signal alerts and daily performance summaries.

Usage:
    from bot.discord_webhook import send_signal, send_summary
    send_signal(signal_dict)
"""

from __future__ import annotations

import os
import sys
import json
import requests
from datetime import datetime, timezone
from typing import Optional

# Shared pro embed builders (single source of truth across bots)
sys.path.insert(0, "/home/ubuntu/common")
try:
    import pro_format
    HAS_PRO_FORMAT = True
except ImportError:
    HAS_PRO_FORMAT = False
    print("[discord_webhook] pro_format not available, falling back to legacy embeds")

# ── Load webhook URL from environment ─────────────────────────────────
WEBHOOK_URL: Optional[str] = os.getenv("DISCORD_WEBHOOK_URL")
WATCH_WEBHOOK_URL: Optional[str] = os.getenv("DISCORD_WATCH_WEBHOOK")
# DAILY_WEBHOOK_v1: dedicated channel for the daily perf summary
DAILY_WEBHOOK_URL: Optional[str] = os.getenv("DISCORD_WEBHOOK_URL_DAILY")

# TV_CHART_URL_v1: clickable chart in watch alerts
_TV_CHART_URL = os.getenv(
    "TRADINGVIEW_CHART_URL",
    "https://www.tradingview.com/chart/pViMM9Zt/?symbol=BYBIT%3ABTCUSDT.P",
).strip()

# ── Colour codes (Discord uses decimal) ───────────────────────────────
COLOR_BUY     = 0x00C851   # green
COLOR_SELL    = 0xFF4444   # red
COLOR_INFO    = 0x33B5E5   # blue
COLOR_WARNING = 0xFFBB33   # orange
COLOR_SUCCESS = 0x00C851   # green

# ── Grade → emoji ────────────────────────────────────────────────────
GRADE_EMOJI = {
    "A+": "🏆", "A": "⭐", "B": "✅", "C": "⚠️", "D": "❌",
}

# ── Bybit perpetual base URL ──────────────────────────────────────────
BYBIT_URL = "https://www.bybit.com/trade/usdt/{pair}"


# =====================================================================
# Core send function
# =====================================================================

def _post(payload: dict) -> bool:
    """POST payload to Discord webhook. Returns True on success."""
    if not WEBHOOK_URL:
        print("[discord] ⚠  DISCORD_WEBHOOK_URL not set — skipping.")
        return False
    try:
        resp = requests.post(
            WEBHOOK_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            return True
        print(f"[discord] ✗ HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as exc:
        print(f"[discord] ✗ Request failed: {exc}")
        return False


# =====================================================================
# Signal embed
# =====================================================================

def send_signal(sig: dict) -> bool:
    """
    Send a full trading signal. Routes through pro_format embed builder
    when available — otherwise falls back to legacy embed below.

    Expected keys in `sig`:
        side, symbol, interval, time, entry, tp1, tp2, tp3,
        sl, atr, probability, confluence_score, grade, grade_desc,
        risk_pct, rr_blended, account_balance, position, confluences
    """
    if HAS_PRO_FORMAT and WEBHOOK_URL:
        # Adapt cryptobot's signal schema → pro_format
        side_pf = "long" if sig["side"].upper() == "BUY" else "short"
        # Map grade to score (A+=10, A=9, B=8, C=6, D=4)
        grade_to_score = {"A+": 10, "A": 9, "B": 8, "C": 6, "D": 4}
        score = grade_to_score.get(sig.get("grade", "B"), 7)
        # Confluences → breakdown format
        breakdown = [(("conf%d" % i), 2, c) for i, c in enumerate(sig.get("confluences", [])[:6])]
        try:
            embed = pro_format.build_signal_embed(
                symbol=sig["symbol"], side=side_pf,
                entry=float(sig["entry"]), sl=float(sig["sl"]),
                tp=float(sig.get("tp1", sig.get("tp", 0))),
                rr=float(sig.get("rr_blended", 0) or 0),
                score=score, breakdown=breakdown,
                fg=int(sig.get("fg", 50)),
                system=sig.get("strategy_name", sig.get("method", "?")),
                regime=sig.get("regime", "BULL"),
                sl_source=sig.get("sl_source", "—"),
                tp_source=sig.get("tp_source", "—"),
            )
            return pro_format.send_embed(WEBHOOK_URL, embed, username="📊 SMC Signal Bot")
        except Exception as exc:
            print("[discord] pro_format build failed (%s) — falling back to legacy" % exc)

    # ── LEGACY embed fallback ──
    side      = sig["side"].upper()       # BUY / SELL
    symbol    = sig["symbol"]
    interval  = sig.get("interval", "15m").upper()
    prob      = sig.get("probability", 0)
    grade     = sig.get("grade", "B")
    conf_list = sig.get("confluences", [])
    pos       = sig.get("position", {})
    ts        = sig.get("time", datetime.now(timezone.utc))

    color     = COLOR_BUY if side == "BUY" else COLOR_SELL
    arrow     = "📈" if side == "BUY"  else "📉"
    g_emoji   = GRADE_EMOJI.get(grade, "✅")
    bybit_lnk = BYBIT_URL.format(pair=symbol.replace("USDT", ""))

    # ── Direction + entry block ────────────────────────────────────────
    entry_str = f"${sig['entry']:,.2f}"
    tp1_str   = f"${sig['tp1']:,.2f}  (+{abs(sig['tp1']-sig['entry'])/sig['entry']*100:.2f}%)"
    tp2_str   = f"${sig['tp2']:,.2f}  (+{abs(sig['tp2']-sig['entry'])/sig['entry']*100:.2f}%)"
    _tp3      = sig.get('tp3') or 0
    tp3_str   = (f"${_tp3:,.2f}  (+{abs(_tp3-sig['entry'])/sig['entry']*100:.2f}%)"
                 if side == "BUY" else
                 f"${_tp3:,.2f}  (-{abs(_tp3-sig['entry'])/sig['entry']*100:.2f}%)") \
                if _tp3 else "—"   # FIX: show dash instead of $0.00 when tp3 is missing
    sl_pct    = abs(sig['sl'] - sig['entry']) / sig['entry'] * 100
    sl_str    = f"${sig['sl']:,.2f}  (-{sl_pct:.2f}%)" if side == "BUY" \
                else f"${sig['sl']:,.2f}  (+{sl_pct:.2f}%)"

    # ── SMC confluence list ────────────────────────────────────────────
    conf_text = "\n".join(f"✓ {c}" for c in conf_list) if conf_list else "—"

    # ── Position sizing ────────────────────────────────────────────────
    risk_amt  = pos.get("risk_amount", 0)
    units     = pos.get("units", 0)
    pos_val   = pos.get("position_value", 0)
    acc_bal   = sig.get("account_balance", 10000)
    risk_pct  = sig.get("risk_pct", 2.0)

    sizing_text = (
        f"Risk {risk_pct:.1f}% = **${risk_amt:,.0f}**  →  "
        f"**{units:.4f} {symbol.replace('USDT','')}**\n"
        f"Position value: ${pos_val:,.0f}  |  Account: ${acc_bal:,.0f}"
    )

    # ── Build embed ────────────────────────────────────────────────────
    embed = {
        "title": f"{arrow}  {side} SIGNAL — {symbol}  [{interval}]",
        "color": color,
        "url":   bybit_lnk,
        "timestamp": ts.isoformat(),
        "fields": [
            {
                "name":   "🎯  Entry",
                "value":  f"**{entry_str}**",
                "inline": True,
            },
            {
                "name":   f"{g_emoji}  Grade",
                "value":  f"**{grade}**  |  Win Prob: **{prob:.0%}**",
                "inline": True,
            },
            {
                "name":   "🔗  Bybit",
                "value":  f"[Open {symbol}]({bybit_lnk})",
                "inline": True,
            },
            {
                "name":   "🎯  Take Profits  *(scale out — don't exit all at once)*",
                "value": (
                    f"**TP1 (50%):** {tp1_str}  → *move SL to entry (break-even)*\n"
                    f"**TP2 (30%):** {tp2_str}\n"
                    f"**TP3 (20%):** {tp3_str}"
                ),
                "inline": False,
            },
            {
                "name":   "🛑  Stop Loss",
                "value":  f"**{sl_str}**  |  R:R = 1 : {sig.get('rr_blended', 0):.2f}",
                "inline": False,
            },
            {
                "name":   "💰  Position Sizing",
                "value":  sizing_text,
                "inline": False,
            },
            {
                "name":   f"📐  SMC Confluence  ({len(conf_list)} signals active)",
                "value":  f"```{conf_text}```",
                "inline": False,
            },
        ],
        "footer": {
            "text": (
                f"SMC Signal Bot  •  {interval} timeframe  •  "
                "Not financial advice — trade responsibly"
            ),
        },
        "thumbnail": {
            "url": "https://cryptologos.cc/logos/bitcoin-btc-logo.png"
        },
    }

    payload = {
        "username":   "📊 SMC Signal Bot",
        "avatar_url": "https://cryptologos.cc/logos/bitcoin-btc-logo.png",
        "embeds":     [embed],
    }

    ok = _post(payload)
    if ok:
        print(f"[discord] ✓ Signal sent → {side} {symbol} {interval}")
    return ok


# =====================================================================
# Daily performance summary
# =====================================================================

def send_daily_summary(stats: dict) -> bool:
    """
    Send a daily win/loss summary embed.

    Expected keys in `stats`:
        date, total_signals, wins, losses, win_rate,
        avg_rr, best_trade, worst_trade, active_models
    """
    wins  = stats.get("wins", 0)
    total = stats.get("total_signals", 0)
    rate  = stats.get("win_rate", 0)

    color = COLOR_SUCCESS if rate >= 0.75 else COLOR_WARNING

    embed = {
        "title":       "📊  Daily Performance Summary",
        "color":       color,
        "description": f"**Date:** {stats.get('date', 'Today')}",
        "fields": [
            {
                "name":   "📈  Results",
                "value": (
                    f"Signals: **{total}**\n"
                    f"Wins: **{wins}**  |  Losses: **{stats.get('losses',0)}**\n"
                    f"Win Rate: **{rate:.1%}**"
                ),
                "inline": True,
            },
            {
                "name":   "⚡  Performance",
                "value": (
                    f"Avg R:R: **{stats.get('avg_rr',0):.2f}**\n"
                    f"Best: **{stats.get('best_trade','—')}**\n"
                    f"Worst: **{stats.get('worst_trade','—')}**"
                ),
                "inline": True,
            },
            {
                "name":   "🤖  Active Models",
                "value":  f"**{stats.get('active_models', 0)}** models scanning",
                "inline": True,
            },
        ],
        "footer": {
            "text": "SMC Signal Bot  •  Daily Summary",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Daily summary → dedicated channel if configured
    if DAILY_WEBHOOK_URL:
        try:
            import requests as _r, json as _j
            resp = _r.post(DAILY_WEBHOOK_URL,
                           json={"username": "📊 SMC Signal Bot", "embeds": [embed]},
                           headers={"Content-Type": "application/json"}, timeout=10)
            return resp.status_code in (200, 204)
        except Exception as _e:
            print("[daily-summary] dedicated channel post failed: %s" % _e)
    return _post({
        "username": "📊 SMC Signal Bot",
        "embeds":   [embed],
    })


# =====================================================================
# System status ping
# =====================================================================

def send_status(message: str, level: str = "info") -> bool:
    """Send a quick status message (startup, error, warning)."""
    colors = {"info": COLOR_INFO, "warning": COLOR_WARNING, "error": COLOR_SELL}
    icons  = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}

    embed = {
        "title":       f"{icons.get(level,'ℹ️')}  System Status",
        "description": message,
        "color":       colors.get(level, COLOR_INFO),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "footer":      {"text": "SMC Signal Bot"},
    }

    return _post({"username": "📊 SMC Signal Bot", "embeds": [embed]})


# =====================================================================
# Watch heartbeat (4H bot pulse → #watch-alerts channel)
# =====================================================================

def _post_watch(payload: dict) -> bool:
    """POST payload to the watch webhook. Falls back to main webhook if unset."""
    url = WATCH_WEBHOOK_URL or WEBHOOK_URL
    if not url:
        print("[watch] ⚠  No watch webhook configured — skipping heartbeat.")
        return False
    try:
        resp = requests.post(
            url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            return True
        print(f"[watch] ✗ HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as exc:
        print(f"[watch] ✗ Request failed: {exc}")
        return False


def _bar(pct: float, width: int = 10) -> str:
    """ASCII progress bar. pct in 0..100."""
    filled = max(0, min(width, int(round(pct / 100.0 * width))))
    return "█" * filled + "░" * (width - filled)


def _parse_proximity(reason: str):
    """
    Parse a reason string from StrategyEngine._reasons into (strategy, pct, hint).

    Returns:
        (strategy_name, pct_0_to_100 or None, short_hint_string)

    If pct cannot be computed from the string (e.g. "Not bullish candle"),
    pct is None and a categorical state ("low", "mid") is returned in hint.
    """
    import re

    m = re.match(r"\[(\w+)\]\s*(.*)", reason)
    if not m:
        return ("?", None, reason[:60])
    strat, body = m.group(1), m.group(2).strip()

    # BREAKOUT: "No breakout (close=77630, high30=79473)"
    mm = re.search(r"close=(\d+).*?high30=(\d+)", body)
    if mm:
        c, h = float(mm.group(1)), float(mm.group(2))
        if h > 0:
            pct = min(100.0, c / h * 100.0)
            gap = (h - c) / h * 100.0
            return (strat, pct, "needs $%.0f (+%.1f%%)" % (h, gap))

    # DIP: "No dip (1.8% < 3.0%)"
    mm = re.search(r"No dip \(([\d.]+)%\s*<\s*([\d.]+)%\)", body)
    if mm:
        cur, need = float(mm.group(1)), float(mm.group(2))
        pct = min(100.0, cur / need * 100.0) if need else 0
        return (strat, pct, "dip %.1f%%, needs %.1f%%" % (cur, need))

    # COOLDOWN anywhere
    if "COOLDOWN" in body.upper():
        return (strat, None, "cooldown")

    # PULLBACK: common rejections, no number → qualitative
    qual_map = {
        "Not bullish candle": (0, "bearish bar"),
        "Small body": (30, "body too small"),
        "Low volume": (30, "vol too low"),
        "Below EMA21": (20, "below EMA21"),
        "Close below EMA21": (20, "close below EMA21"),
        "Below EMA55": (20, "below EMA55"),
        "Too far below EMA40": (10, "too far below EMA40"),
        "Too far above EMA40": (10, "too far above EMA40"),
        "Low didn't cross below EMA21": (15, "low didn't reach EMA21"),
        "Bad SL distance": (0, "bad SL"),
        "Vol not expanding": (25, "vol flat"),
        "Not enough dry bars": (15, "vol not compressed"),
        "Too far from EMA21": (10, "far from EMA21"),
    }
    for key, (pct, hint) in qual_map.items():
        if key in body:
            return (strat, float(pct), hint)

    # Fallback — unknown
    return (strat, None, body[:45])


# Short human-readable names for each strategy
_STRAT_LABEL = {
    "BREAKOUT": "BREAKOUT ",
    "MOMENTUM": "MOMENTUM ",
    "DIP":      "PANIC DIP",
    "PULLBACK": "PULLBACK ",
    "VRESET":   "VOL RESET",
    "REJECTION": "REJECT SH",
}


def _format_proximity_block(reasons: list) -> str:
    """
    Reformat the engine's flat list of reject-reason strings into a
    per-strategy progress-bar block, sorted by closeness (highest first).
    """
    parsed = [_parse_proximity(r) for r in reasons]
    # Keep only the FIRST occurrence of each strategy (engine can log multiple)
    seen = set()
    rows = []
    for strat, pct, hint in parsed:
        if strat in seen or strat == "?":
            continue
        seen.add(strat)
        rows.append((strat, pct, hint))
    # Sort by proximity (None treated as -1 so "waiting" items float to bottom)
    rows.sort(key=lambda r: -(r[1] if r[1] is not None else -1))

    lines = []
    for strat, pct, hint in rows:
        label = _STRAT_LABEL.get(strat, strat[:9].ljust(9))
        if pct is None:
            lines.append(f"`{label} ░░░░░░░░░░  —     {hint}`")
        else:
            bar = _bar(pct)
            lines.append(f"`{label} {bar}  {pct:5.1f}%  {hint}`")
    if not lines:
        return "No strategies evaluated."
    # Add summary line about closest
    closest = next((r for r in rows if r[1] is not None), None)
    if closest and closest[1] >= 90:
        lines.append(f"**⚠ Closest: {closest[0]} at {closest[1]:.1f}% — watch next 4H close**")
    return "\n".join(lines)


def send_watch_heartbeat(
    regime: str,
    price: float,
    atr: float,
    fg: int,
    reasons: list,
    signals: Optional[list] = None,
    blocked: Optional[str] = None,
) -> bool:
    """
    Post a per-4H-close bot heartbeat to the watch channel.

    Routes through pro_format when available. Falls back to legacy embed.
    """
    # ── New pro_format path (preferred) ──
    if HAS_PRO_FORMAT and (WATCH_WEBHOOK_URL or WEBHOOK_URL):
        url = WATCH_WEBHOOK_URL or WEBHOOK_URL
        try:
            # Convert raw reason strings → (factor, pct, hint) tuples for pro_format
            factors = [_parse_proximity(r) for r in (reasons or [])]
            # Filter out "?" entries and dedupe by strategy
            seen = set(); cleaned = []
            for strat, pct, hint in factors:
                if strat == "?" or strat in seen:
                    continue
                seen.add(strat)
                # pro_format expects pct as float 0..100 or None
                cleaned.append((strat, pct, hint))
            cleaned.sort(key=lambda r: -(r[1] if r[1] is not None else -1))

            embed = pro_format.build_heartbeat_embed(
                symbol="BTCUSDT", price=price, regime=regime, fg=fg, atr=atr,
                factors=cleaned, mode="PAPER", bot_name="cryptobot",
            )
            if signals:
                embed["title"] = "🎯  Signal Fired  ·  BTCUSDT " + regime
                embed["color"] = pro_format.COLOR_LONG
            elif blocked:
                embed["title"] = "⏸️  Check Blocked  ·  BTCUSDT " + regime
                embed["description"] = "_%s_" % blocked
                embed["color"] = pro_format.COLOR_WARNING

            embed["url"] = _TV_CHART_URL
            existing_desc = embed.get("description") or ""
            embed["description"] = (existing_desc + "\n\n📊 [Open chart](" + _TV_CHART_URL + ")").strip()
            return pro_format.send_embed(url, embed, username="🤖 cryptobot · Watch")
        except Exception as exc:
            print("[discord] heartbeat pro_format failed (%s) — legacy fallback" % exc)

    # ── LEGACY embed fallback ──
    now = datetime.now(timezone.utc)

    # Regime visuals
    if regime == "BULL":
        color = COLOR_BUY
        regime_emoji = "📈"
    elif regime == "BEAR":
        color = COLOR_SELL
        regime_emoji = "📉"
    else:
        color = COLOR_WARNING
        regime_emoji = "⏸️"

    # Title + main status block
    if signals:
        title = "🎯  Signal Fired"
        color = COLOR_SUCCESS
        lines = []
        for s in signals:
            side = str(s.get("side", "buy")).upper()
            lines.append(
                f"**[{s['strategy']}]** {side} @ ${s['entry']:,.0f}  |  SL ${s['sl']:,.0f}"
            )
        status_block = "\n".join(lines)
    elif blocked:
        title = "⏸️  Check Blocked"
        color = COLOR_WARNING
        status_block = f"**{blocked}**"
    else:
        title = f"{regime_emoji}  4H Heartbeat"
        if reasons:
            status_block = _format_proximity_block(reasons)
        else:
            status_block = "No conditions evaluated."

    embed = {
        "title": f"{title}  —  {regime}",
        "url": _TV_CHART_URL,
        "color": color,
        "fields": [
            {"name": "Regime",     "value": regime,              "inline": True},
            {"name": "Price",      "value": f"${price:,.0f}",    "inline": True},
            {"name": "F&G",        "value": str(fg),             "inline": True},
            {"name": "ATR(14)",    "value": f"${atr:,.0f}",      "inline": True},
            {"name": "Time (UTC)", "value": now.strftime("%Y-%m-%d %H:%M"), "inline": True},
            {"name": "Mode",       "value": "PAPER",             "inline": True},
            {"name": "Status",     "value": status_block,        "inline": False},
            {"name": "Chart",      "value": f"📊 [Open chart]({_TV_CHART_URL})", "inline": False},
        ],
        "footer": {"text": "4H candle close • bot alive"},
        "timestamp": now.isoformat(),
    }

    return _post_watch({
        "username": "🤖 4H Watch",
        "embeds":   [embed],
    })
