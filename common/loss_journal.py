"""
loss_journal.py — Auto-record every losing trade with full forensic context.

When a SL event fires, this module captures:
  • Trade basics (entry/exit/SL/strategy/side/pnl/R)
  • Pre-entry context (last 8 closed 15m bars before entry)
  • During-trade bars (every 15m bar between open and close)
  • Max favorable excursion (MFE) — best R the trade reached
  • Max adverse excursion (MAE) — worst R the trade reached
  • Did price reach TP1? (would the trade have won if held?)
  • Time-of-day + day-of-week (low-liquidity windows tend to fail more)
  • Any prior bar with > 50% body rejection wick (liquidity-grab fingerprint)

Each loss is appended as one JSON line to:
  /home/ubuntu/common/loss_journal.jsonl

Used by /losses slash command (operator review) + offline pattern analysis.
"""
from __future__ import annotations

import os
import json
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Optional

JOURNAL_PATH = "/home/ubuntu/common/loss_journal.jsonl"

EXCHANGE = os.environ.get("ZONE_ALERTER_EXCHANGE", "bybit").lower()


def _fetch_klines_around(timestamp_ms: int, before_bars: int = 12,
                         after_bars: int = 16) -> list:
    """Fetch 15m klines covering [-before, +after] bars around a timestamp.

    Returns list of dicts (chronological): {ts, open, high, low, close}.
    """
    interval_ms = 15 * 60 * 1000
    end_ts = timestamp_ms + after_bars * interval_ms
    n = before_bars + after_bars + 4
    try:
        if EXCHANGE == "bybit":
            url = (f"https://api.bybit.com/v5/market/kline?"
                   f"category=linear&symbol=BTCUSDT&interval=15"
                   f"&limit={min(n, 200)}&end={end_ts}")
            data = json.loads(urllib.request.urlopen(url, timeout=8).read())
            rows = data["result"]["list"]
            rows.reverse()   # chronological
            return [{
                "ts": int(r[0]),
                "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]),
            } for r in rows]
        else:
            # Binance fallback
            url = (f"https://api.binance.com/api/v3/klines?"
                   f"symbol=BTCUSDT&interval=15m&limit={n}&endTime={end_ts}")
            rows = json.loads(urllib.request.urlopen(url, timeout=8).read())
            return [{
                "ts": int(r[0]),
                "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]),
            } for r in rows]
    except Exception as e:
        print("[loss_journal] kline_fetch_fail:", e)
        return []


def _detect_liquidity_grab(bars: list, threshold_pct: float = 0.55) -> Optional[Dict]:
    """Look for a 'liquidity grab' bar: large wick where close gives back
    >threshold_pct of the bar's range. This is the pattern that killed Trade #3."""
    for b in bars:
        rng = b["high"] - b["low"]
        if rng <= 0:
            continue
        body_top = max(b["open"], b["close"])
        upper_wick = b["high"] - body_top
        give_back_pct = upper_wick / rng
        # Trigger only on bars with at least a 0.6% range (filters noise)
        if rng / b["close"] < 0.006:
            continue
        if give_back_pct >= threshold_pct:
            return {
                "ts_ms": b["ts"],
                "high": b["high"], "close": b["close"],
                "give_back_pct": round(give_back_pct, 3),
                "wick_size_pct": round(upper_wick / b["close"] * 100, 3),
            }
    return None


def record_loss(trade: Dict, exit_event: str, exit_price: float,
                pnl: float, r_val: float) -> bool:
    """Append a rich loss-journal entry for a stopped-out trade."""
    try:
        opened_at = trade.get("opened_at")
        closed_at = trade.get("closed_at") or datetime.now(timezone.utc).isoformat()
        if not opened_at:
            return False
        try:
            open_dt = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
            close_dt = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        except Exception:
            return False
        open_ms = int(open_dt.timestamp() * 1000)
        close_ms = int(close_dt.timestamp() * 1000)

        entry = float(trade.get("entry", 0))
        sl = float(trade.get("sl_original", trade.get("sl_current", 0)))
        side = trade.get("side", "buy")
        risk_R = abs(entry - sl) if (entry and sl) else 0
        tp1 = float(trade.get("tp1", 0) or 0)

        # Fetch klines: 12 bars before entry through 16 bars after open
        bars = _fetch_klines_around(open_ms, before_bars=12, after_bars=16)
        pre_entry = [b for b in bars if b["ts"] < open_ms][-8:]
        in_trade = [b for b in bars if open_ms <= b["ts"] <= close_ms]

        # Compute MFE / MAE
        mfe_R = None
        mae_R = None
        reached_tp1 = False
        if in_trade and risk_R > 0:
            highest = max(b["high"] for b in in_trade)
            lowest = min(b["low"] for b in in_trade)
            if side == "buy":
                mfe_R = (highest - entry) / risk_R
                mae_R = (lowest - entry) / risk_R
                reached_tp1 = bool(tp1 and highest >= tp1)
            else:
                mfe_R = (entry - lowest) / risk_R
                mae_R = (entry - highest) / risk_R
                reached_tp1 = bool(tp1 and lowest <= tp1)

        # Look for liquidity-grab pattern in last 6 pre-entry bars
        lg = _detect_liquidity_grab(pre_entry[-6:]) if pre_entry else None

        entry_dt = open_dt
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "trade_id": trade.get("id"),
            "strategy": trade.get("strategy",
                                  trade.get("method",
                                            trade.get("strategy_name", "?"))),
            "symbol": trade.get("symbol", "BTCUSDT"),
            "side": side,
            "interval": trade.get("interval", "?"),
            "exit_event": exit_event,
            "opened_at": str(opened_at),
            "closed_at": str(closed_at),
            "duration_min": int((close_dt - open_dt).total_seconds() / 60),
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "exit_price": exit_price,
            "risk_pct": round(risk_R / entry * 100, 3) if entry else None,
            "pnl": pnl,
            "r_realized": r_val,
            "mfe_R": round(mfe_R, 3) if mfe_R is not None else None,
            "mae_R": round(mae_R, 3) if mae_R is not None else None,
            "reached_tp1": reached_tp1,
            # Time-of-day fingerprint
            "entry_hour_utc": entry_dt.hour,
            "entry_dow": entry_dt.strftime("%a"),    # Mon/Tue/...
            "low_liquidity_window": entry_dt.hour in (20, 21, 22, 23, 0, 1, 2, 3, 4),
            # Pre-entry context (compact OHLC)
            "pre_entry_bars_15m": [
                {"t": datetime.fromtimestamp(b["ts"]/1000, tz=timezone.utc).strftime("%m-%d %H:%M"),
                 "o": round(b["open"], 2), "h": round(b["high"], 2),
                 "l": round(b["low"], 2),  "c": round(b["close"], 2)}
                for b in pre_entry
            ],
            "in_trade_bars_15m": [
                {"t": datetime.fromtimestamp(b["ts"]/1000, tz=timezone.utc).strftime("%m-%d %H:%M"),
                 "o": round(b["open"], 2), "h": round(b["high"], 2),
                 "l": round(b["low"], 2),  "c": round(b["close"], 2)}
                for b in in_trade
            ],
            # Pattern fingerprints
            "liquidity_grab_pre_entry": lg,
        }

        os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
        with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        print(f"[loss_journal] recorded: trade={trade.get('id')} "
              f"event={exit_event} pnl={pnl} mfe={mfe_R} mae={mae_R}")
        return True
    except Exception as e:
        print("[loss_journal] record_fail:", e)
        return False


def list_losses(limit: int = 50) -> list:
    """Read recent loss-journal entries for /losses command etc."""
    if not os.path.exists(JOURNAL_PATH):
        return []
    out = []
    with open(JOURNAL_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out[-limit:]


def summary() -> Dict:
    """Aggregate stats across all recorded losses."""
    losses = list_losses(10000)
    n = len(losses)
    if n == 0:
        return {"n": 0}
    by_hour = {}
    by_strategy = {}
    by_dow = {}
    avg_mfe = []
    avg_mae = []
    n_lg = 0
    n_lowliq = 0
    n_reached_tp1 = 0
    for l in losses:
        h = l.get("entry_hour_utc")
        if h is not None:
            by_hour[h] = by_hour.get(h, 0) + 1
        s = l.get("strategy", "?")
        by_strategy[s] = by_strategy.get(s, 0) + 1
        dow = l.get("entry_dow", "?")
        by_dow[dow] = by_dow.get(dow, 0) + 1
        if l.get("mfe_R") is not None:
            avg_mfe.append(l["mfe_R"])
        if l.get("mae_R") is not None:
            avg_mae.append(l["mae_R"])
        if l.get("liquidity_grab_pre_entry"):
            n_lg += 1
        if l.get("low_liquidity_window"):
            n_lowliq += 1
        if l.get("reached_tp1"):
            n_reached_tp1 += 1
    return {
        "n": n,
        "by_strategy": by_strategy,
        "by_hour_utc": by_hour,
        "by_dow": by_dow,
        "avg_mfe_R": round(sum(avg_mfe)/len(avg_mfe), 2) if avg_mfe else None,
        "avg_mae_R": round(sum(avg_mae)/len(avg_mae), 2) if avg_mae else None,
        "pct_after_liquidity_grab": round(n_lg/n*100, 1),
        "pct_low_liquidity_hour": round(n_lowliq/n*100, 1),
        "pct_reached_tp1_then_failed": round(n_reached_tp1/n*100, 1),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        import json as _j
        print(_j.dumps(summary(), indent=2, default=str))
    else:
        for l in list_losses(20):
            print(f"  {l.get('closed_at','?')[:19]}  "
                  f"{l.get('strategy','?'):<20}  "
                  f"entry ${l.get('entry',0):,.0f}  "
                  f"R={l.get('r_realized',0):+.2f}  "
                  f"MFE={l.get('mfe_R'):.2f}  MAE={l.get('mae_R'):.2f}  "
                  f"LG={'yes' if l.get('liquidity_grab_pre_entry') else 'no'}  "
                  f"hr={l.get('entry_hour_utc')}")
