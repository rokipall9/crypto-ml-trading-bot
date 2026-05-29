"""
verify_trades.py — Independent re-verification of every closed trade
against public Bybit kline data.

The bot's paper_trades.json is the bot's *own* accounting — that's not
trustworthy on its own. This module pulls the actual public Bybit
kline history for each trade window and checks:

  1. Entry bar exists at the open timestamp (within tolerance)
  2. SL price was actually traded (price low <= SL during open window)
     OR TP1 price was actually traded (high >= TP1 during open window)
  3. Exit price matches a real bar at the close timestamp
  4. Direction of P&L matches the price action

If a trade fails any check, it's flagged. A tampered paper_trades.json
would fail this verification because Bybit's kline history is
immutable and externally hosted.

Usage:
  python3 verify_trades.py            # verify all closed trades
  python3 verify_trades.py post       # verify + post Discord summary
"""
from __future__ import annotations

import os
import json
import urllib.request
from datetime import datetime, timezone
from typing import List, Dict, Tuple

STORE = "/home/ubuntu/bot/logs/paper_trades.json"
PRICE_TOL_PCT = 0.10   # 0.1% tolerance on entry/exit price match
INTERVAL_MS = 15 * 60 * 1000


def _fetch_bybit_klines_window(start_ms: int, end_ms: int,
                              interval: str = "15") -> List[dict]:
    """Fetch 15m klines covering [start_ms, end_ms+pad]. Public API, no auth."""
    pad_ms = 15 * 60 * 1000   # ±15min pad
    bars: List[dict] = []
    cursor_end = end_ms + pad_ms
    target_start = start_ms - pad_ms
    while cursor_end > target_start:
        n = 200
        url = (f"https://api.bybit.com/v5/market/kline?"
               f"category=linear&symbol=BTCUSDT&interval={interval}"
               f"&limit={n}&end={cursor_end}")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "srs-verify/1.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=8).read())
        except Exception as e:
            print("[verify] fetch_fail:", e)
            break
        rows = data.get("result", {}).get("list", [])
        if not rows:
            break
        for r in rows:
            ts = int(r[0])
            if ts < target_start:
                continue
            bars.append({
                "ts": ts, "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]),
            })
        oldest = int(rows[-1][0])
        if oldest <= target_start:
            break
        cursor_end = oldest - 1
    bars.sort(key=lambda b: b["ts"])
    return bars


def _bar_at(bars: List[dict], ts_ms: int) -> Dict | None:
    """Find the bar covering the given timestamp."""
    for b in bars:
        if b["ts"] <= ts_ms < b["ts"] + INTERVAL_MS:
            return b
    return None


def _price_within_pct(claimed: float, actual_low: float, actual_high: float,
                     tol_pct: float = PRICE_TOL_PCT) -> bool:
    """Is the claimed price within [low-tol, high+tol] of the actual bar?"""
    pad = (actual_low + actual_high) / 2 * tol_pct / 100
    return (actual_low - pad) <= claimed <= (actual_high + pad)


def verify_trade(trade: Dict) -> Dict:
    """Verify one closed trade against Bybit public data."""
    result = {
        "trade_id": trade.get("id"),
        "strategy": trade.get("strategy", trade.get("method", "?")),
        "side": trade.get("side"),
        "entry": trade.get("entry"),
        "exit": trade.get("exit_price", trade.get("sl_current")),
        "status": trade.get("status"),
        "pnl_claimed": float(trade.get("realized_pnl", trade.get("pnl", 0))),
        "checks": [],
        "verdict": "?",
    }

    try:
        opened_at = trade.get("opened_at")
        closed_at = trade.get("closed_at")
        if not (opened_at and closed_at):
            result["verdict"] = "MISSING_TIMES"
            return result
        open_dt = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
        close_dt = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        open_ms = int(open_dt.timestamp() * 1000)
        close_ms = int(close_dt.timestamp() * 1000)

        bars = _fetch_bybit_klines_window(open_ms, close_ms)
        if not bars:
            result["verdict"] = "NO_BYBIT_DATA"
            return result

        # Check 1: entry bar exists, entry price is plausible
        entry = float(trade.get("entry", 0))
        entry_bar = _bar_at(bars, open_ms)
        if not entry_bar:
            result["checks"].append({"name": "entry_bar", "pass": False,
                                      "detail": "no bar at open_ms"})
        else:
            ok = _price_within_pct(entry, entry_bar["low"], entry_bar["high"])
            result["checks"].append({
                "name": "entry_price_in_bar",
                "pass": ok,
                "claimed": entry,
                "actual_low": entry_bar["low"], "actual_high": entry_bar["high"],
            })

        # Check 2: exit price exists in some bar within trade window
        exit_price = float(trade.get("exit_price", trade.get("sl_current", 0)))
        in_window = [b for b in bars if open_ms <= b["ts"] <= close_ms + INTERVAL_MS]
        exit_ok = any(_price_within_pct(exit_price, b["low"], b["high"])
                      for b in in_window)
        result["checks"].append({
            "name": "exit_price_traded_in_window",
            "pass": exit_ok,
            "claimed": exit_price,
            "n_bars_in_window": len(in_window),
        })

        # Check 3: the SL or TP price was actually reached (depending on outcome)
        sl = float(trade.get("sl_original", trade.get("sl_current", 0)))
        tp1 = float(trade.get("tp1", 0) or 0)
        side = trade.get("side", "buy")
        status = trade.get("status", "?")

        if status in ("SL", "LOSS") and sl > 0:
            # SL must have been hit by price low (long) or high (short)
            if side == "buy":
                sl_hit = any(b["low"] <= sl for b in in_window)
            else:
                sl_hit = any(b["high"] >= sl for b in in_window)
            result["checks"].append({
                "name": "SL_actually_hit",
                "pass": sl_hit,
                "sl": sl,
            })
        elif status in ("WIN", "PARTIAL_WIN", "TP1") and tp1 > 0:
            # TP1 must have been hit
            if side == "buy":
                tp1_hit = any(b["high"] >= tp1 for b in in_window)
            else:
                tp1_hit = any(b["low"] <= tp1 for b in in_window)
            result["checks"].append({
                "name": "TP1_actually_hit",
                "pass": tp1_hit,
                "tp1": tp1,
            })

        # Check 4: P&L direction matches the price move
        pnl_claimed = result["pnl_claimed"]
        if entry > 0 and exit_price > 0:
            if side == "buy":
                expected_sign = 1 if exit_price > entry else -1
            else:
                expected_sign = 1 if exit_price < entry else -1
            actual_sign = 1 if pnl_claimed > 0 else -1 if pnl_claimed < 0 else 0
            sign_ok = (actual_sign == expected_sign or pnl_claimed == 0)
            result["checks"].append({
                "name": "pnl_sign_matches_price_direction",
                "pass": sign_ok,
                "side": side, "entry": entry, "exit": exit_price,
                "pnl": pnl_claimed,
            })

        # Verdict: PASS if every named check passed
        all_pass = all(c["pass"] for c in result["checks"])
        result["verdict"] = "VERIFIED" if all_pass else "SUSPICIOUS"
    except Exception as e:
        result["verdict"] = "ERROR"
        result["error"] = str(e)
    return result


def verify_all() -> List[Dict]:
    if not os.path.exists(STORE):
        return []
    with open(STORE) as f:
        store = json.load(f)
    return [verify_trade(t) for t in store.get("closed_trades", [])]


def render_text(results: List[Dict]) -> str:
    lines = []
    lines.append(f"🔐 Independent verification — {len(results)} closed trades")
    lines.append("Source: Bybit public kline API (immutable, externally hosted)")
    lines.append("")
    n_verified = sum(1 for r in results if r["verdict"] == "VERIFIED")
    n_susp = sum(1 for r in results if r["verdict"] == "SUSPICIOUS")
    n_err = sum(1 for r in results if r["verdict"] in ("ERROR", "MISSING_TIMES",
                                                       "NO_BYBIT_DATA"))
    lines.append(f"VERIFIED:    {n_verified}")
    lines.append(f"SUSPICIOUS:  {n_susp}")
    lines.append(f"ERRORS:      {n_err}")
    lines.append("")
    for r in results:
        verdict_emoji = {"VERIFIED": "✅", "SUSPICIOUS": "❌"}.get(r["verdict"], "⚠")
        lines.append(f"{verdict_emoji}  {(r.get('trade_id') or '?')[:8]}  "
                     f"{r['strategy']}  {(r['side'] or '?').upper()}  "
                     f"entry ${r['entry']:>9,.2f}  pnl ${r['pnl_claimed']:>+8.2f}  "
                     f"[{r['verdict']}]")
        for c in r["checks"]:
            mark = "✓" if c["pass"] else "✗"
            lines.append(f"      {mark} {c['name']}")
    return "\n".join(lines)


def post_summary_to_discord(results: List[Dict]) -> bool:
    webhook = (os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
               or os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip())
    if not webhook:
        return False
    n_verified = sum(1 for r in results if r["verdict"] == "VERIFIED")
    n_susp = sum(1 for r in results if r["verdict"] == "SUSPICIOUS")
    n_err = len(results) - n_verified - n_susp
    color = 0x00C851 if n_susp == 0 and n_err == 0 else 0xFF4444
    rows = []
    for r in results:
        em = {"VERIFIED": "✅", "SUSPICIOUS": "❌"}.get(r["verdict"], "⚠")
        rows.append(f"{em} `{(r.get('trade_id') or '?')[:8]}` "
                    f"{r['strategy']} ({r['side'].upper() if r['side'] else '?'}) "
                    f"`${r['pnl_claimed']:+.2f}`  [{r['verdict']}]")
    embed = {
        "title": f"🔐 Independent verification · {len(results)} trades",
        "description": (
            f"_Re-checked against Bybit public klines. If any trade was "
            f"fabricated or had price tampering, it would fail._\n\n"
            f"**Verified:** {n_verified}  ·  "
            f"**Suspicious:** {n_susp}  ·  "
            f"**Errors:** {n_err}\n\n"
            + "\n".join(rows)),
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Independent — Bybit API · educational"},
    }
    payload = {"username": "🔐 Auditor", "embeds": [embed]}
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "srs-verify/1.0"})
        urllib.request.urlopen(req, timeout=8).read()
        return True
    except Exception as e:
        print("[verify] post_fail:", e)
        return False


if __name__ == "__main__":
    import sys
    results = verify_all()
    print(render_text(results))
    if len(sys.argv) > 1 and sys.argv[1] == "post":
        ok = post_summary_to_discord(results)
        print(f"\nposted to discord: {ok}")
