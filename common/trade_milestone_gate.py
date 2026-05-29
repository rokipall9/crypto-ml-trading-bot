"""
trade_milestone_gate.py — Watch closed-trade count, fire a Discord
celebration + readiness report at preset milestones (10, 25, 50, 100).

Each milestone fires ONCE — state persisted in milestone_gate_state.json.

The 10-trade milestone is special: it's the operator's review gate before
any live-trading decision. The embed contains everything needed to judge:
  • Win rate, profit factor, expectancy
  • Avg winner / avg loser (in $ and R)
  • Best / worst trade
  • Max consecutive losses
  • Largest drawdown from peak
  • Per-strategy breakdown
  • Loss patterns (from loss_journal)

Run via systemd timer every 5 min, or call after each trade close.
"""
from __future__ import annotations

import os
import sys
import json
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional

sys.path.insert(0, "/home/ubuntu/common")

STORE = "/home/ubuntu/bot/logs/paper_trades.json"
STATE_FILE = "/home/ubuntu/common/milestone_gate_state.json"

MILESTONES = [10, 25, 50, 100]

WEBHOOK = (os.environ.get("DISCORD_APPROACH_WEBHOOK", "").strip()
           or os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
           or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())

TV_CHART_URL = os.environ.get(
    "TRADINGVIEW_CHART_URL",
    "https://www.tradingview.com/chart/pViMM9Zt/?symbol=BYBIT%3ABTCUSDT.P",
).strip()


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"fired_milestones": []}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"fired_milestones": []}


def _save_state(s: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, default=str)
    os.replace(tmp, STATE_FILE)


def _trade_pnl(t: dict) -> float:
    return float(t.get("realized_pnl", t.get("pnl", 0)) or 0)


def _is_win(t: dict) -> bool:
    return t.get("status") in ("WIN", "PARTIAL_WIN", "TP1", "TP2", "TP3") \
           or _trade_pnl(t) > 0.01


def _is_loss(t: dict) -> bool:
    return t.get("status") in ("SL", "LOSS") or _trade_pnl(t) < -0.01


def _compute_stats(closed: List[dict], starting_balance: float = 10000.0
                   ) -> Dict:
    """Compute everything the operator wants to see at a milestone."""
    n = len(closed)
    if n == 0:
        return {"n": 0}

    wins = [t for t in closed if _is_win(t)]
    losses = [t for t in closed if _is_loss(t)]

    pnls = [_trade_pnl(t) for t in closed]
    win_pnls = [_trade_pnl(t) for t in wins]
    loss_pnls = [_trade_pnl(t) for t in losses]

    total_pnl = sum(pnls)
    total_win_pnl = sum(win_pnls)
    total_loss_pnl = abs(sum(loss_pnls))
    if total_loss_pnl == 0:
        profit_factor = float("inf") if total_win_pnl > 0 else 0
    else:
        profit_factor = total_win_pnl / total_loss_pnl

    avg_win = (sum(win_pnls) / len(win_pnls)) if win_pnls else 0
    avg_loss = (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0
    expectancy = total_pnl / n

    # Max consecutive losses
    max_consec_loss = 0
    cur = 0
    for t in closed:
        if _is_loss(t):
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        elif _is_win(t):
            cur = 0

    # Equity curve + max drawdown
    eq = starting_balance
    peak = eq
    max_dd_pct = 0.0
    max_dd_abs = 0.0
    for t in closed:
        eq += _trade_pnl(t)
        if eq > peak:
            peak = eq
        dd_abs = peak - eq
        dd_pct = dd_abs / peak * 100 if peak > 0 else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd_abs = dd_abs

    by_strategy: Dict[str, Dict] = {}
    for t in closed:
        s = t.get("strategy", t.get("method", t.get("strategy_name", "?")))
        by_strategy.setdefault(s, {"n": 0, "w": 0, "l": 0, "pnl": 0.0})
        by_strategy[s]["n"] += 1
        if _is_win(t):
            by_strategy[s]["w"] += 1
        elif _is_loss(t):
            by_strategy[s]["l"] += 1
        by_strategy[s]["pnl"] += _trade_pnl(t)

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "wr_pct": len(wins) / n * 100,
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / starting_balance * 100, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "profit_factor_display": ("∞" if profit_factor == float("inf")
                                  else round(profit_factor, 2)),
        "best_trade": round(max(pnls), 2),
        "worst_trade": round(min(pnls), 2),
        "max_consec_loss": max_consec_loss,
        "max_drawdown_abs": round(max_dd_abs, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "ending_balance": round(starting_balance + total_pnl, 2),
        "by_strategy": by_strategy,
    }


def _fire_milestone_alert(milestone: int, stats: Dict) -> bool:
    """Post a celebratory + actionable Discord embed at this milestone."""
    if not WEBHOOK:
        print("[milestone] no webhook configured")
        return False

    color = 0xFFD700 if milestone == 10 else 0xFFA500

    title = f"🎯 Milestone reached: {milestone} trades closed"
    if milestone == 10:
        subtitle = ("This is your **review gate** before any live-trading decision.\n"
                    "_Below is the full picture — read carefully._")
    else:
        subtitle = "_Performance review — every {} trades_".format(milestone)

    s = stats
    overview = (
        f"**Trades:**  {s['n']} (W: {s['wins']} · L: {s['losses']})\n"
        f"**Win rate:**  `{s['wr_pct']:.1f}%`\n"
        f"**Net P&L:**  `+${s['total_pnl']:,.2f}` (`{s['total_pnl_pct']:+.2f}%`)\n"
        f"**Balance:**  `${s['ending_balance']:,.2f}`"
    )
    risk_block = (
        f"**Profit factor:**  `{s['profit_factor']:.2f}`\n"
        f"**Expectancy/trade:**  `${s['expectancy']:+.2f}`\n"
        f"**Avg winner:**  `${s['avg_win']:+.2f}`\n"
        f"**Avg loser:**  `${s['avg_loss']:+.2f}`\n"
        f"**Max consec losses:**  `{s['max_consec_loss']}`\n"
        f"**Max drawdown:**  `${s['max_drawdown_abs']:.2f}` (`{s['max_drawdown_pct']:.2f}%`)\n"
        f"**Best trade:**  `+${s['best_trade']:,.2f}`\n"
        f"**Worst trade:**  `${s['worst_trade']:,.2f}`"
    )

    by_strat = []
    for k, v in sorted(s["by_strategy"].items(), key=lambda kv: -kv[1]["pnl"]):
        wr = v["w"] / v["n"] * 100 if v["n"] else 0
        by_strat.append(
            f"`{k}` · {v['n']} trades · {wr:.0f}% WR · "
            f"`${v['pnl']:+.2f}`")
    by_strat_block = "\n".join(by_strat) or "_no strategies recorded_"

    # Pull pattern stats from loss_journal if available
    patterns = ""
    try:
        import loss_journal
        ls = loss_journal.summary()
        if ls.get("n", 0) > 0:
            patterns = (
                f"\n\n**Loss patterns** (n={ls['n']}):\n"
                f"• {ls.get('pct_after_liquidity_grab', 0)}% after a liquidity-grab wick\n"
                f"• {ls.get('pct_low_liquidity_hour', 0)}% in low-liquidity UTC hours\n"
                f"• {ls.get('pct_reached_tp1_then_failed', 0)}% reached TP1 then failed (trail issue)\n"
                f"• avg MFE: `{ls.get('avg_mfe_R')}R` · avg MAE: `{ls.get('avg_mae_R')}R`")
    except Exception:
        pass

    decision_guide = ""
    if milestone == 10:
        decision_guide = (
            "\n\n**Decision guide before going live:**\n"
            "✅ Profit factor ≥ 1.5 + WR ≥ 50% → looks viable\n"
            "✅ Max drawdown ≤ 5% → risk size felt OK\n"
            "✅ Max consec losses ≤ 3 → strategy hasn't tilted\n"
            "❌ Profit factor < 1.0 → NOT ready, paper longer\n"
            "❌ One strategy carrying everything → fragile, paper longer\n"
            "❌ Best trade is huge outlier → avg downward → paper longer\n\n"
            "**Recommended:** keep paper-trading to 30+ closes before live. "
            "10 is informative but small — 30 is statistically meaningful."
        )

    embed = {
        "title": title,
        "url": TV_CHART_URL,
        "description": (
            subtitle + "\n\n"
            "**📊 Overview**\n" + overview + "\n\n"
            "**📐 Risk metrics**\n" + risk_block + "\n\n"
            "**🏆 By strategy**\n" + by_strat_block +
            patterns + decision_guide
        ),
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Milestone gate · educational · paper-traded"},
    }
    payload = {
        "content": "@everyone" if milestone == 10 else "",
        "allowed_mentions": {"parse": ["everyone"]} if milestone == 10 else {},
        "username": "🎯 Milestone Gate",
        "embeds": [embed],
    }
    try:
        req = urllib.request.Request(
            WEBHOOK, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "srs-milestone/1.0"})
        urllib.request.urlopen(req, timeout=8).read()
        return True
    except Exception as e:
        print("[milestone] post_fail:", e)
        return False


def check_and_fire() -> Dict:
    """Check current trade count vs milestones, fire any not-yet-fired."""
    if not os.path.exists(STORE):
        return {"status": "no_store"}
    with open(STORE) as f:
        store = json.load(f)
    closed = store.get("closed_trades", [])
    n = len(closed)

    state = _load_state()
    fired = set(state.get("fired_milestones", []))
    starting_balance = float(store.get("starting_balance", 10000.0))

    new_fires = []
    for m in MILESTONES:
        if n >= m and m not in fired:
            stats = _compute_stats(closed[:m], starting_balance)
            if _fire_milestone_alert(m, stats):
                fired.add(m)
                new_fires.append(m)

    state["fired_milestones"] = sorted(fired)
    _save_state(state)
    return {"status": "ok", "trades_closed": n,
            "milestones_fired_this_run": new_fires,
            "all_fired": sorted(fired),
            "next_milestone": next((m for m in MILESTONES if m not in fired), None)}


if __name__ == "__main__":
    print(json.dumps(check_and_fire(), indent=2, default=str))
