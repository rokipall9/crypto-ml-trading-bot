"""
strategy_report.py — Per-strategy performance breakdown.

Reads closed_trades from paper_trades.json and computes:
  • WR, total P&L, avg P&L per trade
  • Profit factor (gross wins / gross losses)
  • Best / worst trade
  • Avg winner $ / avg loser $
  • Max consecutive losses
  • Side-specific stats (long vs short)

Used by /perf slash command + scheduled daily report.
"""
from __future__ import annotations

import os
import json
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

STORE = "/home/ubuntu/bot/logs/paper_trades.json"
BOOK_STORE = "/home/ubuntu/bot/logs/paper_trades_book.json"   # separate book bankroll


def _trade_pnl(t: dict) -> float:
    return float(t.get("realized_pnl", t.get("pnl", 0)) or 0)


def _is_win(t: dict) -> bool:
    return (t.get("status") in ("WIN", "PARTIAL_WIN", "TP1", "TP2", "TP3")
            or _trade_pnl(t) > 0.01)


def _is_loss(t: dict) -> bool:
    return t.get("status") in ("SL", "LOSS") or _trade_pnl(t) < -0.01


def _load_all_closed() -> List[dict]:
    """Pull closed trades from BOTH bankrolls (SMC + book) into one list.

    Each trade keeps its own strategy name; the source is tagged so /perf
    can show them side-by-side without mixing P&L into the main bankroll.
    """
    out: List[dict] = []
    for path, source_tag in ((STORE, "smc"), (BOOK_STORE, "book")):
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                d = json.load(f)
            for t in d.get("closed_trades", []):
                t = dict(t)
                t["_source"] = source_tag
                out.append(t)
        except Exception:
            pass
    return out


def per_strategy(since_days: Optional[int] = None,
                 group_by_side: bool = True) -> List[Dict]:
    """Return list of dicts with per-strategy stats, sorted by total P&L desc."""
    closed = _load_all_closed()
    if not closed:
        return []

    if since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        kept = []
        for t in closed:
            ts_str = t.get("closed_at")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                if ts >= cutoff:
                    kept.append(t)
            except Exception:
                pass
        closed = kept

    groups: Dict[tuple, List[dict]] = defaultdict(list)
    for t in closed:
        strat = t.get("strategy", t.get("method", t.get("strategy_name", "?")))
        side = t.get("side", "?")
        key = (strat, side) if group_by_side else (strat, "any")
        groups[key].append(t)

    results = []
    for (strat, side), trades in groups.items():
        n = len(trades)
        wins = [t for t in trades if _is_win(t)]
        losses = [t for t in trades if _is_loss(t)]
        pnls = [_trade_pnl(t) for t in trades]
        win_pnls = [p for p in pnls if p > 0]
        loss_pnls = [p for p in pnls if p < 0]

        total_pnl = sum(pnls)
        gross_wins = sum(win_pnls)
        gross_losses = abs(sum(loss_pnls))
        # PF is undefined when no losses exist; cap display to avoid
        # huge meaningless numbers (was rendering as 1.07e+11)
        if gross_losses == 0 and gross_wins > 0:
            pf_display = float("inf")
        elif gross_losses == 0:
            pf_display = 0.0
        else:
            pf_display = gross_wins / gross_losses

        # Max consecutive losses
        max_consec = 0
        cur = 0
        for t in trades:
            if _is_loss(t):
                cur += 1
                max_consec = max(max_consec, cur)
            elif _is_win(t):
                cur = 0

        results.append({
            "strategy": strat,
            "side": side,
            "n": n,
            "wins": len(wins),
            "losses": len(losses),
            "wr_pct": round(len(wins) / n * 100, 1) if n else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / n, 2) if n else 0,
            "best": round(max(pnls, default=0), 2),
            "worst": round(min(pnls, default=0), 2),
            "profit_factor": (round(pf_display, 2)
                              if pf_display != float("inf") else "∞"),
            "avg_win": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0,
            "avg_loss": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0,
            "max_consec_loss": max_consec,
        })

    results.sort(key=lambda r: -r["total_pnl"])
    return results


def overall_stats(since_days: Optional[int] = None) -> Dict:
    """Aggregate across ALL strategies (SMC + book bankrolls combined)."""
    closed = _load_all_closed()
    if not closed:
        return {"n": 0}
    pnls = [_trade_pnl(t) for t in closed]
    wins_all = [p for p in pnls if p > 0]
    losses_all = [p for p in pnls if p < 0]
    pf = (sum(wins_all) / abs(sum(losses_all))) if losses_all else float("inf")
    # Combined balance — sum of both bankrolls
    combined_balance = 0.0
    for path in (STORE, BOOK_STORE):
        if os.path.exists(path):
            try:
                with open(path) as f:
                    combined_balance += float(json.load(f).get("balance", 0))
            except Exception:
                pass
    return {
        "n": len(closed),
        "total_pnl": round(sum(pnls), 2),
        "wins": len(wins_all),
        "losses": len(losses_all),
        "wr_pct": round(len(wins_all) / len(pnls) * 100, 1) if pnls else 0,
        "profit_factor": round(pf, 2) if pf != float("inf") else "∞",
        "balance": round(combined_balance, 2),
    }


def _verdict(pf) -> str:
    if isinstance(pf, str):    # "∞"
        return "✅"
    if pf >= 1.5:
        return "✅"
    if pf >= 1.0:
        return "🟡"
    return "❌"


def _pf_str(pf) -> str:
    if isinstance(pf, str):
        return pf
    return f"{pf:.2f}"


def render_text(rows: List[Dict], overall: Dict, window_label: str = "all-time") -> str:
    lines = []
    lines.append(f"📊 Per-strategy performance · {window_label}")
    lines.append("")
    if overall.get("n", 0) == 0:
        lines.append("No closed trades yet.")
        return "\n".join(lines)
    lines.append(f"OVERALL: {overall['n']} trades · {overall['wr_pct']:.1f}% WR · "
                 f"+${overall['total_pnl']:.2f} · PF "
                 f"{_pf_str(overall.get('profit_factor', 0))}")
    lines.append("")
    for r in rows:
        n_label = "trade" if r["n"] == 1 else "trades"
        lines.append(
            f"{_verdict(r['profit_factor'])} {r['strategy']:<22} ({r['side'].upper()})  "
            f"{r['n']:>2} {n_label}  {r['wr_pct']:>5.1f}% WR  "
            f"${r['total_pnl']:>+8.2f}  PF {_pf_str(r['profit_factor']):>5}")
    return "\n".join(lines)


# ─── Discord embed posting ───────────────────────────────────────────

def build_embed(rows: List[Dict], overall: Dict,
                window_label: str = "all-time") -> Dict:
    if overall.get("n", 0) == 0:
        return {
            "title": f"📊 Per-strategy performance · {window_label}",
            "description": "_No closed trades yet — bot is still scanning._",
            "color": 0x5B8DEF,
        }

    overall_block = (
        f"**{overall['n']}** trades  ·  "
        f"**{overall['wr_pct']:.1f}%** WR  ·  "
        f"**+${overall['total_pnl']:.2f}** P&L\n"
        f"PF: **{_pf_str(overall.get('profit_factor', 0))}**  ·  "
        f"Balance: **${overall['balance']:,.2f}**"
    )

    fields = [{"name": "📊 Overall", "value": overall_block, "inline": False}]

    for r in rows:
        side_emoji = "📈" if r["side"] == "buy" else "📉" if r["side"] == "sell" else "•"
        verdict_emoji = _verdict(r["profit_factor"])

        # Sample-size warning
        sample_note = ""
        if r["n"] < 10:
            sample_note = "  ⚠ small sample"
        elif r["n"] < 30:
            sample_note = "  📊 building sample"

        n_label = "trade" if r["n"] == 1 else "trades"
        value = (
            f"{verdict_emoji} **{r['wr_pct']:.0f}% WR**  "
            f"({r['wins']}W / {r['losses']}L) · {r['n']} {n_label}{sample_note}\n"
            f"💰 P&L: **${r['total_pnl']:+,.2f}**  ·  "
            f"avg/trade `${r['avg_pnl']:+.2f}`\n"
            f"📐 PF **{_pf_str(r['profit_factor'])}**  ·  "
            f"avg win `${r['avg_win']:+.2f}`  ·  "
            f"avg loss `${r['avg_loss']:+.2f}`\n"
            f"🏆 best `${r['best']:+,.2f}`  ·  "
            f"💀 worst `${r['worst']:+,.2f}`"
        )
        if r["max_consec_loss"] > 1:
            value += f"\n⚠ max consec losses: **{r['max_consec_loss']}**"

        fields.append({
            "name": f"{side_emoji} {r['strategy']} ({r['side'].upper()})",
            "value": value,
            "inline": False,
        })

    return {
        "title": f"📊 Per-strategy performance · {window_label}",
        "description": "_Sorted by total P&L · ✅ PF≥1.5 · 🟡 PF≥1.0 · ❌ PF<1.0_",
        "color": 0x5B8DEF,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Educational · paper-traded · 4H Strategy System"},
    }


def post_to_discord(window_label: str = "all-time",
                    since_days: Optional[int] = None) -> bool:
    webhook = (os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
               or os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip())
    if not webhook:
        return False
    rows = per_strategy(since_days=since_days)
    overall = overall_stats(since_days=since_days)
    embed = build_embed(rows, overall, window_label)
    payload = {"username": "📊 Strategy Report", "embeds": [embed]}
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "srs-perf/1.0"})
        urllib.request.urlopen(req, timeout=8).read()
        return True
    except Exception as e:
        print("[strategy_report] post_fail:", e)
        return False


if __name__ == "__main__":
    import sys
    since = None
    label = "all-time"
    if len(sys.argv) > 1 and sys.argv[1] == "7d":
        since, label = 7, "last 7 days"
    elif len(sys.argv) > 1 and sys.argv[1] == "30d":
        since, label = 30, "last 30 days"
    rows = per_strategy(since_days=since)
    overall = overall_stats(since_days=since)
    print(render_text(rows, overall, label))
    if len(sys.argv) > 2 and sys.argv[2] == "post":
        ok = post_to_discord(label, since)
        print(f"\n  posted to discord: {ok}")
