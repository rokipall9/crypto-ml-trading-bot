"""
pro_format.py — pro-grade Discord embed builders for signal bots.

Single source of truth for how alerts and reports look in Discord.
Used by cryptobot heartbeat, daily_signal alerts, weekly_report, etc.

Three embed types:
  build_signal_embed      — trade alert (green for long, red for short)
  build_heartbeat_embed   — operations heartbeat (blurple)
  build_weekly_embed      — weekly retrospective (amber)
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional


# Brand assets
BTC_LOGO = "https://cryptologos.cc/logos/bitcoin-btc-logo.png"
ETH_LOGO = "https://cryptologos.cc/logos/ethereum-eth-logo.png"
BOT_AVATAR = BTC_LOGO    # default

# Color palette (Discord uses decimal RGB)
COLOR_LONG     = 0x00C851   # vibrant green
COLOR_SHORT    = 0xFF4444   # vibrant red
COLOR_HEARTBEAT = 0x5865F2  # Discord blurple
COLOR_WEEKLY   = 0xFFB400   # premium amber/gold
COLOR_NEUTRAL  = 0x99AAB5   # discord gray
COLOR_WARNING  = 0xFFA500   # orange
COLOR_DANGER   = 0xCC0000   # darker red

DISCLAIMER = "⚠ Educational only — not financial advice. Paper / dry-run mode."


def _logo_for(symbol: str) -> str:
    if symbol.upper().startswith("ETH"):
        return ETH_LOGO
    return BTC_LOGO


def _bar(pct: float, width: int = 16) -> str:
    """ASCII progress bar. pct in 0..100."""
    filled = max(0, min(width, int(round(pct / 100.0 * width))))
    return "█" * filled + "░" * (width - filled)


# ── Trade Signal embed ───────────────────────────────────────────────
def build_signal_embed(symbol: str, side: str, entry: float, sl: float, tp: float,
                       rr: float, score: int, score_max: int = 10,
                       breakdown: Optional[List] = None, fg: int = 50,
                       system: str = "A", regime: str = "BULL",
                       sl_source: str = "swing", tp_source: str = "swing") -> Dict:
    """Build a pro-grade trade signal embed."""
    is_long = side.lower() in ("long", "buy")
    color = COLOR_LONG if is_long else COLOR_SHORT
    arrow = "📈" if is_long else "📉"
    side_label = "LONG" if is_long else "SHORT"

    sl_pct = abs(sl - entry) / entry * 100
    tp_pct = abs(tp - entry) / entry * 100
    sl_sign = "-" if is_long else "+"
    tp_sign = "+" if is_long else "-"

    # Setup breakdown text
    if breakdown:
        bd_lines = []
        for factor, pts, reason in breakdown:
            mark = "✓" if pts > 0 else "✗"
            short = reason if len(reason) <= 60 else reason[:57] + "..."
            bd_lines.append(f"{mark} {short}")
        breakdown_text = "\n".join(bd_lines)
    else:
        breakdown_text = "—"

    embed = {
        "title": f"{arrow}  TRADE SIGNAL  ·  {symbol} {side_label}",
        "description": (f"**Verified setup** · Score **{score}/{score_max}** · "
                        f"Risk:Reward **1:{rr:.2f}** · Regime **{regime}**"),
        "color": color,
        "fields": [
            {"name": "📍  Entry",
             "value": f"```\n${entry:,.0f}\n```",
             "inline": True},
            {"name": "🛑  Stop Loss",
             "value": f"```\n${sl:,.0f}\n{sl_sign}{sl_pct:.2f}%\n```",
             "inline": True},
            {"name": "🎯  Take Profit",
             "value": f"```\n${tp:,.0f}\n{tp_sign}{tp_pct:.2f}%\n```",
             "inline": True},
            {"name": "⚖️  Risk : Reward",
             "value": f"**1 : {rr:.2f}**",
             "inline": True},
            {"name": "😨  F&G",
             "value": f"**{fg}**",
             "inline": True},
            {"name": "🏷️  System",
             "value": f"`{system}`",
             "inline": True},
            {"name": "📊  Setup Breakdown",
             "value": f"```diff\n{breakdown_text}\n```",
             "inline": False},
            {"name": "📐  Levels",
             "value": (f"SL anchored to: `{sl_source}`  ·  "
                       f"TP anchored to: `{tp_source}`"),
             "inline": False},
        ],
        "footer": {
            "text": f"Pro Signal · {symbol} · {DISCLAIMER}",
            "icon_url": _logo_for(symbol),
        },
        "thumbnail": {"url": _logo_for(symbol)},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return embed


# ── Heartbeat embed ──────────────────────────────────────────────────
def build_heartbeat_embed(symbol: str, price: float, regime: str, fg: int,
                          atr: float = 0,
                          factors: Optional[List] = None,
                          mode: str = "PAPER",
                          bot_name: str = "Pro Signal") -> Dict:
    """Build heartbeat embed with closeness bars per factor.

    factors: list of (factor_name, pct_0_100, hint_str). Sorted by closeness.
    """
    # Regime visuals
    regime_emoji = {"BULL": "📈", "BEAR": "📉", "CHOP": "⏸️"}.get(regime, "❓")
    regime_color_map = {"BULL": COLOR_LONG, "BEAR": COLOR_SHORT, "CHOP": COLOR_NEUTRAL}
    color = regime_color_map.get(regime, COLOR_HEARTBEAT)

    # Build proximity table as code block (monospace alignment)
    if factors:
        rows = []
        for name, pct, hint in factors[:6]:   # max 6 rows
            label = name[:11].ljust(11)
            if pct is None:
                rows.append(f"{label} ░░░░░░░░░░░░░░░░  ―       {hint[:30]}")
            else:
                bar = _bar(pct, 16)
                rows.append(f"{label} {bar}  {pct:5.1f}%  {hint[:30]}")
        prox_text = "```\n" + "\n".join(rows) + "\n```"

        # Find closest
        scored = [f for f in factors if f[1] is not None]
        if scored:
            closest = max(scored, key=lambda f: f[1])
            if closest[1] >= 90:
                closest_line = f"⚠ **{closest[0]} at {closest[1]:.1f}%** — watch next 4H close"
            else:
                closest_line = f"Closest: **{closest[0]}** at {closest[1]:.1f}%"
        else:
            closest_line = "No active proximity"
    else:
        prox_text = "```\nNo factors evaluated\n```"
        closest_line = "—"

    embed = {
        "title": f"{regime_emoji}  4H Heartbeat  ·  {symbol} {regime}",
        "description": f"_Bot scanning · {closest_line}_",
        "color": color,
        "fields": [
            {"name": "💰  Price",
             "value": f"**${price:,.0f}**",
             "inline": True},
            {"name": "📈  Regime",
             "value": f"**{regime}**",
             "inline": True},
            {"name": "😨  F&G",
             "value": f"**{fg}**",
             "inline": True},
            {"name": "📡  Setup Proximity",
             "value": prox_text,
             "inline": False},
            {"name": "📊  Context",
             "value": (f"ATR(14): `${atr:,.0f}`  ·  "
                       f"Mode: `{mode}`  ·  "
                       f"Bot: `{bot_name}`"),
             "inline": False},
        ],
        "footer": {
            "text": "4H candle close · Bot heartbeat · Educational",
            "icon_url": _logo_for(symbol),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return embed


# ── Weekly Report embed ──────────────────────────────────────────────
def build_outcome_embed(bot: str, system: str, symbol: str,
                        status: str, r: float,
                        entry: float, sl: float, tp: float,
                        opened_at: str, exit_time: str = "",
                        bars_held: int = 0) -> Dict:
    """Embed for a resolved forward-test trade outcome (TP/SL/TIME)."""
    if status == "TP":
        color = COLOR_LONG; emoji = "✅"; verdict = "WIN"
    elif status == "SL":
        color = COLOR_SHORT; emoji = "❌"; verdict = "LOSS"
    elif status == "BE":
        color = COLOR_NEUTRAL; emoji = "🟰"; verdict = "BREAKEVEN"
    else:
        color = COLOR_WARNING; emoji = "⏰"; verdict = "TIME-STOP"

    r_sign = "+" if r >= 0 else ""
    embed = {
        "title": f"{emoji}  TRADE CLOSED  ·  {verdict}  ·  {r_sign}{r:.2f}R",
        "description": f"`{bot}/{system}` on **{symbol}** · forward-test outcome",
        "color": color,
        "fields": [
            {"name": "📍 Entry",      "value": f"```\n${entry:,.0f}\n```",   "inline": True},
            {"name": "🛑 Stop Loss",  "value": f"```\n${sl:,.0f}\n```",      "inline": True},
            {"name": "🎯 Take Profit","value": f"```\n${tp:,.0f}\n```",      "inline": True},
            {"name": "📊 Outcome",    "value": f"**{status}**", "inline": True},
            {"name": "💵 Result",     "value": f"**{r_sign}{r:.2f}R**", "inline": True},
            {"name": "⏱ Bars Held",  "value": f"**{bars_held}**", "inline": True},
            {"name": "🕓 Window",
             "value": f"Opened: `{opened_at[:16]}`\nExited: `{exit_time[:16] if exit_time else 'now'}`",
             "inline": False},
        ],
        "footer": {
            "text": f"Forward-test ledger · live price action · educational",
            "icon_url": _logo_for(symbol),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return embed


def build_daily_summary_embed(date_label: str,
                               scans: int, qualifying: int, alerts: int,
                               max_score: int, closest_factor: str = "",
                               regime: str = "BULL",
                               price: float = 0, fg: int = 50) -> Dict:
    """Compact daily heartbeat — a quick day-in-the-life recap."""
    color = COLOR_LONG if regime == "BULL" else COLOR_SHORT if regime == "BEAR" else COLOR_NEUTRAL
    activity = "🔥 active" if alerts else ("👀 watching" if qualifying else "💤 quiet")

    embed = {
        "title": f"🗓  Daily Recap  ·  {date_label}",
        "description": f"_Bot status: {activity} · regime: **{regime}**_",
        "color": color,
        "fields": [
            {"name": "🔍  Scans",          "value": f"**{scans}**",            "inline": True},
            {"name": "✅  Qualifying",     "value": f"**{qualifying}**",       "inline": True},
            {"name": "🎯  Alerts fired",   "value": f"**{alerts}**",           "inline": True},
            {"name": "📈  Max score (today)", "value": f"**{max_score}/10**",  "inline": True},
            {"name": "💰  BTC last",       "value": f"**${price:,.0f}**",      "inline": True},
            {"name": "😨  F&G",            "value": f"**{fg}**",               "inline": True},
        ],
        "footer": {"text": "Daily recap · Educational · Mode: PAPER",
                   "icon_url": BTC_LOGO},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if closest_factor:
        embed["fields"].append({
            "name": "🚨  Closest to firing today",
            "value": closest_factor,
            "inline": False,
        })
    return embed


def build_weekly_embed(period_label: str,
                       scans_total: int,
                       alerts_fired: int,
                       wins: int = 0, losses: int = 0,
                       total_r: float = 0, max_dd_r: float = 0,
                       close_calls: int = 0,
                       what_if_text: str = "",
                       conclusion: str = "") -> Dict:
    """Build weekly retrospective embed."""
    wr = (wins / alerts_fired * 100) if alerts_fired else 0
    pf_text = "—"
    if losses > 0 and wins > 0:
        pf_est = wins * 2.5 / max(1, losses)   # rough estimate; real calc needs R values
        pf_text = f"~{pf_est:.2f}"
    elif alerts_fired == 0:
        pf_text = "no trades"

    activity_text = (
        f"Scans: **{scans_total:,}**\n"
        f"Alerts fired: **{alerts_fired}**\n"
        f"Wins (TP): **{wins}**  ·  Losses (SL): **{losses}**\n"
        f"Win rate: **{wr:.1f}%**" if alerts_fired else
        f"Scans: **{scans_total:,}**\n"
        f"Alerts fired: **0**\n"
        f"_(no trades this week — bot waiting for valid setups)_"
    )

    fields = [
        {"name": "📊  Activity",
         "value": activity_text,
         "inline": True},
        {"name": "💰  Performance",
         "value": (f"Total R: **{total_r:+.2f}**\n"
                   f"Max DD: **{max_dd_r:.2f}R**\n"
                   f"PF estimate: **{pf_text}**"),
         "inline": True},
    ]

    if close_calls > 0:
        fields.append({
            "name": "🎯  Close Calls",
            "value": f"Setups that scored 1pt below threshold: **{close_calls}**",
            "inline": False,
        })

    if what_if_text:
        fields.append({
            "name": "🔬  What-If Analysis",
            "value": f"```\n{what_if_text[:900]}\n```",
            "inline": False,
        })

    if conclusion:
        fields.append({
            "name": "✅  Verdict",
            "value": conclusion,
            "inline": False,
        })

    embed = {
        "title": f"📅  Weekly Recap  ·  {period_label}",
        "description": "_Bot performance summary · automated retrospective_",
        "color": COLOR_WEEKLY,
        "fields": fields,
        "footer": {
            "text": "Weekly auto-report · Sundays 23:00 UTC · Educational",
            "icon_url": BTC_LOGO,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return embed


# ── Send via webhook ─────────────────────────────────────────────────
def send_embed(webhook_url: str, embed: Dict, username: str = "Pro Signal",
               image_bytes: Optional[bytes] = None,
               image_name: str = "chart.png") -> bool:
    """POST one embed to a Discord webhook. If image_bytes provided,
    sends as multipart attachment and references the image inside the embed."""
    if not webhook_url:
        return False

    if image_bytes:
        # multipart/form-data with embed referencing attached file
        embed = dict(embed)
        embed["image"] = {"url": f"attachment://{image_name}"}
        payload_json = json.dumps({
            "username": username,
            "avatar_url": BOT_AVATAR,
            "embeds": [embed],
        })
        # Hand-build multipart form (urllib doesn't have a clean helper)
        import secrets
        boundary = "----PFB" + secrets.token_hex(8)
        body = io_multipart(boundary, payload_json, image_bytes, image_name)
        req = urllib.request.Request(
            webhook_url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "User-Agent": "ProSignalFormatter/1.0"})
    else:
        payload = {
            "username": username,
            "avatar_url": BOT_AVATAR,
            "embeds": [embed],
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "ProSignalFormatter/1.0",
        })

    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 204)
    except Exception as e:
        print("[pro_format] send failed: %s" % e)
        return False


def io_multipart(boundary: str, payload_json: str,
                 image_bytes: bytes, image_name: str) -> bytes:
    """Hand-build multipart/form-data body for Discord webhook image upload."""
    crlf = b"\r\n"
    parts = []
    parts.append(f"--{boundary}".encode())
    parts.append(b'Content-Disposition: form-data; name="payload_json"')
    parts.append(b"Content-Type: application/json")
    parts.append(b"")
    parts.append(payload_json.encode("utf-8"))

    parts.append(f"--{boundary}".encode())
    parts.append(f'Content-Disposition: form-data; name="files[0]"; filename="{image_name}"'.encode())
    parts.append(b"Content-Type: image/png")
    parts.append(b"")
    body = crlf.join(parts) + crlf + image_bytes + crlf + f"--{boundary}--".encode() + crlf
    return body
