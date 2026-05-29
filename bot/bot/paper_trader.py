"""
bot/paper_trader.py — Paper trading system (full simulation).

Behaviour:
  • Opens a trade on every signal at CURRENT market price
  • Background loop checks prices every 2 min against SL/TP levels
  • TP1 → close 33 % + move SL to entry (breakeven)
  • TP2 → close another 33 %
  • TP3 → close remaining 34 % (full close)
  • SL  → close 100 % of remaining position
  • Sends Discord notification on every close
  • Tracks: win rate, profit factor, net-R, monthly P&L
  • Never resets on restart (JSON persistent store)
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

PAPER_FILE       = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "paper_trades.json",
)
STARTING_BALANCE = 10_000.0
RISK_PCT         = 0.01          # 1 % of balance per trade
TP1_FRACTION     = 0.33

# ─── LOSS_JOURNAL_v1: record full forensic context on every SL hit ─────
def _record_loss_journal(trade, exit_event, exit_price, pnl, r_val):
    """Fire-and-forget call to loss_journal. Never raises."""
    try:
        import sys as _sys
        if "/home/ubuntu/common" not in _sys.path:
            _sys.path.insert(0, "/home/ubuntu/common")
        import loss_journal as _lj
        _lj.record_loss(trade, exit_event, exit_price, pnl, r_val)
    except Exception as _e:
        print("[paper] loss_journal_call_fail:", _e)
# ─── /LOSS_JOURNAL_v1 ───
# ─── FORWARD_LEDGER_v1: write closes to canonical ledger ─────────────
_FORWARD_LEDGER_PATH = "/home/ubuntu/common/forward_results.jsonl"
def _append_forward_result(trade, event, pnl, r_val, price):
    """Append one close-event line to /home/ubuntu/common/forward_results.jsonl
    so data_health, dashboard, and the public ledger see resolved trades."""
    try:
        from datetime import datetime as _dt, timezone as _tz
        line = {
            "ts": _dt.now(_tz.utc).isoformat(),
            "event": "close",
            "system": trade.get("strategy", trade.get("method", trade.get("strategy_name", "?"))),
            "symbol": trade.get("symbol", "BTCUSDT"),
            "side": trade.get("side", "buy"),
            "interval": trade.get("interval", "4h"),
            "status": event,
            "opened_at": trade.get("opened_at"),
            "exit_time": _dt.now(_tz.utc).isoformat(),
            "entry": trade.get("entry"),
            "exit": price,
            "pnl": pnl,
            "r": r_val,
            "trade_id": trade.get("id"),
        }
        os.makedirs(os.path.dirname(_FORWARD_LEDGER_PATH), exist_ok=True)
        with open(_FORWARD_LEDGER_PATH, "a") as _f:
            _f.write(json.dumps(line, default=str) + "\n")
    except Exception as _e:
        print("[paper] forward_ledger_append_fail:", _e)

    # ─── ML OUTCOME RECORDING ───────────────────────────────────────
    # Feed every close back to the ML filter so it can learn.
    # Posts the outcome to the ML channel (if the trade had a score).
    try:
        import sys as _sys
        _sys.path.insert(0, "/home/ubuntu/common")
        import ml_filter as _mf
        import ml_alerter as _ma
        outcome = {
            "strategy":       trade.get("strategy") or trade.get("method"),
            "side":           trade.get("side"),
            "exit_event":     event,
            "exit_price":     price,
            "entry_price":    trade.get("entry"),
            "realized_pnl":   pnl,
            "realized_R":     r_val,
            "ml_score":       trade.get("ml_score"),
            "ml_tier":        trade.get("ml_tier"),
            "opened_at":      trade.get("opened_at"),
        }
        _mf.record_outcome(str(trade.get("id", "")), outcome)
        if trade.get("ml_score") is not None:
            score_stub = {
                "tier":     trade.get("ml_tier"),
                "score":    trade.get("ml_score"),
                "features": {"strategy": trade.get("strategy")},
            }
            outcome_stub = {
                "realized_R":         r_val,
                "realized_pnl_usdt":  pnl,
                "exit_price":         price,
            }
            _ma.post_outcome(score_stub, outcome_stub)
    except Exception as _ml_e:
        print("[paper] ml_outcome_record_fail:", _ml_e)
# ─── /FORWARD_LEDGER_v1 ───
         # close 33% at TP1, let 67% run to TP2/TP3
TP2_FRACTION     = 0.5          # close 50 % of remaining at TP2 (=33 % of original)
# TP3 → closes whatever is left


# ── File I/O ──────────────────────────────────────────────────────────

def _load_paper() -> dict:
    if os.path.exists(PAPER_FILE):
        try:
            with open(PAPER_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "balance" in data:
                # back-compat: ensure new keys exist
                data.setdefault("open_trades", [])
                data.setdefault("closed_trades", [])
                return data
        except Exception:
            pass
    return {
        "balance":       STARTING_BALANCE,
        "open_trades":   [],
        "closed_trades": [],
    }


def _save_paper(store: dict) -> None:
    os.makedirs(os.path.dirname(PAPER_FILE), exist_ok=True)
    with open(PAPER_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, default=str)


# ── Live shadow close helper (added 2026-05-11) ──────────────────────
# Whenever paper closes a trade (SL/TP/time-stop/PARTIAL_WIN/etc.), this
# helper force-closes the corresponding Bybit position so live mirrors
# paper exactly. Wrapped in try/except so paper logic is never affected
# by live failures.

def _close_live_for(trade: dict) -> None:
    try:
        import sys as _sys
        _sys.path.insert(0, "/home/ubuntu/common")
        import live_trader as _lt
        strat_name = trade.get("strategy") or trade.get("method") or ""
        if _lt.is_live_for(strat_name):
            _lt.close_paper_trade(
                paper_trade_id=str(trade.get("id", "")),
                exit_price=float(trade.get("exit_price", 0) or 0),
                exit_reason=str(trade.get("status", "?")),
            )
    except Exception as _e:
        print("[paper_trader] live close failed (paper unaffected):", _e)


def _partial_close_live_for(trade: dict, fraction_of_original: float,
                             exit_price: float, reason: str) -> None:
    """Mirror a paper TP1/TP2 partial into Bybit (closes same fraction)."""
    try:
        import sys as _sys
        _sys.path.insert(0, "/home/ubuntu/common")
        import live_trader as _lt
        strat_name = trade.get("strategy") or trade.get("method") or ""
        if _lt.is_live_for(strat_name):
            _lt.partial_close_paper_trade(
                paper_trade_id=str(trade.get("id", "")),
                fraction_of_original=fraction_of_original,
                exit_price=float(exit_price),
                exit_reason=reason,
            )
    except Exception as _e:
        print("[paper_trader] partial live close failed (paper unaffected):", _e)


def _update_live_sl_for(trade: dict, new_sl: float) -> None:
    """Mirror a paper SL change (e.g., BE+buffer at TP1) onto Bybit.
    Lets the user SEE the SL move on their Bybit account when paper moves it."""
    try:
        import sys as _sys
        _sys.path.insert(0, "/home/ubuntu/common")
        import live_trader as _lt
        strat_name = trade.get("strategy") or trade.get("method") or ""
        if _lt.is_live_for(strat_name):
            _lt.update_live_sl(str(trade.get("id", "")), float(new_sl))
    except Exception as _e:
        print("[paper_trader] live SL update failed:", _e)


# ── Open trade ────────────────────────────────────────────────────────

def open_paper_trade(sig: dict) -> dict:
    """
    Called immediately when a signal is sent.
    Returns the new trade record (empty dict on failure).
    """
    store   = _load_paper()

    # -- Position limits (5-strategy system: all buy BTCUSDT) ──────────
    import config as _cfg
    open_trades = store["open_trades"]
    max_open = getattr(_cfg, "MAX_OPEN_TRADES", 5)
    if len(open_trades) >= max_open:
        print(f"[PAPER] REJECT {sig.get('symbol','')} -- max {max_open} open trades reached ({len(open_trades)} open)")
        return {}
    # Block same STRATEGY from opening twice (but allow different strategies on same symbol)
    strat_name = sig.get("strategy_name", sig.get("method", ""))
    if strat_name and any(t.get("strategy", t.get("method", "")) == strat_name for t in open_trades):
        print(f"[PAPER] REJECT {sig.get('symbol','')} -- strategy {strat_name} already has open trade")
        return {}

    balance = store["balance"]

    entry = float(sig.get("entry", 0) or 0)
    sl    = float(sig.get("sl",    0) or 0)
    tp1   = float(sig.get("tp1",   0) or 0)
    tp2   = float(sig.get("tp2",   0) or 0)
    tp3   = float(sig.get("tp3",   tp2) or tp2)

    if entry <= 0 or sl <= 0 or tp1 <= 0:
        return {}

    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return {}

    risk_amount    = balance * RISK_PCT
    position_value = risk_amount / (sl_dist / entry)   # size such that SL = 1 R
    units_total    = position_value / entry

    # R-value helpers
    r_size = sl_dist  # 1 R = distance to SL

    record = {
        "id":              str(uuid.uuid4())[:8],
        "symbol":          sig.get("symbol", ""),
        "side":            sig.get("side", "buy").lower(),
        "interval":        sig.get("interval", "15m"),
        "strategy":        sig.get("strategy_name", sig.get("method", "SMC")),
        "method":          sig.get("method", ""),
        "probability":     round(float(sig.get("probability", 0) or 0), 4),
        # Price levels
        "entry":           round(entry, 8),
        "sl_original":     round(sl, 8),
        "sl_current":      round(sl, 8),      # moves to entry after TP1
        "tp1":             round(tp1, 8),
        "tp2":             round(tp2, 8),
        "tp3":             round(tp3, 8),
        # Position sizing
        "risk_amount":     round(risk_amount, 4),
        "position_value":  round(position_value, 4),
        "units_total":     round(units_total, 8),
        "units_remaining": round(units_total, 8),
        "r_size":          round(r_size, 8),
        # Accounting
        "balance_at_open": round(balance, 4),
        "realized_pnl":    0.0,               # from partial closes
        "realized_r":      0.0,
        "partial_closes":  [],
        # State
        "tp1_hit":         False,
        "tp2_hit":         False,
        "opened_at":       datetime.now(timezone.utc).isoformat(),
        "status":          "OPEN",
    }

    # ── ML FILTER (Phase 0 heuristic) ────────────────────────────────
    # Scores the signal, posts a review to the ML channel, and tags
    # the record with the score. In shadow mode the trade still opens.
    # In gate mode (ML_FILTER_MODE=gate) only HIGH tier passes.
    ml_score = None
    try:
        import sys as _sys
        _sys.path.insert(0, "/home/ubuntu/common")
        import ml_filter as _mf
        import ml_alerter as _ma
        ml_sig = {
            "strategy": record.get("strategy"),
            "method":   record.get("method"),
            "side":     record.get("side"),
            "entry":    record.get("entry"),
            "sl":       record.get("sl_original"),
            "tp":       record.get("tp1"),
        }
        ml_score = _mf.score_signal(ml_sig)
        record["ml_score"] = ml_score.get("score")
        record["ml_tier"] = ml_score.get("tier")
        record["ml_model"] = ml_score.get("model")
        record["ml_mode"] = ml_score.get("mode")
        # Post review embed (best-effort, never fails the trade)
        try:
            _ma.post_signal_review(ml_score, ml_sig)
        except Exception as _pe:
            print("[paper_trader] ml alert post failed:", _pe)
        # Gate enforcement (only in gate mode)
        if not ml_score.get("allow", True):
            print(f"[PAPER] ML REJECTED {record.get('strategy')} "
                  f"tier={ml_score.get('tier')} score={ml_score.get('score'):.3f}")
            return {}
    except Exception as _e:
        print("[paper_trader] ML filter failed (paper continues):", _e)

    store["open_trades"].append(record)
    _save_paper(store)

    # ── LIVE shadow order (no-op if strategy not whitelisted) ────────
    # Mirrors the paper trade into a real Bybit order. Fully isolated
    # from paper logic — if anything fails in live, paper continues.
    try:
        import sys as _sys
        _sys.path.insert(0, "/home/ubuntu/common")
        import live_trader as _lt
        strat_name = sig.get("strategy") or sig.get("method") or ""
        if _lt.is_live_for(strat_name):
            _lt.open_live_trade(
                strategy=strat_name,
                side="long" if sig.get("side", "buy").lower() == "buy" else "short",
                entry=float(record["entry"]),
                stop=float(record["sl_original"]),
                target=float(record["tp1"]),  # use TP1 as the live take-profit
                paper_trade_id=str(record.get("id", "")),
            )
    except Exception as _e:
        print("[paper_trader] live shadow failed (paper unaffected):", _e)

    return record


# ── Price checking (called by background loop) ─────────────────────

def check_and_update_trades(prices: dict[str, float]) -> list[dict]:
    """
    Called with a {symbol: current_price} dict every 2 minutes.
    Returns a list of close-event dicts for Discord notifications.
    Each event: {"trade": ..., "event": "TP1"|"TP2"|"TP3"|"SL", "pnl": ..., "r": ...}
    """
    store  = _load_paper()
    events = []

    still_open = []
    for trade in store["open_trades"]:
        sym   = trade["symbol"]
        price = prices.get(sym)
        if price is None:
            still_open.append(trade)
            continue

        side      = trade["side"]
        sl        = trade["sl_current"]
        tp1       = trade["tp1"]
        tp2       = trade["tp2"]
        tp3       = trade["tp3"]
        tp1_hit   = trade.get("tp1_hit", False)
        tp2_hit   = trade.get("tp2_hit", False)

        def _price_hit_level(level: float) -> bool:
            if side == "buy":
                return price >= level
            else:
                return price <= level

        def _price_hit_sl() -> bool:
            if side == "buy":
                return price <= sl
            else:
                return price >= sl

        closed = False

        # ── SL check ──────────────────────────────────────────────────
        if _price_hit_sl():
            ev = _partial_close(trade, sl, "SL", fraction=1.0)
            store["balance"] = round(store["balance"] + ev["pnl"], 4)
            # Assign status based on total realized pnl, not just SL label
            total_pnl = trade.get("realized_pnl", 0)
            if total_pnl > 0.01:
                trade["status"] = "PARTIAL_WIN"   # hit TP1/TP2, stopped at breakeven or profit
            elif total_pnl > -0.01:
                trade["status"] = "BREAKEVEN"
            else:
                trade["status"] = "SL"
            trade["closed_at"] = datetime.now(timezone.utc).isoformat()
            trade["balance_after"] = store["balance"]
            store["closed_trades"].append(trade); _close_live_for(trade)
            events.append({"trade": dict(trade), "event": "SL",
                           "pnl": ev["pnl"], "r": ev["r"], "price": sl})
            _append_forward_result(trade, "SL", ev["pnl"], ev["r"], sl)
            _record_loss_journal(trade, "SL", sl, ev["pnl"], ev["r"])
            closed = True

        # ── TP3 check (only after TP1 + TP2 hit) ─────────────────────
        elif tp1_hit and tp2_hit and _price_hit_level(tp3):
            ev = _partial_close(trade, tp3, "TP3", fraction=1.0)
            store["balance"] = round(store["balance"] + ev["pnl"], 4)
            trade["status"]  = "TP3"
            trade["closed_at"] = datetime.now(timezone.utc).isoformat()
            trade["balance_after"] = store["balance"]
            store["closed_trades"].append(trade); _close_live_for(trade)
            events.append({"trade": dict(trade), "event": "TP3",
                           "pnl": ev["pnl"], "r": ev["r"], "price": tp3})
            _append_forward_result(trade, "TP3", ev["pnl"], ev["r"], tp3)
            closed = True

        # ── TP2 check ─────────────────────────────────────────────────
        elif tp1_hit and not tp2_hit and _price_hit_level(tp2):
            ev = _partial_close(trade, tp2, "TP2", fraction=TP2_FRACTION)
            store["balance"] = round(store["balance"] + ev["pnl"], 4)
            trade["tp2_hit"] = True
            # Live: TP2 closes TP2_FRACTION of REMAINING (after TP1 took TP1_FRACTION)
            # = TP2_FRACTION × (1 - TP1_FRACTION) of ORIGINAL
            _partial_close_live_for(trade,
                                    TP2_FRACTION * (1.0 - TP1_FRACTION),
                                    tp2, "TP2")
            events.append({"trade": dict(trade), "event": "TP2",
                           "pnl": ev["pnl"], "r": ev["r"], "price": tp2})
            _append_forward_result(trade, "TP2", ev["pnl"], ev["r"], tp2)

        # ── TP1 check ─────────────────────────────────────────────────
        elif not tp1_hit and _price_hit_level(tp1):
            ev = _partial_close(trade, tp1, "TP1", fraction=TP1_FRACTION)
            store["balance"]    = round(store["balance"] + ev["pnl"], 4)
            trade["tp1_hit"]    = True
            if TP1_FRACTION >= 1.0:
                # 100% close at TP1 — fully done
                trade["status"]       = "WIN"
                trade["closed_at"]    = datetime.now(timezone.utc).isoformat()
                trade["balance_after"] = store["balance"]
                store["closed_trades"].append(trade); _close_live_for(trade)
                events.append({"trade": dict(trade), "event": "TP1",
                               "pnl": ev["pnl"], "r": ev["r"], "price": tp1})
                _append_forward_result(trade, "TP1", ev["pnl"], ev["r"], tp1)
                closed = True
            else:
                _sl_dist = abs(trade["entry"] - trade["sl_original"])
                _profit_buffer = _sl_dist * 0.25
                trade["sl_current"] = round(trade["entry"] + (_profit_buffer if trade["side"]=="buy" else -_profit_buffer), 8)
                # Live: close TP1_FRACTION of ORIGINAL qty on Bybit
                _partial_close_live_for(trade, TP1_FRACTION, tp1, "TP1")
                # Live: mirror the BE+buffer SL move so user sees it on Bybit UI
                _update_live_sl_for(trade, trade["sl_current"])
                events.append({"trade": dict(trade), "event": "TP1",
                               "pnl": ev["pnl"], "r": ev["r"], "price": tp1})
                _append_forward_result(trade, "TP1", ev["pnl"], ev["r"], tp1)

        # ── SBS time stop: close after 3 hours ─────────────────────
        if not closed and trade.get("method") == "SBS":
            from datetime import datetime as _dt, timezone as _tz
            _opened = trade.get("opened_at")
            if _opened:
                try:
                    _open_time = _dt.fromisoformat(_opened)
                    _elapsed_h = (_dt.now(_tz.utc) - _open_time).total_seconds() / 3600
                    if _elapsed_h >= 3.0:
                        # Time stop: close at current price
                        ev = _partial_close(trade, price, "TIME", fraction=1.0)
                        store["balance"] = round(store["balance"] + ev["pnl"], 4)
                        total_pnl = trade.get("realized_pnl", 0)
                        if total_pnl > 0.01:
                            trade["status"] = "TIME_WIN"
                        elif total_pnl > -0.01:
                            trade["status"] = "TIME_FLAT"
                        else:
                            trade["status"] = "TIME_LOSS"
                        trade["closed_at"] = _dt.now(_tz.utc).isoformat()
                        trade["balance_after"] = store["balance"]
                        store["closed_trades"].append(trade); _close_live_for(trade)
                        events.append({"trade": dict(trade), "event": "TIME",
                                       "pnl": ev["pnl"], "r": ev["r"], "price": price})
                        _append_forward_result(trade, "TIME", ev["pnl"], ev["r"], price)
                        closed = True
                        print("[SBS] Time stop: %.1fh elapsed, pnl=$%.2f" % (_elapsed_h, ev["pnl"]))
                except Exception as _ts_err:
                    print("[SBS] Time stop check error: %s" % _ts_err)

        if not closed:
            still_open.append(trade)

    store["open_trades"] = still_open

    # -- Alert detection (deduped: fire once per state change) -----
    closed_trades = store.get("closed_trades", [])
    if len(closed_trades) >= 3:
        last_3 = closed_trades[-3:]
        streak_loss = all(t.get("realized_pnl", 0) < -0.01 for t in last_3)
        if streak_loss:
            # Dedupe key: identify THIS specific 3-loss streak by its
            # most-recent close. Re-firing only happens when a NEW close
            # extends/changes the streak.
            streak_key = str(
                last_3[-1].get("closed_at")
                or last_3[-1].get("id")
                or last_3[-1].get("trade_id")
                or len(closed_trades)
            )
            if store.get("last_streak_alert_key") != streak_key:
                events.append({
                    "trade": None,
                    "event": "ALERT_LOSING_STREAK",
                    "pnl": sum(t.get("realized_pnl", 0) for t in last_3),
                    "r": 0,
                    "price": 0,
                    "message": "⚠️ 3 consecutive losses! Consider pausing."
                })
                store["last_streak_alert_key"] = streak_key
        else:
            # Streak broken (most recent isn't a 3-loss tail) — reset key
            store.pop("last_streak_alert_key", None)

    # Drawdown alert (deduped: fire when crossing 5%, then again only
    # if drawdown deepens by ≥2% from the last alert; reset on recovery)
    peak_balance = max(STARTING_BALANCE, max((t.get("balance_after", STARTING_BALANCE) for t in closed_trades), default=STARTING_BALANCE))
    current_balance = store["balance"]
    drawdown_pct = (peak_balance - current_balance) / peak_balance * 100
    if drawdown_pct > 5:
        last_dd_alert = float(store.get("last_dd_alert_pct", 0))
        if last_dd_alert == 0 or drawdown_pct >= last_dd_alert + 2:
            events.append({
                "trade": None,
                "event": "ALERT_DRAWDOWN",
                "pnl": 0,
                "r": 0,
                "price": 0,
                "message": f"🔴 Drawdown alert: {drawdown_pct:.1f}% from peak ${peak_balance:.0f}"
            })
            store["last_dd_alert_pct"] = drawdown_pct
    elif drawdown_pct < 2:
        # Recovered — clear DD alert state so next breach re-alerts cleanly
        store.pop("last_dd_alert_pct", None)

    # ── SRS: feed trade outcomes to sweep_trader risk management ────
    for ev in events:
        tr = ev.get("trade")
        if tr is None:
            continue
        # Only process fully closed SRS trades
        if tr.get("method") not in ("SRS", "SBS"):
            continue
        if ev["event"] not in ("SL", "TP1", "TP3", "TIME"):
            continue
        try:
            if tr.get("method") == "SBS":
                from sweep_bounce import record_outcome
            else:
                from sweep_trader import record_outcome
            record_outcome(tr.get("realized_pnl", 0), store["balance"])
            print("[SRS] record_outcome called: pnl=%.2f bal=%.2f" % (
                tr.get("realized_pnl", 0), store["balance"]))
            # Forward-test tracker
            from srs_tracker import log_srs_exit
            sl_dist = abs(tr.get("entry", 0) - tr.get("sl_original", tr.get("sl", 0)))
            log_srs_exit(
                entry_price=tr.get("entry", 0),
                exit_price=ev.get("price", 0),
                result=ev["event"],
                pnl=tr.get("realized_pnl", 0),
                sl_dist=sl_dist,
            )
        except Exception as _ro_err:
            print("[SRS] record_outcome error: %s" % _ro_err)

    _save_paper(store)
    return events


def _partial_close(trade: dict, exit_price: float,
                   label: str, fraction: float) -> dict:
    """
    Close `fraction` of remaining units at exit_price.
    Mutates trade in-place. Returns {"pnl": ..., "r": ...}.
    """
    units_to_close = trade["units_remaining"] * fraction
    entry = trade["entry"]
    side  = trade["side"]
    r_size = trade.get("r_size", abs(entry - trade["sl_original"]))

    if side == "buy":
        pnl = (exit_price - entry) * units_to_close
    else:
        pnl = (entry - exit_price) * units_to_close

    r_val = pnl / (r_size * units_to_close) if r_size > 0 else 0

    trade["units_remaining"] = round(trade["units_remaining"] - units_to_close, 8)
    trade["realized_pnl"]    = round(trade.get("realized_pnl", 0) + pnl, 4)
    trade["realized_r"]      = round(trade.get("realized_r",   0) + r_val, 4)

    trade["partial_closes"].append({
        "label":      label,
        "price":      round(exit_price, 8),
        "units":      round(units_to_close, 8),
        "pnl":        round(pnl, 4),
        "r":          round(r_val, 4),
        "time":       datetime.now(timezone.utc).isoformat(),
    })

    return {"pnl": round(pnl, 4), "r": round(r_val, 4)}


# ── Legacy close (for manual / compat) ───────────────────────────────

def close_paper_trade(symbol: str, side: str,
                       exit_price: float, reason: str) -> Optional[dict]:
    """Legacy full-close. Used by /paper manual close if needed."""
    store = _load_paper()
    side  = side.lower()
    idx   = next(
        (i for i, t in enumerate(store["open_trades"])
         if t["symbol"] == symbol and t["side"] == side),
        None,
    )
    if idx is None:
        return None

    trade  = store["open_trades"].pop(idx)
    entry  = trade["entry"]
    units  = trade["units_remaining"]

    if side == "buy":
        pnl = (exit_price - entry) * units
    else:
        pnl = (entry - exit_price) * units

    trade["exit_price"]    = round(exit_price, 8)
    trade["realized_pnl"]  = round(trade.get("realized_pnl", 0) + pnl, 4)
    trade["pnl"]           = trade["realized_pnl"]
    trade["reason"]        = reason
    trade["closed_at"]     = datetime.now(timezone.utc).isoformat()
    trade["status"]        = (
        "WIN" if trade["realized_pnl"] > 0 else
        "SL"  if trade["realized_pnl"] < 0 else "BREAKEVEN"
    )
    store["balance"]           = round(store["balance"] + pnl, 4)
    trade["balance_after"]     = store["balance"]
    store["closed_trades"].append(trade); _close_live_for(trade)
    _save_paper(store)
    return trade


# ── Stats ─────────────────────────────────────────────────────────────

def get_paper_balance() -> float:
    return _load_paper()["balance"]


def get_paper_stats() -> dict:
    store  = _load_paper()
    balance = store["balance"]
    closed  = store["closed_trades"]
    open_t  = store["open_trades"]

    # Classify by realized pnl — status="SL" after TP1 is a win/breakeven, not a loss
    wins      = [t for t in closed if t.get("realized_pnl", t.get("pnl", 0)) >  0.01]
    losses    = [t for t in closed if t.get("realized_pnl", t.get("pnl", 0)) < -0.01]
    breakevens= [t for t in closed if abs(t.get("realized_pnl", t.get("pnl", 0))) <= 0.01]

    total     = len(closed)
    win_count = len(wins)
    loss_count= len(losses)
    win_rate  = win_count / total if total else 0.0

    gross_profit = sum(t.get("realized_pnl", t.get("pnl", 0)) for t in wins)
    gross_loss   = abs(sum(t.get("realized_pnl", t.get("pnl", 0)) for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    net_r = sum(t.get("realized_r", 0) for t in closed)

    total_pnl_pct = (balance - STARTING_BALANCE) / STARTING_BALANCE * 100

    best_trade  = max(closed, key=lambda t: t.get("realized_pnl", t.get("pnl", 0)), default=None)
    worst_trade = min(closed, key=lambda t: t.get("realized_pnl", t.get("pnl", 0)), default=None)

    now = datetime.now(timezone.utc)
    month_start_str = f"{now.year}-{now.month:02d}-01"
    month_trades = [t for t in closed if t.get("closed_at", "") >= month_start_str]
    month_pnl    = sum(t.get("realized_pnl", t.get("pnl", 0)) for t in month_trades)
    bal_before   = balance - month_pnl
    month_pnl_pct= (month_pnl / bal_before * 100) if bal_before > 0 else 0.0

    return {
        "balance":         round(balance, 2),
        "open_trades":     len(open_t),
        "total_trades":    total,
        "wins":            win_count,
        "losses":          loss_count,
        "win_rate":        round(win_rate, 4),
        "profit_factor":   round(profit_factor, 2),
        "net_r":           round(net_r, 2),
        "total_pnl_pct":   round(total_pnl_pct, 2),
        "best_trade":      ({"symbol": best_trade["symbol"].replace("USDT",""),
                             "pnl":    round(best_trade.get("realized_pnl", best_trade.get("pnl", 0)), 2)}
                            if best_trade else None),
        "worst_trade":     ({"symbol": worst_trade["symbol"].replace("USDT",""),
                             "pnl":    round(worst_trade.get("realized_pnl", worst_trade.get("pnl", 0)), 2)}
                            if worst_trade else None),
        "month_pnl":       round(month_pnl, 2),
        "month_pnl_pct":   round(month_pnl_pct, 2),
    }
