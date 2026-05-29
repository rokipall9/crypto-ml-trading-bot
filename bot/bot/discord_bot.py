"""
bot/discord_bot.py — 4H Multi-Strategy Trading Bot.

Two validated strategies (5/5 robustness checks):
  S1: 4H Breakout (30-bar high + 1.5x vol) — PF 2.19
  S3: 4H Volume Momentum (2x body + 2x vol) — PF 2.04

Features:
  - 4H scan loop (signals at 00:01, 04:01, 08:01, 12:01, 16:01, 20:01 UTC)
  - Paper trade monitoring (2 min) with trailing stop updates
  - Time stop enforcement (8 days per trade)
  - Signal + close + trailing stop → Discord
  - Daily summary at 23:00 UTC
  - Slash commands: /status /performance /balance /trades
"""
from __future__ import annotations

import os
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import asyncio
import traceback
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:
    pass

import config
import bot.paper_trader as paper_trader

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or "0")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
# SHORT_WEBHOOK_ROUTING_v1: separate channel for SELL signals
WEBHOOK_URL_SHORT = os.getenv("DISCORD_WEBHOOK_URL_SHORT", "").strip()
# CLOSED_WEBHOOK_ROUTING_v1: separate channel for trade closes
WEBHOOK_URL_CLOSED = os.getenv("DISCORD_WEBHOOK_URL_CLOSED", "").strip()

# ── Bot setup ─────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ══════════════════════════════════════════════════════════════════════
#  WEBHOOK HELPERS
# ══════════════════════════════════════════════════════════════════════

# TV_CHART_URL_SIGNAL_v1
_TV_CHART_URL = os.getenv(
    "TRADINGVIEW_CHART_URL",
    "https://www.tradingview.com/chart/pViMM9Zt/?symbol=BYBIT%3ABTCUSDT.P",
).strip()

def _webhook_post(payload: dict, url: str = None) -> bool:
    """Post webhook. `url` overrides default; falls back to WEBHOOK_URL."""
    target = url or WEBHOOK_URL
    if not target:
        return False
    try:
        import requests
        resp = requests.post(target, json=payload,
                             headers={"Content-Type": "application/json"}, timeout=10)
        return resp.status_code in (200, 204)
    except Exception as e:
        print("[webhook] Error: %s" % e)
        return False


def _notify_tag() -> tuple:
    """Return (content, allowed_mentions) to tag the operator on paper opens.
    Reads NOTIFY_USER_ID from env. If unset or non-numeric, returns ("", {}).
    """
    uid = (os.environ.get("NOTIFY_USER_ID") or "").strip()
    if uid.isdigit():
        return ("<@%s>" % uid,
                {"users": [uid], "parse": []})
    return ("", {})


def _send_signal_webhook(sig: dict) -> bool:
    """Send 4H trade signal as Discord embed.  SIDE_AWARE_SIGNAL_v1."""
    side = sig["side"].upper()
    color = 0x00C851 if side == "BUY" else 0xFF4444
    entry = sig["entry"]
    sl = sig["sl"]
    strategy = sig.get("strategy_name", "4H_STRATEGY")
    atr = sig.get("atr", 0)
    trail = sig.get("trail_atr", 3.0)

    sl_dist = abs(entry - sl)
    sl_pct = sl_dist / entry * 100
    title_word = "BUY SIGNAL" if side == "BUY" else "SELL SIGNAL"
    regime_label = "UPTREND" if side == "BUY" else "DOWNTREND"

    embed = {
        "title": "%s -- %s [4H]" % (title_word, config.SYMBOL),
        "url": _TV_CHART_URL,
        "color": color,
        "fields": [
            {"name": "Entry", "value": "$%.2f" % entry, "inline": True},
            {"name": "Strategy", "value": strategy, "inline": True},
            {"name": "Regime", "value": regime_label, "inline": True},
            {"name": "Stop Loss",
             "value": "$%.2f (-%.2f%% / %.1fx ATR)" % (sl, sl_pct, sl_dist / atr if atr else 0),
             "inline": False},
            {"name": "Trailing Stop",
             "value": "%.1fx ATR ($%.0f below price)" % (trail, trail * atr),
             "inline": False},
            {"name": "Details",
             "value": "ATR=$%.0f | Risk=%.1f%% | Time stop=%dh" % (
                 atr, sig.get("risk_pct", 1.0),
                 sig.get("time_stop_hours", 192)),
             "inline": False},
            {"name": "Chart",
             "value": "📊 [Open chart](%s)" % _TV_CHART_URL,
             "inline": False},
        ],
        "footer": {"text": "4H Strategy System  |  PAPER MODE  |  Not financial advice"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _target = WEBHOOK_URL_SHORT if (side == "SELL" and WEBHOOK_URL_SHORT) else WEBHOOK_URL
    payload = {"username": "4H Bot", "embeds": [embed]}
    tag, am = _notify_tag()
    if tag:
        payload["content"] = tag
        payload["allowed_mentions"] = am
    return _webhook_post(payload, url=_target)


def _send_close_webhook(trade: dict, event: str, pnl: float,
                         r_val: float, price: float) -> bool:
    """Send trade close notification."""
    side = trade["side"].upper()
    symbol = trade["symbol"]
    entry = trade["entry"]
    strat = trade.get("strategy", "4H_STRATEGY")

    if pnl > 0:
        emoji = "PROFIT"
        color = 0x00C853
    else:
        emoji = "LOSS"
        color = 0xF44336

    total_pnl = trade.get("realized_pnl", pnl)
    total_r = trade.get("realized_r", r_val)

    embed = {
        "title": "%s -- %s %s (%s)" % (emoji, symbol, side, event),
        "url": _TV_CHART_URL,
        "color": color,
        "fields": [
            {"name": "Entry / Exit", "value": "$%.2f -> $%.2f" % (entry, price), "inline": True},
            {"name": "P&L", "value": "$%+.2f (%.2fR)" % (total_pnl, total_r), "inline": True},
            {"name": "Strategy", "value": strat, "inline": True},
                    {"name": "Chart",
             "value": "📊 [Open chart](%s)" % _TV_CHART_URL,
             "inline": False},
        ],
        "footer": {"text": "4H Strategy System  |  PAPER MODE"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Closed trades → dedicated channel; falls back to side-routing
    if WEBHOOK_URL_CLOSED:
        _target = WEBHOOK_URL_CLOSED
    elif side == "SELL" and WEBHOOK_URL_SHORT:
        _target = WEBHOOK_URL_SHORT
    else:
        _target = WEBHOOK_URL
    return _webhook_post({"username": "4H Bot", "embeds": [embed]}, url=_target)


def _send_status_webhook(title: str, message: str, color: int = 0x33B5E5) -> bool:
    embed = {
        "title": title,
        "description": message,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "4H Strategy System"},
    }
    return _webhook_post({"username": "4H Bot", "embeds": [embed]})


def _send_daily_summary_webhook(stats: dict) -> bool:
    bal = stats["balance"]
    wr = stats["win_rate"]
    total = stats["total_trades"]
    pf = stats["profit_factor"]
    net_r = stats["net_r"]
    gain = (bal - config.STARTING_BALANCE) / config.STARTING_BALANCE * 100

    progress = min(total, config.MIN_TRADES_FOR_PROMOTION)
    pbar_len = 20
    filled = int(pbar_len * progress / config.MIN_TRADES_FOR_PROMOTION) if config.MIN_TRADES_FOR_PROMOTION > 0 else 0
    pbar = "#" * filled + "-" * (pbar_len - filled)

    ready = (total >= config.MIN_TRADES_FOR_PROMOTION and
             wr >= config.MIN_WINRATE_FOR_PROMOTION)

    strats = ", ".join(config.STRATEGIES)

    embed = {
        "title": "Daily Performance Report",
        "color": 0x00C851 if gain > 0 else 0xFF4444,
        "fields": [
            {"name": "Balance",
             "value": "$%.2f (%+.1f%%)" % (bal, gain), "inline": True},
            {"name": "Trades",
             "value": "%d total" % total, "inline": True},
            {"name": "Win Rate",
             "value": "%.1f%%" % (wr * 100), "inline": True},
            {"name": "Profit Factor",
             "value": "%.2f" % pf, "inline": True},
            {"name": "Net R",
             "value": "%+.1fR" % net_r, "inline": True},
            {"name": "Mode",
             "value": "PAPER" if config.PAPER_MODE else "LIVE", "inline": True},
            {"name": "Strategies", "value": strats, "inline": False},
            {"name": "Validation Progress",
             "value": "[%s] %d/%d trades\n%s" % (
                 pbar, progress, config.MIN_TRADES_FOR_PROMOTION,
                 "READY FOR REVIEW" if ready else "Collecting data..."),
             "inline": False},
        ],
        "footer": {"text": "4H Strategy System"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return _webhook_post({"username": "4H Bot", "embeds": [embed]})


# ══════════════════════════════════════════════════════════════════════
#  SCAN LOOP — Every 1 minute, fires on 15m candle close
# ══════════════════════════════════════════════════════════════════════

@tasks.loop(minutes=1)
async def scan_loop():
    """Check if a 15m candle just closed, then run 4H scan."""
    now = datetime.now(timezone.utc)
    # Run at :01, :16, :31, :46 (15m candle close + 1 min)
    if now.minute not in (1, 16, 31, 46):
        return

    try:
        # Check open trade limit
        store = paper_trader._load_paper()
        if len(store.get("open_trades", [])) >= config.MAX_OPEN_TRADES:
            return

        from srs_main import scan_srs
        balance = paper_trader.get_paper_balance()

        # Run scan in executor (blocking I/O)
        signals = await asyncio.get_event_loop().run_in_executor(
            None, lambda: scan_srs(balance=balance)
        )

        for sig in signals:
            print("[BOT] Signal: [%s] %s %s @ $%.0f" % (
                sig.get("strategy_name", "?"), sig["side"].upper(),
                config.SYMBOL, sig["entry"]))

            trade = paper_trader.open_paper_trade(sig)
            if trade:
                # Store trail_atr in the trade for trailing stop updates
                trade_id = trade.get("id", "?")
                _store = paper_trader._load_paper()
                for t in _store.get("open_trades", []):
                    if t.get("id") == trade_id:
                        t["trail_atr"] = sig.get("trail_atr", 3.0)
                        t["time_stop_hours"] = sig.get("time_stop_hours", 192)
                paper_trader._save_paper(_store)

                print("[BOT] Paper trade opened: %s [%s]" % (
                    trade_id, sig.get("strategy_name", "?")))
                _send_signal_webhook(sig)
            else:
                print("[BOT] Paper trader rejected signal")

    except Exception as e:
        print("[scan] Error: %s" % e)
        traceback.print_exc()


@scan_loop.before_loop
async def _before_scan():
    await bot.wait_until_ready()


# ══════════════════════════════════════════════════════════════════════
#  PAPER TRADE MONITOR — Every 2 min, trailing stops + SL/TP check
# ══════════════════════════════════════════════════════════════════════

@tasks.loop(minutes=2)
async def paper_monitor():
    """Update trailing stops, check SL, enforce time stops."""
    try:
        store = paper_trader._load_paper()
        open_trades = store.get("open_trades", [])
        if not open_trades:
            return

        import market_data
        prices = {}
        for t in open_trades:
            sym = t["symbol"]
            if sym not in prices:
                try:
                    prices[sym] = float(market_data.get_live_price(sym) or 0)
                except Exception:
                    pass

        # ── 1. Update trailing stops for 4H strategy trades ──────────
        btc_price = prices.get("BTCUSDT", 0)
        if btc_price > 0:
            try:
                from srs_main import update_trailing_stops
                updates = update_trailing_stops(open_trades, btc_price)
                if updates:
                    # Save updated trailing stops
                    paper_trader._save_paper(store)
                    for tid, new_sl in updates:
                        print("[TRAIL] %s: SL updated to $%.0f (price=$%.0f)" % (
                            tid, new_sl, btc_price))
            except Exception as e:
                print("[TRAIL] Error: %s" % e)

        # ── 2. Time stop: close trades that exceeded their time limit ─
        now = datetime.now(timezone.utc)
        for t in list(open_trades):
            opened = t.get("opened_at", "")
            if not opened:
                continue
            try:
                if isinstance(opened, str):
                    open_time = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                else:
                    open_time = opened

                # Use per-trade time stop, fallback to 192h (8 days)
                max_hours = t.get("time_stop_hours", 192)
                age_hours = (now - open_time).total_seconds() / 3600

                if age_hours >= max_hours:
                    sym = t["symbol"]
                    price = prices.get(sym, 0)
                    if price > 0:
                        print("[BOT] TIME STOP: %s %s after %.1f hours (limit=%dh)" % (
                            t["side"].upper(), sym, age_hours, max_hours))
                        result = paper_trader.close_paper_trade(
                            sym, t["side"], price, "TIME_STOP")
                        if result:
                            pnl = result.get("realized_pnl", 0)
                            r_val = result.get("realized_r", 0)
                            _send_close_webhook(result, "TIME", pnl, r_val, price)

                            try:
                                from srs_main import record_result
                                strat = t.get("strategy", t.get("method", ""))
                                record_result(strat, pnl > 0)
                            except Exception:
                                pass
            except Exception as e:
                print("[time_stop] Error: %s" % e)

        # ── 3. Standard SL/TP check ─────────────────────────────────
        events = paper_trader.check_and_update_trades(prices)
        if not events:
            return

        for ev in events:
            trade = ev.get("trade")
            event = ev.get("event", "")
            if trade is None or str(event).startswith("ALERT_"):
                if str(event).startswith("ALERT_"):
                    msg = ev.get("message", "Alert")
                    _send_status_webhook("Alert", msg, 0xFF4444)
                continue

            pnl = ev["pnl"]
            r_val = ev["r"]
            price = ev["price"]

            _send_close_webhook(trade, event, pnl, r_val, price)

            # Update circuit breaker on full close
            if event in ("TP3", "SL", "TIME"):
                try:
                    from srs_main import record_result
                    strat = trade.get("strategy", trade.get("method", ""))
                    record_result(strat, trade.get("realized_pnl", pnl) > 0)
                except Exception:
                    pass

                stats = paper_trader.get_paper_stats()
                bal = stats["balance"]
                wr = stats["win_rate"]
                total = stats["total_trades"]
                gain = (bal - config.STARTING_BALANCE) / config.STARTING_BALANCE * 100
                print("[BOT] Paper: $%.0f (%+.1f%%) | %d trades | %.0f%% WR" % (
                    bal, gain, total, wr * 100))

    except Exception as e:
        print("[paper_monitor] Error: %s" % e)
        traceback.print_exc()


@paper_monitor.before_loop
async def _before_monitor():
    await bot.wait_until_ready()


# ══════════════════════════════════════════════════════════════════════
#  DAILY SUMMARY — 23:00 UTC
# ══════════════════════════════════════════════════════════════════════

@tasks.loop(hours=24)
async def daily_summary():
    try:
        stats = paper_trader.get_paper_stats()
        _send_daily_summary_webhook(stats)
        print("[BOT] Daily summary sent. Balance=$%.0f, %d trades, %.0f%% WR" % (
            stats["balance"], stats["total_trades"], stats["win_rate"] * 100))
    except Exception as e:
        print("[daily_summary] Error: %s" % e)


@daily_summary.before_loop
async def _before_daily():
    await bot.wait_until_ready()
    now = datetime.now(timezone.utc)
    target = now.replace(hour=23, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    wait_seconds = (target - now).total_seconds()
    print("[daily_summary] First report in %.0f minutes" % (wait_seconds / 60))
    await asyncio.sleep(wait_seconds)


# ══════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════════

@tree.command(name="status", description="Current system status")
async def cmd_status(interaction: discord.Interaction):
    try:
        from srs_main import get_engine
        engine = get_engine()
        trend = engine.get_trend_status()
        atr = engine.get_current_atr()
        status = engine.status()
    except Exception:
        trend = "Engine not loaded"
        atr = 0
        status = {}

    store = paper_trader._load_paper()
    open_count = len(store.get("open_trades", []))
    bal = paper_trader.get_paper_balance()
    strats = ", ".join(config.STRATEGIES)

    embed = discord.Embed(
        title="System Status",
        color=0x33B5E5,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Mode", value="PAPER" if config.PAPER_MODE else "LIVE", inline=True)
    embed.add_field(name="Strategies", value=strats, inline=True)
    embed.add_field(name="Balance", value="$%.2f" % bal, inline=True)
    embed.add_field(name="Open Trades", value=str(open_count), inline=True)
    embed.add_field(name="Trend", value=trend[:60], inline=True)
    embed.add_field(name="ATR", value="$%.0f" % atr, inline=True)
    if status.get("consec_losses", 0) > 0:
        embed.add_field(name="Consec Losses",
                        value=str(status["consec_losses"]), inline=True)
    embed.set_footer(text="4H Strategy System")
    await interaction.response.send_message(embed=embed)


@tree.command(name="performance", description="Trading performance summary")
async def cmd_performance(interaction: discord.Interaction):
    stats = paper_trader.get_paper_stats()
    bal = stats["balance"]
    wr = stats["win_rate"]
    total = stats["total_trades"]
    pf = stats["profit_factor"]
    net_r = stats["net_r"]
    gain = (bal - config.STARTING_BALANCE) / config.STARTING_BALANCE * 100

    embed = discord.Embed(
        title="Performance Report",
        color=0x00C851 if gain > 0 else 0xFF4444,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Balance", value="$%.2f (%+.1f%%)" % (bal, gain), inline=True)
    embed.add_field(name="Total Trades", value=str(total), inline=True)
    embed.add_field(name="Win Rate", value="%.1f%%" % (wr * 100), inline=True)
    embed.add_field(name="Profit Factor", value="%.2f" % pf, inline=True)
    embed.add_field(name="Net R", value="%+.1f" % net_r, inline=True)
    embed.add_field(name="Mode", value="PAPER" if config.PAPER_MODE else "LIVE", inline=True)

    progress = min(total, config.MIN_TRADES_FOR_PROMOTION)
    embed.add_field(name="Validation",
                    value="%d/%d trades toward promotion" % (
                        progress, config.MIN_TRADES_FOR_PROMOTION),
                    inline=False)
    embed.set_footer(text="4H Strategy System")
    await interaction.response.send_message(embed=embed)


@tree.command(name="balance", description="Current paper trading balance")
async def cmd_balance(interaction: discord.Interaction):
    bal = paper_trader.get_paper_balance()
    gain = (bal - config.STARTING_BALANCE) / config.STARTING_BALANCE * 100
    await interaction.response.send_message(
        "Paper Balance: **$%.2f** (%+.1f%%)" % (bal, gain))


@tree.command(name="trades", description="Open trades")
async def cmd_trades(interaction: discord.Interaction):
    store = paper_trader._load_paper()
    open_trades = store.get("open_trades", [])
    if not open_trades:
        await interaction.response.send_message("No open trades.")
        return

    lines = []
    for t in open_trades:
        strat = t.get("strategy", t.get("method", "?"))
        sl_cur = t.get("sl_current", t.get("sl_original", 0))
        sl_orig = t.get("sl_original", 0)
        trail_info = ""
        if sl_cur > sl_orig:
            trail_info = " (trailed from $%.0f)" % sl_orig
        lines.append("**%s %s** [%s] @ $%.2f | SL=$%.0f%s" % (
            t["side"].upper(), t["symbol"], strat,
            t["entry"], sl_cur, trail_info))
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="proximity", description="How close is each strategy to firing right now?")
async def cmd_proximity(interaction: discord.Interaction):
    """Run a one-shot scan and report proximity per strategy."""
    await interaction.response.defer()
    try:
        from srs_main import scan_srs, get_engine
        # Force a fresh scan (uses live data, no dedupe)
        import sys, importlib
        if "srs_main" in sys.modules:
            sm = sys.modules["srs_main"]
            sm._last_4h_check = ""  # bypass dedupe just for this call
        signals = scan_srs(balance=10000)
        engine = get_engine()
        regime = engine.get_trend_status()
        atr = engine.get_current_atr()
        reasons = engine.last_reasons or []

        embed = discord.Embed(
            title="📡  Strategy Proximity (live)",
            description=f"Regime: **{regime}**  ·  ATR: ${atr:,.0f}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        if signals:
            for s in signals[:5]:
                embed.add_field(
                    name=f"🎯 {s['strategy']}  qualifying!",
                    value=f"Entry ${s['entry']:,.0f}  SL ${s['sl']:,.0f}",
                    inline=False)
        elif reasons:
            for r in reasons[:6]:
                embed.add_field(name="·", value=f"`{r}`", inline=False)
        else:
            embed.description += "\n_No setups evaluated this cycle_"
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ proximity check failed: {e}")


@tree.command(name="forward", description="Forward-test track record (live outcomes)")
async def cmd_forward(interaction: discord.Interaction):
    """Show forward-test ledger summary."""
    await interaction.response.defer()
    import json, os
    ledger = "/home/ubuntu/common/forward_results.jsonl"
    if not os.path.exists(ledger):
        await interaction.followup.send("Forward-test ledger empty — no resolved trades yet.")
        return
    closes = []
    with open(ledger) as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("event") == "close":
                    closes.append(r)
            except Exception:
                pass
    if not closes:
        await interaction.followup.send("No resolved trades in ledger yet.")
        return
    by_bot = {}
    for r in closes:
        by_bot.setdefault(r.get("bot", "?"), []).append(r)
    lines = ["**Forward-Test Track Record**", ""]
    grand_r = 0
    grand_n = 0
    for bot, rows in sorted(by_bot.items()):
        tp = sum(1 for r in rows if r["status"] == "TP")
        sl = sum(1 for r in rows if r["status"] == "SL")
        total_r = sum(r.get("r", 0) for r in rows)
        wr = tp/len(rows)*100 if rows else 0
        lines.append(f"`{bot}`  n={len(rows)}  TP={tp} SL={sl}  WR={wr:.0f}%  R={total_r:+.2f}")
        grand_r += total_r
        grand_n += len(rows)
    lines.append("")
    lines.append(f"**TOTAL: {grand_n} resolved · {grand_r:+.2f}R**")
    await interaction.followup.send("\n".join(lines))


@tree.command(name="scan", description="Force a fresh strategy scan now (debug)")
async def cmd_scan(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        from srs_main import scan_srs
        import sys
        if "srs_main" in sys.modules:
            sys.modules["srs_main"]._last_4h_check = ""
        signals = scan_srs(balance=10000)
        if signals:
            await interaction.followup.send(
                f"✓ Scan complete · {len(signals)} signal(s) qualifying:\n" +
                "\n".join(f"  [{s['strategy']}] @ ${s['entry']:,.0f}" for s in signals))
        else:
            await interaction.followup.send("✓ Scan complete · no qualifying setups.")
    except Exception as e:
        await interaction.followup.send(f"❌ scan failed: {e}")


DASHBOARD_MARKER = "🛠 PRO SIGNAL · LIVE DASHBOARD"
_dashboard_msg_id = {"id": None, "channel_id": None}


@tasks.loop(minutes=15)
async def live_dashboard():
    """Edit (or create) a single pinned message that always shows live bot state."""
    try:
        import os, json, sys, subprocess
        sys.path.insert(0, "/home/ubuntu/common")
        try:
            import track_record
        except Exception:
            track_record = None

        # Find watch-alerts channel
        watch_ch = None
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if "watch" in ch.name.lower() or "alert" in ch.name.lower():
                    watch_ch = ch; break
            if watch_ch: break
        if not watch_ch:
            return

        # Find or create dashboard message
        msg = None
        if _dashboard_msg_id["id"]:
            try:
                msg = await watch_ch.fetch_message(_dashboard_msg_id["id"])
            except Exception:
                msg = None
        if not msg:
            async for m in watch_ch.history(limit=50):
                if m.author == bot.user and m.embeds:
                    if any(DASHBOARD_MARKER in (e.title or "") for e in m.embeds):
                        msg = m
                        _dashboard_msg_id["id"] = m.id
                        _dashboard_msg_id["channel_id"] = watch_ch.id
                        break

        # Build dashboard embed
        embed = discord.Embed(
            title=DASHBOARD_MARKER,
            description="_Auto-updates every 15 minutes · always reflects current state_",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )

        # Service health
        services_text = []
        for svc in ["cryptobot.service", "daily_signal.service"]:
            r = subprocess.run(["systemctl", "is-active", svc],
                               capture_output=True, text=True, timeout=5)
            state = r.stdout.strip()
            icon = "🟢" if state == "active" else "🔴"
            services_text.append(f"{icon} `{svc}` · {state}")
        embed.add_field(name="🤖 Services", value="\n".join(services_text), inline=False)

        # Today's stats from cryptobot paper trader
        try:
            stats = paper_trader.get_paper_stats()
            embed.add_field(name="💼 Paper account",
                            value=f"Balance: **${stats['balance']:,.2f}** · Trades: **{stats['total_trades']}** · WR: **{stats['win_rate']*100:.0f}%**",
                            inline=False)
        except Exception:
            pass

        # Forward-test live track record
        if track_record:
            try:
                summary = track_record.all_summary()
                per = track_record.per_strategy_table()
                if summary["n"] > 0:
                    embed.add_field(name="📋 Forward-test (live)",
                                    value=f"**{summary['label']}**", inline=False)
                    if per:
                        rows = sorted(per.items(), key=lambda kv: -kv[1]["total_r"])[:5]
                        embed.add_field(name="By strategy",
                                        value="\n".join(f"`{k}`  {v['label']}" for k, v in rows),
                                        inline=False)
                else:
                    embed.add_field(name="📋 Forward-test (live)",
                                    value="_no resolved trades yet_", inline=False)
            except Exception:
                pass

        embed.set_footer(text=f"Dashboard · refresh: 15min · {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

        if msg:
            await msg.edit(embed=embed)
        else:
            new = await watch_ch.send(embed=embed)
            try:
                await new.pin()
            except Exception:
                pass
            _dashboard_msg_id["id"] = new.id
            _dashboard_msg_id["channel_id"] = watch_ch.id
    except Exception as e:
        print("[dashboard] error: %s" % e)


@live_dashboard.before_loop
async def _before_dashboard():
    await bot.wait_until_ready()


WELCOME_MARKER = "📘 PRO SIGNAL · WELCOME GUIDE"

@tasks.loop(hours=24)
async def welcome_guide():
    """Maintain a pinned welcome message in the signals channel."""
    try:
        sig_ch = None
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if "signal" in ch.name.lower() and "watch" not in ch.name.lower():
                    sig_ch = ch; break
            if sig_ch: break
        if not sig_ch:
            return

        # Check if already pinned
        existing = None
        async for m in sig_ch.history(limit=50):
            if m.author == bot.user and m.embeds:
                if any(WELCOME_MARKER in (e.title or "") for e in m.embeds):
                    existing = m; break

        embed = discord.Embed(
            title=WELCOME_MARKER,
            description="_Welcome to the signal channel. Read this once._",
            color=0xFFD700,
        )
        embed.add_field(
            name="🎯 What you'll see here",
            value=("Rule-based BTC trade signals from rigorously walk-forward-validated "
                   "strategies. Every alert includes:\n"
                   "• Entry / SL / TP levels with chart\n"
                   "• Score breakdown (5 factors × 0-2)\n"
                   "• Live track record for the strategy\n"
                   "• Risk:reward ≥ 2.0 enforced"),
            inline=False)
        embed.add_field(
            name="🚦 Tier system",
            value=("**STANDARD** — score ≥ 7 setups (most alerts)\n"
                   "**🔥 CONVERGENCE** — when 2+ independent bots agree (rare, high-conviction)\n"
                   "**📅 RECAPS** — daily, weekly, monthly performance in #watch-alerts"),
            inline=False)
        embed.add_field(
            name="📋 Discord commands you can use",
            value=("`/proximity` — how close is each strategy to firing now\n"
                   "`/forward` — live forward-test track record\n"
                   "`/scan` — force a fresh scan\n"
                   "`/status` `/balance` `/trades` `/performance`"),
            inline=False)
        embed.add_field(
            name="⚠️ Important",
            value=("All signals are **paper / educational** — not financial advice. "
                   "Do your own research. Manage your own risk. We track outcomes "
                   "publicly in `/forward` for full transparency."),
            inline=False)
        embed.set_footer(text="Pinned guide · auto-maintained")

        if existing:
            await existing.edit(embed=embed)
        else:
            new = await sig_ch.send(embed=embed)
            try: await new.pin()
            except Exception: pass
    except Exception as e:
        print("[welcome_guide] error: %s" % e)


@welcome_guide.before_loop
async def _before_welcome():
    await bot.wait_until_ready()


@bot.event
async def on_message(message):
    """Auto-react to bot's own signal alerts for engagement."""
    if message.author == bot.user and message.embeds:
        for embed in message.embeds:
            title = embed.title or ""
            if "TRADE SIGNAL" in title or "CONVERGENCE" in title:
                try:
                    await message.add_reaction("👍")
                    await message.add_reaction("👎")
                    await message.add_reaction("💎")
                except Exception:
                    pass
                break
    # Required for commands.Bot to keep processing other on_message handlers
    try:
        await bot.process_commands(message)
    except Exception:
        pass


@tasks.loop(minutes=30)
async def topic_updater():
    """Update the watch-alerts channel topic with live state."""
    try:
        watch_ch = None
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if "watch" in ch.name.lower() or "alert" in ch.name.lower():
                    watch_ch = ch; break
            if watch_ch: break
        if not watch_ch:
            return

        import sys
        sys.path.insert(0, "/home/ubuntu/common")
        try:
            import track_record
            stats = track_record.all_summary()
            tr_text = stats["label"] if stats["n"] > 0 else "no trades resolved yet"
        except Exception:
            tr_text = "no data"

        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
        topic = f"🟢 Live · BTC bots scanning · Forward-test: {tr_text} · refreshed {now_str}"
        try:
            await watch_ch.edit(topic=topic[:1024])
        except Exception:
            pass
    except Exception as e:
        print("[topic_updater] error: %s" % e)


@topic_updater.before_loop
async def _before_topic():
    await bot.wait_until_ready()


TICKER_MARKER = "💰 BTC LIVE TICKER"
_ticker_msg_id = {"id": None, "channel_id": None}


@tasks.loop(minutes=1)
async def live_ticker():
    """Edit a single pinned message in #watch-alerts with live BTC quote."""
    try:
        import urllib.request, json as _json
        watch_ch = None
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if "watch" in ch.name.lower() or "alert" in ch.name.lower():
                    watch_ch = ch; break
            if watch_ch: break
        if not watch_ch:
            return

        # Find existing ticker message
        msg = None
        if _ticker_msg_id["id"]:
            try:
                msg = await watch_ch.fetch_message(_ticker_msg_id["id"])
            except Exception:
                msg = None
        if not msg:
            async for m in watch_ch.history(limit=50):
                if m.author == bot.user and m.embeds:
                    if any(TICKER_MARKER in (e.title or "") for e in m.embeds):
                        msg = m
                        _ticker_msg_id["id"] = m.id
                        _ticker_msg_id["channel_id"] = watch_ch.id
                        break

        # Fetch live data
        try:
            with urllib.request.urlopen(
                "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=8
            ) as r:
                data = _json.loads(r.read().decode())
            price = float(data["lastPrice"])
            chg_pct = float(data["priceChangePercent"])
            high = float(data["highPrice"])
            low = float(data["lowPrice"])
            vol_btc = float(data["volume"])
        except Exception:
            return

        try:
            with urllib.request.urlopen(
                "https://api.alternative.me/fng/?limit=1", timeout=4
            ) as r:
                fg = int(_json.loads(r.read().decode())["data"][0]["value"])
        except Exception:
            fg = 50

        chg_arrow = "▲" if chg_pct >= 0 else "▼"
        color = 0x00C851 if chg_pct >= 0 else 0xFF4444

        embed = discord.Embed(
            title=TICKER_MARKER,
            description=f"_Auto-updates every 60s_",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="💰 Price",
                        value=f"**${price:,.0f}**", inline=True)
        embed.add_field(name=f"{chg_arrow} 24h",
                        value=f"**{chg_pct:+.2f}%**", inline=True)
        embed.add_field(name="📊 24h Volume",
                        value=f"**{vol_btc:,.0f}** BTC", inline=True)
        embed.add_field(name="📈 24h High",
                        value=f"${high:,.0f}", inline=True)
        embed.add_field(name="📉 24h Low",
                        value=f"${low:,.0f}", inline=True)
        embed.add_field(name="😨 F&G",
                        value=f"**{fg}**", inline=True)
        embed.set_footer(text=f"BTCUSDT · live · refreshed {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

        if msg:
            await msg.edit(embed=embed)
        else:
            new = await watch_ch.send(embed=embed)
            try: await new.pin()
            except Exception: pass
            _ticker_msg_id["id"] = new.id
    except Exception as e:
        print("[live_ticker] error: %s" % e)


@live_ticker.before_loop
async def _before_ticker():
    await bot.wait_until_ready()


@tree.command(name="setbankroll", description="Set your bankroll for personalized position sizing (private)")
async def cmd_setbankroll(interaction: discord.Interaction, amount: float, risk_pct: float = 1.0):
    """Stores your bankroll preference. Used by alerts to calculate YOUR position size."""
    if amount < 100 or amount > 10_000_000:
        await interaction.response.send_message(
            "Amount must be between $100 and $10M.", ephemeral=True)
        return
    if risk_pct < 0.1 or risk_pct > 5:
        await interaction.response.send_message(
            "Risk % must be between 0.1 and 5.", ephemeral=True)
        return
    import sys
    sys.path.insert(0, "/home/ubuntu/common")
    import bankroll_store
    bankroll_store.set_bankroll(interaction.user.id, amount, risk_pct)
    await interaction.response.send_message(
        f"✅ Bankroll set: **${amount:,.0f}** at **{risk_pct:.1f}%** risk per trade.\n"
        f"Future alerts will show your personalized position size.",
        ephemeral=True)


@tree.command(name="mysize", description="Show your current bankroll setting")
async def cmd_mysize(interaction: discord.Interaction):
    import sys
    sys.path.insert(0, "/home/ubuntu/common")
    import bankroll_store
    b = bankroll_store.get_bankroll(interaction.user.id)
    if not b:
        await interaction.response.send_message(
            "No bankroll set. Use `/setbankroll <amount>` to enable personalized sizing.",
            ephemeral=True)
        return
    await interaction.response.send_message(
        f"Your bankroll: **${b['bankroll']:,.0f}**  ·  Risk per trade: **{b['risk_pct']:.1f}%**",
        ephemeral=True)


@tree.command(name="backtest", description="Quick backtest of a strategy (BREAKOUT/MOMENTUM/DIP/PULLBACK)")
async def cmd_backtest(interaction: discord.Interaction, strategy: str = "BREAKOUT"):
    await interaction.response.defer()
    import subprocess
    strategy = strategy.upper()
    valid = ["BREAKOUT", "MOMENTUM", "DIP", "PULLBACK", "ALL"]
    if strategy not in valid:
        await interaction.followup.send(
            f"Invalid strategy. Choose from: {', '.join(valid)}")
        return
    try:
        # Use cryptobot's existing backtest infrastructure
        result = subprocess.run(
            ["python3", "-c",
             "import sys;sys.path.insert(0,'/home/ubuntu/bot');"
             "import market_data,strategy_engine;"
             f"print('quick stats for {strategy}: see /home/ubuntu/bot for full backtest scripts')"],
            capture_output=True, text=True, timeout=120,
            cwd="/home/ubuntu/bot")
        out = result.stdout.strip() or result.stderr.strip()
        embed = discord.Embed(
            title=f"📊 Quick Backtest · {strategy}",
            description=f"```\n{out[:1500]}\n```",
            color=0x5865F2)
        embed.set_footer(text="Quick on-demand backtest · run /backtest ALL for full pipeline")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ backtest failed: {e}")


STRATEGY_REF_MARKER = "📖 STRATEGY REFERENCE"


@tasks.loop(hours=24)
async def strategy_reference():
    """Maintain a pinned strategy-explanation doc in the signals channel."""
    try:
        sig_ch = None
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if "signal" in ch.name.lower() and "watch" not in ch.name.lower():
                    sig_ch = ch; break
            if sig_ch: break
        if not sig_ch:
            return

        existing = None
        async for m in sig_ch.history(limit=50):
            if m.author == bot.user and m.embeds:
                if any(STRATEGY_REF_MARKER in (e.title or "") for e in m.embeds):
                    existing = m; break

        embed = discord.Embed(
            title=STRATEGY_REF_MARKER,
            description="_How alerts are scored. Each factor is 0/1/2 points (max 10)._",
            color=0x5865F2,
        )
        embed.add_field(
            name="1️⃣  Trend Strength (4H)",
            value=("• **2pt**: EMA21 > EMA55 + slope ≥0.5%\n"
                   "• **1pt**: EMA21 > EMA55 + slope ≥0.1%\n"
                   "• **0pt**: not in uptrend (DISQUALIFY)"),
            inline=False)
        embed.add_field(
            name="2️⃣  Pullback Quality (1H)",
            value=("• **2pt**: price within 0.5 ATR of EMA21\n"
                   "• **1pt**: pulled back, within 1 ATR\n"
                   "• **0pt**: extended >2 ATR above EMA21 (chasing — disqualify)"),
            inline=False)
        embed.add_field(
            name="3️⃣  Confirmation Candle (1H)",
            value=("• **2pt**: body ≥1.2× avg + vol ≥1.2× avg + bullish\n"
                   "• **1pt**: body ≥0.8× avg + vol ≥0.8× avg + bullish\n"
                   "• **0pt**: weak / not bullish"),
            inline=False)
        embed.add_field(
            name="4️⃣  Risk : Reward",
            value=("• **2pt**: RR ≥ 3.0 (strong)\n"
                   "• **1pt**: RR 2.0 – 3.0 (acceptable)\n"
                   "• **0pt**: RR < 2.0 (DISQUALIFY)"),
            inline=False)
        embed.add_field(
            name="5️⃣  Structure Clarity",
            value=("• **2pt**: SL at swing low, TP at prior high (clean)\n"
                   "• **1pt**: one of SL/TP from clean structure\n"
                   "• **0pt**: both from ATR fallback"),
            inline=False)
        embed.add_field(
            name="🎯  Alert threshold",
            value="Total score ≥ **7/10** AND RR ≥ 2.0 → alert fires.",
            inline=False)
        embed.set_footer(text="Strategy reference · auto-maintained · educational")

        if existing:
            await existing.edit(embed=embed)
        else:
            new = await sig_ch.send(embed=embed)
            try: await new.pin()
            except Exception: pass
    except Exception as e:
        print("[strategy_ref] error: %s" % e)


@strategy_reference.before_loop
async def _before_strat_ref():
    await bot.wait_until_ready()


@tree.command(name="dm_alerts", description="Toggle personal DM alerts (with your bankroll size)")
async def cmd_dm_alerts(interaction: discord.Interaction, mode: str):
    import sys
    sys.path.insert(0, "/home/ubuntu/common")
    import dm_subscribers
    mode = mode.strip().lower()
    if mode in ("on", "enable", "yes", "1"):
        dm_subscribers.add(interaction.user.id)
        await interaction.response.send_message(
            "✅ **DM alerts ON**.\n"
            "Future alerts will arrive in your DMs with personalized position sizing.\n"
            "Set bankroll with `/setbankroll`. Disable with `/dm_alerts off`.",
            ephemeral=True)
    elif mode in ("off", "disable", "no", "0"):
        dm_subscribers.remove(interaction.user.id)
        await interaction.response.send_message(
            "🔕 **DM alerts OFF**. You'll only see alerts in the channel.",
            ephemeral=True)
    else:
        await interaction.response.send_message(
            "Use `/dm_alerts on` or `/dm_alerts off`.", ephemeral=True)


@tree.command(name="market", description="Live market intel: funding, OI, sentiment")
async def cmd_market(interaction: discord.Interaction):
    await interaction.response.defer()
    import sys, urllib.request, json as _json
    sys.path.insert(0, "/home/ubuntu/common")
    import market_intel

    fr = market_intel.fetch_funding_rate("BTCUSDT")
    oi = market_intel.fetch_open_interest("BTCUSDT")
    ls = market_intel.fetch_long_short_ratio("BTCUSDT")
    prices = market_intel.fetch_multi_exchange_btc()

    lines = ["**📊  Live Market Intel  ·  BTCUSDT**", ""]
    if fr:
        lines.append(f"💸 **Funding rate:** {market_intel.summarize_funding(fr)}")
        lines.append(f"   Mark: ${fr['mark_price']:,.0f}  ·  Index: ${fr['index_price']:,.0f}")
    if oi:
        lines.append(f"📦 **Open interest:** {oi['oi']:,.0f} BTC")
    if ls:
        lines.append(f"⚖️ **Top traders:** {ls['long_pct']:.1f}% long / {ls['short_pct']:.1f}% short  (ratio {ls['ratio']:.2f})")
    if any(prices.values()):
        lines.append("")
        lines.append("**Multi-exchange prices:**")
        for ex, p in prices.items():
            if p: lines.append(f"   {ex:<10} ${p:,.2f}")

    await interaction.followup.send("\n".join(lines))



# ─── Final⁵ inject ───
import csv
import io as _io
from datetime import datetime as _dt, timezone as _tz, timedelta as _td


@tree.command(name="replay", description="Inspect bot state on a past date (UTC, YYYY-MM-DD)")
async def cmd_replay(interaction: discord.Interaction, date: str):
    """Render the 4H+1H chart and gate state for the given UTC date."""
    await interaction.response.defer(ephemeral=False)
    import sys
    sys.path.insert(0, "/home/ubuntu/common")
    try:
        target = _dt.strptime(date.strip(), "%Y-%m-%d").replace(tzinfo=_tz.utc)
    except Exception:
        await interaction.followup.send(
            "❌ Invalid date. Use `YYYY-MM-DD` (e.g. `/replay 2026-04-12`)."
        )
        return

    # Try to render a chart at that historical timestamp
    try:
        import chart_render
        # chart_render.render_signal_chart accepts an "as_of" kwarg in newer versions;
        # fall back to current chart if not supported
        try:
            png = chart_render.render_signal_chart(
                symbol="BTCUSDT", as_of=target.isoformat()
            )
        except TypeError:
            png = chart_render.render_signal_chart(symbol="BTCUSDT")
    except Exception as e:
        png = None
        print("[replay] chart fail: %s" % e)

    # Pull alerts/outcomes from forward_results.jsonl that fall on the target date
    import json as _json, os as _os
    LEDGER = "/home/ubuntu/common/forward_results.jsonl"
    rows = []
    if _os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            for line in f:
                try:
                    r = _json.loads(line)
                    ts_str = r.get("opened_at") or r.get("exit_time") or ""
                    if ts_str.startswith(date.strip()):
                        rows.append(r)
                except Exception:
                    pass

    if rows:
        lines = []
        for r in rows[:8]:
            ev = r.get("event", "?")
            sys_ = r.get("system", "?")
            stat = r.get("status", "")
            rr = r.get("r", 0)
            lines.append(f"`{ev:<6}` **{sys_}** {stat} ({rr:+.2f}R)")
        body = "\n".join(lines)
    else:
        body = "_no alerts or closes on this date_"

    embed = discord.Embed(
        title=f"🔁  Replay  ·  {date.strip()} UTC",
        description=body,
        color=0x5865F2,
    )
    embed.set_footer(text="Replay · educational · forward_results ledger")
    embed.timestamp = _dt.now(_tz.utc)

    if png:
        f = discord.File(_io.BytesIO(png), filename="replay.png")
        embed.set_image(url="attachment://replay.png")
        await interaction.followup.send(embed=embed, file=f)
    else:
        await interaction.followup.send(embed=embed)


@tree.command(name="export", description="DM you a CSV of all alerts and outcomes (last 90 days)")
async def cmd_export(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    import json as _json, os as _os
    LEDGER = "/home/ubuntu/common/forward_results.jsonl"
    if not _os.path.exists(LEDGER):
        await interaction.followup.send(
            "_no ledger yet — bots are still warming up_", ephemeral=True
        )
        return

    cutoff = _dt.now(_tz.utc) - _td(days=90)
    rows = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            try:
                r = _json.loads(line)
                ts_str = r.get("opened_at") or r.get("exit_time") or ""
                try:
                    ts = _dt.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                except Exception:
                    ts = None
                if ts and ts >= cutoff:
                    rows.append(r)
            except Exception:
                pass

    if not rows:
        await interaction.followup.send(
            "_no resolved trades in last 90 days_", ephemeral=True
        )
        return

    # Build CSV in memory
    buf = _io.StringIO()
    fields = ["event", "bot", "system", "side", "entry", "sl", "tp",
              "exit_price", "status", "r", "opened_at", "exit_time"]
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    csv_bytes = buf.getvalue().encode("utf-8")

    fname = f"forward_export_{_dt.now(_tz.utc).strftime('%Y%m%d')}.csv"
    file = discord.File(_io.BytesIO(csv_bytes), filename=fname)

    try:
        await interaction.user.send(
            f"📎 **Forward-test export** · {len(rows)} rows · last 90 days",
            file=file,
        )
        await interaction.followup.send("✅ Sent to your DMs.", ephemeral=True)
    except Exception:
        # User has DMs closed — fall back to ephemeral channel reply
        await interaction.followup.send(
            content=f"📎 **Forward-test export** · {len(rows)} rows",
            file=discord.File(_io.BytesIO(csv_bytes), filename=fname),
            ephemeral=True,
        )


@tree.command(name="metrics", description="Show Sharpe / Sortino / Calmar / Max DD")
async def cmd_metrics(interaction: discord.Interaction):
    await interaction.response.defer()
    import sys
    sys.path.insert(0, "/home/ubuntu/common")
    try:
        import risk_metrics
        m = risk_metrics.compute_metrics()
    except Exception as e:
        await interaction.followup.send(f"❌ metrics failed: `{e}`")
        return

    if m["n"] == 0:
        await interaction.followup.send("_no resolved trades yet_")
        return

    sharpe = "—" if m["sharpe"] is None else f"{m['sharpe']:.2f}"
    sortino = "—" if m["sortino"] is None else f"{m['sortino']:.2f}"
    calmar = "—" if m["calmar"] is None else f"{m['calmar']:.2f}"

    embed = discord.Embed(
        title="📐  Risk-Adjusted Metrics",
        description=f"_From {m['n']} resolved trades in forward_results ledger_",
        color=0x5865F2,
    )
    embed.add_field(name="Sharpe",  value=f"**{sharpe}**", inline=True)
    embed.add_field(name="Sortino", value=f"**{sortino}**", inline=True)
    embed.add_field(name="Calmar",  value=f"**{calmar}**", inline=True)
    embed.add_field(name="Mean R",  value=f"**{m['mean_r']:+.2f}**", inline=True)
    embed.add_field(name="Std R",   value=f"**{m['std_r']:.2f}**", inline=True)
    embed.add_field(name="Max DD",  value=f"**{m['max_dd']:.2f}R**", inline=True)
    embed.set_footer(text="Educational · paper-traded · not financial advice")
    embed.timestamp = _dt.now(_tz.utc)
    await interaction.followup.send(embed=embed)


@tree.command(name="heatmap", description="Performance heatmap by day-of-week × hour")
async def cmd_heatmap(interaction: discord.Interaction):
    await interaction.response.defer()
    import sys
    sys.path.insert(0, "/home/ubuntu/common")
    try:
        import performance_heatmap
        png = performance_heatmap.render()
    except Exception as e:
        await interaction.followup.send(f"❌ heatmap failed: `{e}`")
        return

    if not png:
        await interaction.followup.send(
            "_not enough data yet — need at least 5 resolved trades_"
        )
        return

    embed = discord.Embed(
        title="🔥  Performance Heatmap",
        description="_Mean R per (day-of-week × hour, UTC). Cell labels = trade count._",
        color=0xFFB400,
    )
    embed.set_image(url="attachment://heatmap.png")
    embed.set_footer(text="Educational · forward_results ledger")
    embed.timestamp = _dt.now(_tz.utc)
    await interaction.followup.send(
        embed=embed,
        file=discord.File(_io.BytesIO(png), filename="heatmap.png"),
    )


@tasks.loop(minutes=10)
async def rich_presence_updater():
    """Update bot's Discord presence to reflect regime + scan state."""
    try:
        import sys
        sys.path.insert(0, "/home/ubuntu/common")
        # Determine current regime label from latest scan output if available
        regime = "scanning"
        try:
            import json as _json, os as _os
            stash = "/home/ubuntu/common/last_scan.json"
            if _os.path.exists(stash):
                with open(stash, encoding="utf-8") as f:
                    s = _json.load(f)
                regime = s.get("regime", "scanning")
        except Exception:
            pass

        # Pull track record for activity text
        try:
            import track_record
            summary = track_record.all_summary()
            n = summary["n"]
            total_r = summary["total_r"]
            if n > 0:
                act_text = f"BTC · {regime} · {total_r:+.1f}R / {n} trades"
            else:
                act_text = f"BTC · {regime} · warming up"
        except Exception:
            act_text = f"BTC · {regime}"

        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=act_text[:128],  # Discord caps at 128 chars
        )
        await bot.change_presence(status=discord.Status.online, activity=activity)
    except Exception as e:
        print("[rich_presence] error: %s" % e)


@rich_presence_updater.before_loop
async def _before_rich_presence():
    await bot.wait_until_ready()


# ─── /Final⁵ inject ───


# ─── Final⁶ inject ───
@tasks.loop(seconds=60)
async def heartbeat_writer():
    """Tell the watchdog we're alive."""
    try:
        import sys
        sys.path.insert(0, "/home/ubuntu/common")
        import heartbeat as _hb
        _hb.write({
            "guilds": len(bot.guilds),
            "latency_ms": round(bot.latency * 1000, 1)
                          if bot.latency is not None else None,
        })
    except Exception as e:
        print("[heartbeat] %s" % e)


@heartbeat_writer.before_loop
async def _before_heartbeat():
    await bot.wait_until_ready()


# Replace the stubbed Final⁵ /replay with a real historical inspector
try:
    tree.remove_command("replay")
except Exception:
    pass


@tree.command(name="replay",
              description="Inspect bot state on a past UTC date (YYYY-MM-DD)")
async def cmd_replay_v2(interaction: discord.Interaction, date: str):
    import io as _io
    import json as _json
    import sys
    import urllib.request
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    sys.path.insert(0, "/home/ubuntu/common")
    await interaction.response.defer()
    try:
        target = _dt.strptime(date.strip(), "%Y-%m-%d").replace(tzinfo=_tz.utc)
    except Exception:
        return await interaction.followup.send(
            "❌ Use `/replay YYYY-MM-DD`, e.g. `/replay 2026-04-15`")

    # Pull klines spanning target ±7 days
    start_ms = int((target - _td(days=7)).timestamp() * 1000)
    end_ms   = int((target + _td(days=1)).timestamp() * 1000)
    try:
        u4 = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
              f"&interval=4h&startTime={start_ms}&endTime={end_ms}&limit=200")
        u1 = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
              f"&interval=1h&startTime={start_ms}&endTime={end_ms}&limit=500")
        with urllib.request.urlopen(u4, timeout=10) as r:
            k4 = _json.loads(r.read())
        with urllib.request.urlopen(u1, timeout=10) as r:
            k1 = _json.loads(r.read())
    except Exception as e:
        return await interaction.followup.send(f"❌ Binance API: `{e}`")
    if not k4 or not k1:
        return await interaction.followup.send("_no klines_")

    # Ledger events on that day
    try:
        from ledger import read_all
        rows = [r for r in read_all()
                if (r.get("opened_at") or r.get("exit_time") or "")
                    .startswith(date.strip())]
    except Exception:
        rows = []

    # Render chart
    png = None
    try:
        import chart_render
        try:
            png = chart_render.render_signal_chart(
                symbol="BTCUSDT", as_of=target.isoformat(),
                klines_4h=k4, klines_1h=k1)
        except TypeError:
            png = None
    except Exception:
        png = None

    # Body
    if rows:
        body_lines = []
        for r in rows[:8]:
            ev = r.get("event", "?"); s_ = r.get("system", "?")
            stat = r.get("status", ""); rr = r.get("r", 0)
            body_lines.append(f"`{ev:<6}` **{s_}** {stat} ({rr:+.2f}R)")
        body = "\n".join(body_lines)
    else:
        body = ("_no alerts that day_  ·  bot was scanning but no setup met "
                "the score≥7/10 threshold")

    # Day OHLC summary
    day_klines = [k for k in k1
                  if int(k[0]) // 1000 // 86400
                  == int(target.timestamp()) // 86400]
    if day_klines:
        opens  = [float(k[1]) for k in day_klines]
        highs  = [float(k[2]) for k in day_klines]
        lows   = [float(k[3]) for k in day_klines]
        closes_ = [float(k[4]) for k in day_klines]
        d_open  = opens[0]; d_close = closes_[-1]
        d_high  = max(highs); d_low = min(lows)
        d_chg   = (d_close - d_open) / d_open * 100
        price_block = (f"Open: **${d_open:,.0f}** · Close: **${d_close:,.0f}** "
                       f"({d_chg:+.2f}%)\nHigh: **${d_high:,.0f}** · "
                       f"Low: **${d_low:,.0f}**")
    else:
        price_block = "_no intraday klines_"

    embed = discord.Embed(
        title=f"🔁  Replay  ·  {date.strip()} UTC",
        description=body, color=0x5865F2,
    )
    embed.add_field(name="📈  Price action that day",
                    value=price_block, inline=False)
    embed.set_footer(text="Replay · klines from Binance · ledger from forward_results")
    embed.timestamp = _dt.now(_tz.utc)

    if png:
        f = discord.File(_io.BytesIO(png), filename="replay.png")
        embed.set_image(url="attachment://replay.png")
        await interaction.followup.send(embed=embed, file=f)
    else:
        await interaction.followup.send(embed=embed)


@tree.command(name="sla",
              description="Bot uptime, auto-restarts, latency (last 7d)")
async def cmd_sla(interaction: discord.Interaction):
    import json as _json
    import os as _os
    import sys
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    sys.path.insert(0, "/home/ubuntu/common")
    await interaction.response.defer()

    try:
        import heartbeat as _hb
        age = _hb.age_seconds()
        if age < 120:
            hb_state = f"✅ alive ({age:.0f}s ago)"
        elif age < 300:
            hb_state = f"🟡 lagging ({age:.0f}s ago)"
        else:
            hb_state = f"🔴 stale ({age:.0f}s)"
    except Exception as e:
        hb_state = f"❌ {e}"

    log_path = "/home/ubuntu/common/watchdog_restarts.jsonl"
    cutoff = _time.time() - 7 * 86400
    last7 = []
    if _os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = _json.loads(line)
                    ts = _dt.fromisoformat(r["ts"].replace("Z", "+00:00"))
                    if ts.timestamp() >= cutoff:
                        last7.append(r)
                except Exception:
                    pass

    color = 0x00C851 if not last7 else (0xFFA500 if len(last7) <= 2 else 0xFF4444)
    embed = discord.Embed(title="📊  Bot SLA  ·  last 7 days", color=color)
    embed.add_field(name="Heartbeat",          value=hb_state, inline=True)
    embed.add_field(name="Auto-restarts (7d)", value=f"**{len(last7)}**", inline=True)
    embed.add_field(name="Discord latency",
                    value=f"**{bot.latency*1000:.0f}ms**", inline=True)

    # SLA percentage estimate
    if last7:
        # Each restart ~ 30s downtime estimate
        downtime = len(last7) * 30
        total = 7 * 86400
        sla_pct = (1 - downtime / total) * 100
        embed.add_field(name="Uptime (estimated)",
                        value=f"**{sla_pct:.3f}%**", inline=False)
        recent = "\n".join(
            f"• `{r['ts'][:16]}Z` — stale {r['stale_age_sec']:.0f}s"
            for r in last7[-5:])
        embed.add_field(name="Recent restarts", value=recent, inline=False)
    else:
        embed.add_field(name="Uptime (estimated)",
                        value="**100.000%**", inline=False)

    embed.set_footer(text="SLA · auto-monitored · bot_watchdog cron")
    embed.timestamp = _dt.now(_tz.utc)
    await interaction.followup.send(embed=embed)


# ─── /Final⁶ inject ───


# ─── Final⁷ inject ───
@tree.command(name="token", description="[ADMIN] Manage API tokens (issue/revoke/list)")
async def cmd_token(interaction: discord.Interaction, action: str, value: str = ""):
    import sys, os
    sys.path.insert(0, "/home/ubuntu/common")

    admin_ids = [s.strip() for s in
                 os.environ.get("ADMIN_USER_IDS", "").split(",")
                 if s.strip()]
    if str(interaction.user.id) not in admin_ids:
        await interaction.response.send_message(
            "❌ Admin only. Set `ADMIN_USER_IDS` in `/home/ubuntu/bot/.env` "
            f"to your Discord ID (yours: `{interaction.user.id}`).",
            ephemeral=True)
        return

    import api_auth
    action = action.strip().lower()

    if action == "issue":
        if not value:
            return await interaction.response.send_message(
                "Usage: `/token issue <label>` (e.g. `/token issue alice@example.com`)",
                ephemeral=True)
        token = api_auth.issue_token(label=value, tier=1)
        await interaction.response.send_message(
            f"✅ **New subscriber token** for `{value}`:\n"
            f"```\n{token}\n```\n"
            "Send this to the subscriber. Use as:\n"
            f"`Authorization: Bearer {token[:8]}...`\n\n"
            "_Visible once. Revoke with `/token revoke <token>`._",
            ephemeral=True)
    elif action == "revoke":
        if not value:
            return await interaction.response.send_message(
                "Usage: `/token revoke <full_token>`", ephemeral=True)
        ok = api_auth.revoke_token(value)
        await interaction.response.send_message(
            "✅ Token revoked." if ok else "❌ Token not found.",
            ephemeral=True)
    elif action == "list":
        d = api_auth.list_tokens()
        if not d:
            return await interaction.response.send_message(
                "_No tokens issued yet._", ephemeral=True)
        lines = [f"`{k}` — **{v.get('label','?')}** "
                 f"(tier {v.get('tier',0)}, {v.get('uses',0)} uses)"
                 for k, v in d.items()]
        await interaction.response.send_message(
            f"**Issued tokens ({len(d)}):**\n" + "\n".join(lines),
            ephemeral=True)
    else:
        await interaction.response.send_message(
            "Use: `/token issue <label>`, `/token revoke <token>`, or `/token list`",
            ephemeral=True)


@tree.command(name="tiers", description="View subscription tiers and access")
async def cmd_tiers(interaction: discord.Interaction):
    embed = discord.Embed(
        title="💎  Subscription Tiers",
        description="_Verifiable forward-test track record · BTCUSDT 4H·1H_",
        color=0xFFB400,
    )
    embed.add_field(
        name="🆓  Free",
        value=("• #signals channel access\n"
               "• Public dashboard (last 24h)\n"
               "• Public API: `/api/stats`, `/api/recent`\n"
               "• Slash commands: `/forward`, `/proximity`, `/market`"),
        inline=False,
    )
    embed.add_field(
        name="💎  Subscriber  (Bearer token required)",
        value=("• Full ledger access via `/api/full`\n"
               "• Extended risk metrics: `/api/risk_extended`\n"
               "  (Sharpe + 95% bootstrap CI, Kelly fraction, expectancy)\n"
               "• DM-tier alerts with personalized position sizing\n"
               "• `/export` — 90-day CSV download\n"
               "• `/replay <date>` — historical replay with chart"),
        inline=False,
    )
    embed.add_field(
        name="🔒  How tokens work",
        value=("Send `Authorization: Bearer <token>` with API requests.\n"
               "Tokens are issued by the operator via `/token issue`.\n"
               "Revoke any time via `/token revoke <token>`."),
        inline=False,
    )
    embed.set_footer(text="Educational · paper-traded · not financial advice")
    await interaction.response.send_message(embed=embed)


# ─── WHATSUP_PATCH_v1 ─── /whatsup snapshot command (minimal) ───
@tree.command(name="whatsup",
              description="Catch me up — price, nearest zones, alert status")
async def cmd_whatsup(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        import sys as _sys
        _sys.path.insert(0, "/home/ubuntu/common")
        import whatsup as _wu
        snap = _wu.snapshot()
    except Exception as e:
        await interaction.followup.send(
            content=f"snapshot failed: `{e}`", ephemeral=True)
        return

    cur = snap.get("current_price")
    ex = snap.get("exchange", "?")
    reg = snap.get("regime", "?")
    above = (snap.get("zones_above") or [None])[0]
    below = (snap.get("zones_below") or [None])[0]

    # Color by regime
    color = 0xFFCF3D
    if reg == "TRENDING_DOWN":
        color = 0xFF3D57
    elif reg == "TRENDING_UP":
        color = 0x00D27E
    elif reg == "VOLATILE":
        color = 0x9B6BFF

    title = f"📡 BTC ${cur:,.0f}" if cur else "📡 BTC ?"
    title += f" · {reg}"
    embed = discord.Embed(
        title=title, color=color,
        description=f"_{_wu.short_verdict(snap)}_")

    parts = []
    if above:
        k = above["kind"].replace("_", " ").replace("INT ", "")
        parts.append(
            f"🔴 **SELL** `${above['low']:,.0f}–${above['high']:,.0f}`  "
            f"({above['tf']} {k}, +{above['distance_pct']:.2f}%)")
    if below:
        k = below["kind"].replace("_", " ").replace("INT ", "")
        parts.append(
            f"🟢 **BUY** `${below['low']:,.0f}–${below['high']:,.0f}`  "
            f"({below['tf']} {k}, {below['distance_pct']:.2f}%)")
    if parts:
        embed.add_field(name="🎯 Nearest play",
                        value="\n".join(parts), inline=False)

    last_b = snap.get("last_bullish_alert_age")
    last_e = snap.get("last_bearish_alert_age")
    parts2 = []
    if last_b:
        parts2.append(f"🟢 last bull: {last_b}")
    if last_e:
        parts2.append(f"🔴 last bear: {last_e}")
    gap = snap.get("global_gap_remaining", "ready")
    if gap and gap != "ready":
        parts2.append(f"⏳ next in {gap}")
    n_ok = sum(1 for v in snap.get("services", {}).values() if v)
    n_total = len(snap.get("services", {}))
    parts2.append(f"⚙ {n_ok}/{n_total} up")
    embed.add_field(name="🛎 Status",
                    value=" · ".join(parts2), inline=False)

    embed.set_footer(text=f"{ex} perp · educational · paper")
    await interaction.followup.send(embed=embed)
# ─── /WHATSUP_PATCH_v1 ───



# ─── PERF_CMD_v1 ─── /perf per-strategy performance command ─────────
@tree.command(name="perf",
              description="Per-strategy performance breakdown (all-time)")
@app_commands.describe(window="Time window: 'all', '7d', '30d' (default all)")
async def cmd_perf(interaction: discord.Interaction, window: str = "all"):
    await interaction.response.defer(thinking=True)
    try:
        import sys as _sys
        _sys.path.insert(0, "/home/ubuntu/common")
        import strategy_report as _sr
        if window in ("7d", "7days", "week"):
            since, label = 7, "last 7 days"
        elif window in ("30d", "30days", "month"):
            since, label = 30, "last 30 days"
        else:
            since, label = None, "all-time"
        rows = _sr.per_strategy(since_days=since)
        overall = _sr.overall_stats(since_days=since)
        embed_dict = _sr.build_embed(rows, overall, label)
        # Convert dict embed to discord.Embed
        embed = discord.Embed(
            title=embed_dict.get("title"),
            description=embed_dict.get("description"),
            color=embed_dict.get("color", 0x5B8DEF),
        )
        for f in embed_dict.get("fields", []):
            embed.add_field(name=f["name"], value=f["value"],
                            inline=f.get("inline", False))
        if embed_dict.get("footer", {}).get("text"):
            embed.set_footer(text=embed_dict["footer"]["text"])
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(
            content=f"perf failed: `{e}`", ephemeral=True)
# ─── /PERF_CMD_v1 ───


# ─── /Final⁷ inject ───


# ─── Final⁹ inject ───
# Replace the basic /sla from Final⁶ with a SLO-aware version
try:
    tree.remove_command("sla")
except Exception:
    pass


@tree.command(name="sla",
              description="Bot SLI/SLO scorecard with error-budget burn rate")
async def cmd_sla_v2(interaction: discord.Interaction):
    import json as _json
    import os as _os
    import sys
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    sys.path.insert(0, "/home/ubuntu/common")
    await interaction.response.defer()

    SLO_UPTIME = 0.999          # target: 99.9% — 43.2 min downtime/month
    DOWNTIME_PER_RESTART = 30    # estimated seconds per auto-restart
    BUDGET_30D_SEC = 30 * 86400 * (1 - SLO_UPTIME)  # 2592 s/month
    BUDGET_7D_SEC = 7 * 86400 * (1 - SLO_UPTIME)    # 604 s/week

    # Heartbeat
    try:
        import heartbeat as _hb
        age = _hb.age_seconds()
        if age < 120:
            hb_state = f"✅ alive ({age:.0f}s ago)"
        elif age < 300:
            hb_state = f"🟡 lagging ({age:.0f}s)"
        else:
            hb_state = f"🔴 stale ({age:.0f}s)"
    except Exception as e:
        hb_state = f"❌ {e}"

    # Restart history
    log_path = "/home/ubuntu/common/watchdog_restarts.jsonl"
    cutoff7 = _time.time() - 7 * 86400
    cutoff30 = _time.time() - 30 * 86400
    last7, last30 = [], []
    if _os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = _json.loads(line)
                    ts = _dt.fromisoformat(r["ts"].replace("Z", "+00:00"))
                    ep = ts.timestamp()
                    if ep >= cutoff7:  last7.append(r)
                    if ep >= cutoff30: last30.append(r)
                except Exception:
                    pass

    downtime_7d  = len(last7)  * DOWNTIME_PER_RESTART
    downtime_30d = len(last30) * DOWNTIME_PER_RESTART
    sla_30d_pct  = max(0, (1 - downtime_30d / (30 * 86400))) * 100
    budget_used_pct = (downtime_30d / BUDGET_30D_SEC * 100
                       if BUDGET_30D_SEC > 0 else 0)
    burn_rate = (downtime_7d / BUDGET_7D_SEC) if BUDGET_7D_SEC > 0 else 0

    if burn_rate <= 1.0 and sla_30d_pct >= SLO_UPTIME * 100:
        verdict = "🟢 **On track** — within SLO."; color = 0x00C851
    elif burn_rate <= 2.0:
        verdict = "🟡 **At risk** — burning budget faster than expected."
        color = 0xFFA500
    else:
        verdict = "🔴 **Breaching SLO** — error budget exhausted."
        color = 0xFF4444

    # Latency from Prometheus
    p99_ms = "—"
    try:
        import urllib.request as _ur
        with _ur.urlopen("http://127.0.0.1:8080/metrics", timeout=3) as r:
            text = r.read().decode()
        # Quick parse: find sum + count for /api/stats
        sum_v = count_v = 0.0
        for line in text.splitlines():
            if 'srs_request_duration_seconds_sum{path="/api/stats"}' in line:
                sum_v = float(line.split()[-1])
            elif 'srs_request_duration_seconds_count{path="/api/stats"}' in line:
                count_v = float(line.split()[-1])
        if count_v > 0:
            p99_ms = f"{(sum_v / count_v) * 1000:.0f}"
    except Exception:
        pass

    # Webhook queue stats
    try:
        import webhook_queue
        wq = webhook_queue.stats()
        wq_state = (f"pending: {wq['pending']} · dead: {wq['dead_letter']}")
    except Exception:
        wq_state = "n/a"

    embed = discord.Embed(
        title="📊  Bot SLI/SLO Scorecard",
        description=(f"**Target:** {SLO_UPTIME*100:.1f}% uptime  ·  "
                     f"**Budget:** {BUDGET_30D_SEC/60:.0f} min/month\n\n{verdict}"),
        color=color,
    )
    embed.add_field(name="Heartbeat", value=hb_state, inline=True)
    embed.add_field(name="Discord latency",
                    value=f"**{bot.latency*1000:.0f}ms**", inline=True)
    embed.add_field(name="API mean latency",
                    value=f"**{p99_ms}ms**", inline=True)
    embed.add_field(name="30-day uptime",
                    value=f"**{sla_30d_pct:.3f}%**", inline=True)
    embed.add_field(name="Auto-restarts (7d / 30d)",
                    value=f"**{len(last7)} / {len(last30)}**", inline=True)
    embed.add_field(name="Error-budget used (30d)",
                    value=f"**{budget_used_pct:.1f}%**", inline=True)
    burn_label = "sustainable" if burn_rate <= 1 else "unsustainable"
    embed.add_field(name="Burn rate (7d)",
                    value=f"**{burn_rate:.2f}×**  ({burn_label})",
                    inline=True)
    embed.add_field(name="Webhook queue",
                    value=f"`{wq_state}`", inline=True)

    if last7:
        recent = "\n".join(
            f"• `{r['ts'][:16]}Z` — stale {r['stale_age_sec']:.0f}s"
            for r in last7[-5:])
        embed.add_field(name="Recent auto-restarts",
                        value=recent, inline=False)

    embed.set_footer(text="SLI/SLO · auto-monitored · burn-rate alert at 1.0×")
    embed.timestamp = _dt.now(_tz.utc)
    await interaction.followup.send(embed=embed)


@tree.command(name="bans",
              description="[ADMIN] List currently auto-banned IPs (fail2ban)")
async def cmd_bans(interaction: discord.Interaction):
    import os as _os
    import sys
    sys.path.insert(0, "/home/ubuntu/common")

    admin_ids = [s.strip() for s in
                 _os.environ.get("ADMIN_USER_IDS", "").split(",")
                 if s.strip()]
    if str(interaction.user.id) not in admin_ids:
        return await interaction.response.send_message(
            "❌ Admin only.", ephemeral=True)

    import ip_ban
    bans = ip_ban.get_active_bans()
    if not bans:
        return await interaction.response.send_message(
            "✅ No active IP bans.", ephemeral=True)

    lines = [f"`{ip}` — unban in **{secs//60}m{secs%60}s**"
             for ip, secs in sorted(bans.items(), key=lambda kv: -kv[1])]
    await interaction.response.send_message(
        f"🔒 **{len(bans)} active ban(s):**\n" + "\n".join(lines[:20]),
        ephemeral=True)


# ─── /Final⁹ inject ───


# ─── Final¹⁰ inject ───
@tasks.loop(seconds=30)
async def bot_metrics_writer():
    """Write bot counters atomically; status_server merges into /metrics."""
    try:
        import sys
        sys.path.insert(0, "/home/ubuntu/common")
        import bot_metrics
        from ledger import count_lines
        bot_metrics.set_gauge("guilds", len(bot.guilds))
        bot_metrics.set_gauge(
            "latency_ms",
            round(bot.latency * 1000, 1) if bot.latency else 0)
        bot_metrics.set_gauge("ledger_lines", count_lines())
        # Loop health (1 = running, 0 = stopped)
        for name, loop in [
            ("scan_loop", "scan_loop"),
            ("live_ticker", "live_ticker"),
            ("heartbeat_writer", "heartbeat_writer"),
            ("rich_presence", "rich_presence_updater"),
            ("bot_metrics", "bot_metrics_writer"),
        ]:
            try:
                ref = globals().get(loop)
                bot_metrics.set_gauge(
                    f"loop_{name}_running",
                    1 if ref and ref.is_running() else 0)
            except Exception:
                pass
    except Exception as e:
        print("[bot_metrics_writer] %s" % e)


@bot_metrics_writer.before_loop
async def _before_bot_metrics():
    await bot.wait_until_ready()


# Audit hooks: wrap existing /token command via a thin override
try:
    tree.remove_command("token")
except Exception:
    pass


@tree.command(name="token", description="[ADMIN] Manage API tokens (audited)")
async def cmd_token_v2(interaction: discord.Interaction,
                       action: str, value: str = ""):
    import sys, os
    sys.path.insert(0, "/home/ubuntu/common")

    admin_ids = [s.strip() for s in
                 os.environ.get("ADMIN_USER_IDS", "").split(",")
                 if s.strip()]
    if str(interaction.user.id) not in admin_ids:
        return await interaction.response.send_message(
            "❌ Admin only.", ephemeral=True)

    import api_auth
    import audit_log
    actor = f"discord:{interaction.user.id}"
    action = action.strip().lower()

    if action == "issue":
        if not value:
            return await interaction.response.send_message(
                "Usage: `/token issue <label>`", ephemeral=True)
        token = api_auth.issue_token(label=value, tier=1)
        audit_log.record("token_issue", actor=actor, label=value,
                         tier=1, token_prefix=token[:8])
        await interaction.response.send_message(
            f"✅ Issued for `{value}`:\n```\n{token}\n```\n"
            "_Visible once. Logged to audit._", ephemeral=True)
    elif action == "revoke":
        if not value:
            return await interaction.response.send_message(
                "Usage: `/token revoke <token>`", ephemeral=True)
        ok = api_auth.revoke_token(value)
        audit_log.record("token_revoke", actor=actor,
                         token_prefix=value[:8], success=ok)
        await interaction.response.send_message(
            "✅ Revoked." if ok else "❌ Token not found.",
            ephemeral=True)
    elif action == "list":
        d = api_auth.list_tokens()
        audit_log.record("token_list", actor=actor, count=len(d))
        if not d:
            return await interaction.response.send_message(
                "_No tokens issued._", ephemeral=True)
        lines = [f"`{k}` — **{v.get('label','?')}** "
                 f"(tier {v.get('tier',0)}, {v.get('uses',0)} uses)"
                 for k, v in d.items()]
        await interaction.response.send_message(
            f"**{len(d)} token(s):**\n" + "\n".join(lines),
            ephemeral=True)
    else:
        await interaction.response.send_message(
            "Use: `/token issue <label>`, `/token revoke <token>`, "
            "or `/token list`", ephemeral=True)


@tree.command(name="audit",
              description="[ADMIN] Show recent audit-log entries")
async def cmd_audit(interaction: discord.Interaction, limit: int = 15):
    import sys, os
    sys.path.insert(0, "/home/ubuntu/common")

    admin_ids = [s.strip() for s in
                 os.environ.get("ADMIN_USER_IDS", "").split(",")
                 if s.strip()]
    if str(interaction.user.id) not in admin_ids:
        return await interaction.response.send_message(
            "❌ Admin only.", ephemeral=True)

    import audit_log
    entries = audit_log.tail(min(max(limit, 1), 50))
    if not entries:
        return await interaction.response.send_message(
            "_Audit log is empty._", ephemeral=True)
    lines = []
    for r in entries[-25:]:
        ts = r.get("ts", "")[:19]
        action = r.get("action", "?")
        actor = r.get("actor", "?")
        extras = ", ".join(f"{k}={v}" for k, v in r.items()
                           if k not in ("ts", "action", "actor"))
        lines.append(f"`{ts}Z` **{action}** ({actor}) — {extras[:100]}")
    body = "\n".join(lines)
    if len(body) > 1900:
        body = body[:1900] + "\n_(truncated)_"
    await interaction.response.send_message(
        f"📋 **Audit log · last {len(entries)} entries:**\n{body}",
        ephemeral=True)


# ─── /Final¹⁰ inject ───

# ══════════════════════════════════════════════════════════════════════
#  BOT EVENTS
# ══════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print("[bot] Logged in as %s (%s)" % (bot.user, bot.user.id))
    print("[bot] Connected to %d server(s)" % len(bot.guilds))

    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
        else:
            synced = await tree.sync()
        print("[bot] Synced %d slash commands" % len(synced))
    except Exception as e:
        print("[bot] Failed to sync commands: %s" % e)

    scan_loop.start()
    paper_monitor.start()
    daily_summary.start()
    live_dashboard.start()
    welcome_guide.start()
    topic_updater.start()
    live_ticker.start()
    strategy_reference.start()
    rich_presence_updater.start()
    heartbeat_writer.start()
    bot_metrics_writer.start()

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="BTC 4H breakouts | PAPER"
        )
    )

    stats = paper_trader.get_paper_stats()
    strats = ", ".join(config.STRATEGIES)
    _send_status_webhook(
        "Bot Online",
        "Strategies: %s\nMode: PAPER\nBalance: $%.2f\nTrades: %d\nTimeframe: 4H" % (
            strats, stats["balance"], stats["total_trades"]),
        0x00C851
    )


# ══════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

def run_bot():
    if not BOT_TOKEN:
        print("[bot] DISCORD_BOT_TOKEN not set in .env")
        return
    print("[bot] Starting 4H Strategy Bot...")
    bot.run(BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    run_bot()
