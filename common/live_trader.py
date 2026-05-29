"""
live_trader.py — Shadow-live mode: mirror paper signals into real Bybit orders.

Design principle: paper trader is the source of truth for strategy logic.
Live trader is a parallel layer that places identical real orders alongside
paper. Paper logic is COMPLETELY UNCHANGED — if anything fails in live,
paper keeps working.

Activation:
  LIVE_TRADING_ENABLED=true       — master kill switch
  LIVE_STRATEGIES=s2_mss,s3_ote,SMC_CONFLUENCE  — comma-separated whitelist
  LIVE_RISK_USDT=5                — fixed dollar risk per trade
  LIVE_MAX_POSITIONS=3            — cap on simultaneous live positions
  LIVE_SYMBOL=BTCUSDT             — only symbol we trade

State:
  /home/ubuntu/bot/logs/live_trades.json — separate from paper state

If credentials missing or LIVE_TRADING_ENABLED != "true", every method
returns {"status": "disabled"} as a safe no-op. Importing this module
does NOT touch Bybit.
"""
from __future__ import annotations

import os
import json
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, List

try:
    from bybit_client import BybitClient, BybitError
except Exception:
    BybitClient = None
    BybitError = Exception


STATE_FILE = "/home/ubuntu/bot/logs/live_trades.json"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip()


def _post_live_alert(title: str, description: str, color: int = 0xF44336) -> None:
    """Post a live-trading alert to Discord. Best-effort; never raises.

    Sends to DISCORD_WATCH_WEBHOOK so live errors land in the same channel
    as other system alerts. Silent failure → Discord notification.
    """
    webhook = _env("DISCORD_WATCH_WEBHOOK") or _env("DISCORD_WEBHOOK_URL_BOOK_CLOSED")
    if not webhook:
        return
    try:
        import urllib.request as _ur
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        net = "MAINNET" if not _env("BYBIT_TESTNET").lower() in ("1", "true", "yes") else "testnet"
        payload = {
            "username": "🤖 Live Trader",
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": f"Bybit {net} · live shadow layer"},
                "timestamp": _dt.now(_tz.utc).isoformat(),
            }],
        }
        req = _ur.Request(
            webhook, data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "live-trader/1.0"})
        _ur.urlopen(req, timeout=8).read()
    except Exception as _e:
        # Never let alert-posting crash the trader
        print(f"[live_trader] alert_post_failed: {_e}")


def _is_enabled() -> bool:
    return _env("LIVE_TRADING_ENABLED").lower() in ("1", "true", "yes")


def _live_strategies() -> List[str]:
    raw = _env("LIVE_STRATEGIES")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _max_positions() -> int:
    try:
        return int(_env("LIVE_MAX_POSITIONS", "3"))
    except Exception:
        return 3


def _risk_usdt() -> float:
    """Risk per trade in USDT.

    Priority:
      1. LIVE_RISK_PCT (e.g. 0.01 = 1% of current Bybit balance) — preferred,
         scales with account growth/drawdown
      2. LIVE_RISK_USDT (fixed dollar amount) — fallback

    Caches balance for 60s to avoid hammering Bybit on rapid scans.
    """
    pct_str = _env("LIVE_RISK_PCT")
    if pct_str:
        try:
            pct = float(pct_str)
            if pct <= 0 or pct > 0.5:   # sanity: max 50% per trade
                return 5.0
            if BybitClient is None or not _env("BYBIT_API_KEY"):
                return 5.0
            # Simple in-process cache: 60s TTL
            global _BAL_CACHE
            try:
                _BAL_CACHE
            except NameError:
                _BAL_CACHE = {"balance": 0.0, "ts": 0}
            import time as _time
            now = _time.time()
            if now - _BAL_CACHE["ts"] > 60:
                try:
                    _BAL_CACHE["balance"] = BybitClient().get_balance("USDT")
                    _BAL_CACHE["ts"] = now
                except Exception:
                    pass
            risk = _BAL_CACHE["balance"] * pct
            # Floor to $1 minimum (otherwise qty would round to 0)
            return max(1.0, round(risk, 2))
        except Exception:
            pass
    # Fallback to fixed dollar
    try:
        return float(_env("LIVE_RISK_USDT", "5"))
    except Exception:
        return 5.0


def _symbol() -> str:
    return _env("LIVE_SYMBOL", "BTCUSDT")


# ─── State persistence ───────────────────────────────────────────────

def _load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"open_orders": [], "closed_orders": []}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"open_orders": [], "closed_orders": []}


def _save_state(s: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


# ─── Public API used by paper runners ────────────────────────────────

def is_live_for(strategy: str) -> bool:
    """Is THIS strategy whitelisted for live trading right now?"""
    if not _is_enabled():
        return False
    if BybitClient is None:
        return False
    if not _env("BYBIT_API_KEY") or not _env("BYBIT_API_SECRET"):
        return False
    return strategy in _live_strategies()


def open_live_trade(strategy: str, side: str,
                    entry: float, stop: float, target: float,
                    paper_trade_id: str) -> Dict:
    """Mirror a paper signal into a real Bybit order.

    Args:
        strategy: e.g. 's3_ote_4h' or 'SMC_CONFLUENCE'
        side: 'long' or 'short' (paper convention)
        entry: signal entry price (used for sizing only — actual fill is market)
        stop: SL price
        target: TP price
        paper_trade_id: link back to the paper trade that triggered this

    Returns:
        {"status": "opened", "order_id": "...", "qty": ..., "live_id": "..."}
        OR {"status": "skipped", "reason": "..."}
        OR {"status": "error", "err": "..."}
    """
    if not is_live_for(strategy):
        return {"status": "skipped", "reason": "not whitelisted or disabled"}

    state = _load_state()
    open_orders = state.get("open_orders", [])

    # Cap on simultaneous live positions
    if len(open_orders) >= _max_positions():
        return {"status": "skipped",
                "reason": f"max_positions ({_max_positions()}) reached"}

    # NOTE (2026-05-11): anti-aggregation guard REMOVED per operator
    # request. Every signal places a live order with 1% risk. Operator
    # accepts that when 2+ strategies fire on same symbol, Bybit one-way
    # mode aggregates them into one position with one SL — a single SL
    # hit then closes the whole aggregate, potentially losing 2-3x the
    # intended per-trade risk. Trade-off: max signal coverage > strict
    # per-trade loss capping.

    # Compute qty from fixed dollar risk
    risk_usdt = _risk_usdt()
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return {"status": "error", "err": "stop_distance <= 0"}
    qty_btc = risk_usdt / stop_distance
    # Round to Bybit's BTCUSDT step size (0.001)
    qty_btc = round(qty_btc, 3)
    if qty_btc < 0.001:
        return {"status": "skipped",
                "reason": f"qty {qty_btc} below 0.001 minimum"}

    bybit_side = "Buy" if side == "long" else "Sell"
    try:
        client = BybitClient()
        # Place order with paper's SL/TP as backstop. After fill, if this
        # is the 2nd+ trade in the aggregate, OVERRIDE the Bybit SL with
        # an aggregate-aware SL so total loss across N trades stays at
        # N × risk_usdt.
        order = client.place_market_order(
            symbol=_symbol(),
            side=bybit_side,
            qty=qty_btc,
            stop_loss=stop,         # paper's intended SL (backstop)
            take_profit=target,     # paper's intended TP
        )

        # ── POST-FILL AUTO-RESIZE (2026-05-13) ───────────────────────
        # Entry slippage problem: bot computes qty from SIGNAL price,
        # but Bybit market order can fill 100s of dollars worse than
        # signal. The signal SL stays the same, so distance grows, so
        # actual risk balloons (e.g. May 13 s3_ote: signal $80,086 →
        # fill $80,235, SL $79,951 → real risk = $284×0.036 = $10
        # instead of intended $5).
        # FIX: After fill, query actual avg fill price. If actual risk
        # to signal SL > risk_usdt × 1.2, shrink position via reduce-
        # only order so risk fits the budget.
        try:
            import time as _t
            _t.sleep(0.8)
            pos = client.get_position(_symbol())
            if pos is not None and float(pos.get("size", 0)) > 0:
                actual_fill = float(pos["entry"])
                if bybit_side == "Buy":
                    actual_dist = max(actual_fill - stop, 0.01)
                else:
                    actual_dist = max(stop - actual_fill, 0.01)
                actual_risk = actual_dist * qty_btc
                max_allowed = risk_usdt * 1.2   # 20% buffer for fees/slip
                if actual_risk > max_allowed:
                    new_qty = round(max_allowed / actual_dist, 3)
                    # NOTE: in aggregate (multi-trade) positions, pos["size"]
                    # is the AGGREGATE size, but we only own qty_btc of it.
                    # We shrink THIS order's contribution by selling excess.
                    excess = round(qty_btc - new_qty, 3)
                    if excess >= 0.001:
                        opp = "Sell" if bybit_side == "Buy" else "Buy"
                        client.place_market_order(
                            symbol=_symbol(),
                            side=opp,
                            qty=excess,
                            reduce_only=True,
                        )
                        print(f"[live_trader] {strategy} AUTO-RESIZE: "
                              f"fill=${actual_fill:.2f} signal=${entry:.2f} "
                              f"SL=${stop:.2f} dist=${actual_dist:.2f} "
                              f"would_risk=${actual_risk:.2f} > "
                              f"budget=${max_allowed:.2f} → shrunk "
                              f"qty {qty_btc}→{new_qty} (sold {excess})")
                        _post_live_alert(
                            title=f"📉 Auto-resized — {strategy}",
                            description=(
                                f"Entry slipped: signal ${entry:,.2f} → "
                                f"fill ${actual_fill:,.2f}.\n"
                                f"At signal SL ${stop:,.2f}, real risk would "
                                f"be **${actual_risk:.2f}** (budget "
                                f"${max_allowed:.2f}).\n"
                                f"Shrunk position **{qty_btc} → {new_qty} BTC** "
                                f"(sold {excess} reduce-only).\n"
                                f"Now risks ~${new_qty * actual_dist:.2f}."
                            ),
                            color=0xFFB300,
                        )
                        qty_btc = new_qty
        except Exception as _re:
            print(f"[live_trader] auto-resize failed: {_re}")
            _post_live_alert(
                title=f"⚠ Auto-resize FAILED — {strategy}",
                description=(
                    f"Couldn't auto-resize after fill — `{_re}`.\n"
                    f"Position is still bounded by signal SL ${stop:,.2f}, "
                    f"but actual risk may exceed budget."
                ),
                color=0xE74C3C,
            )

        # ── AGGREGATE-AWARE SL OVERRIDE (restored 2026-05-13) ───────
        # If this is the 2nd+ same-direction trade in the aggregate,
        # the previous trade's SL just got overwritten by THIS trade's SL.
        # That can leave the FIRST trade exposed to a wider stop.
        # Fix: recompute the aggregate SL based on N × risk_usdt total
        # budget. Set it on Bybit so the ENTIRE aggregate closes at the
        # tighter level, capping total loss at N × $5.
        try:
            import time as _t
            _t.sleep(0.5)   # let position state refresh after order fill
            pos = client.get_position(_symbol())
            if pos is not None and pos["size"] > 0:
                same_side_open = sum(
                    1 for o in open_orders
                    if o.get("bybit_side") == bybit_side
                )
                n_trades = same_side_open + 1   # +1 for this new trade
                if n_trades >= 2:
                    total_risk = n_trades * risk_usdt
                    agg_qty = float(pos["size"])
                    avg_entry = float(pos["entry"])
                    sl_dist = total_risk / agg_qty
                    if bybit_side == "Buy":
                        agg_sl = avg_entry - sl_dist
                    else:
                        agg_sl = avg_entry + sl_dist
                    client.set_position_sl_tp(
                        symbol=_symbol(),
                        stop_loss=round(agg_sl, 2),
                        take_profit=round(target, 2),
                    )
                    print(f"[live_trader] {strategy} AGGREGATE SL OVERRIDE: "
                          f"agg_qty={agg_qty} avg=${avg_entry:.0f} "
                          f"n={n_trades} budget=${total_risk:.2f} "
                          f"new_SL=${agg_sl:.0f}")
        except Exception as _e:
            # If override fails, paper's backstop SL is still active.
            # Log but don't fail the trade.
            print(f"[live_trader] aggregate SL override failed: {_e} "
                  f"— paper SL still active as backstop")
    except BybitError as e:
        err_msg = str(e)
        print(f"[live_trader] order_fail strategy={strategy}: {err_msg}")
        _post_live_alert(
            title=f"⚠️ Live order FAILED — {strategy}",
            description=(
                f"**Side:** {bybit_side}\n"
                f"**Qty:** {qty_btc} BTC (risk ${risk_usdt})\n"
                f"**Entry:** ${entry:,.2f}\n"
                f"**SL:** ${stop:,.2f} · **TP:** ${target:,.2f}\n"
                f"\n**Error:** `{err_msg}`\n"
                f"\n_Paper trade still recorded; live shadow did NOT place._"
            ),
        )
        return {"status": "error", "err": err_msg}

    live_id = "live_" + uuid.uuid4().hex[:8]
    record = {
        "live_id": live_id,
        "paper_trade_id": paper_trade_id,
        "strategy": strategy,
        "side": side,
        "bybit_side": bybit_side,
        "qty": qty_btc,
        "entry_signal": entry,
        "stop": stop,
        "target": target,
        "risk_usdt": risk_usdt,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "bybit_order_id": order.get("orderId"),
        "status": "OPEN",
    }
    open_orders.append(record)
    state["open_orders"] = open_orders
    _save_state(state)

    print(f"[live_trader] OPENED {bybit_side} {qty_btc} {_symbol()} "
          f"strategy={strategy} order_id={order.get('orderId')}")
    return {"status": "opened", "order_id": order.get("orderId"),
            "qty": qty_btc, "live_id": live_id}


def close_paper_trade(paper_trade_id: str,
                       exit_price: Optional[float] = None,
                       exit_reason: str = "PAPER_CLOSE") -> Dict:
    """Force-close the live position corresponding to a paper trade.

    Called by paper runners whenever a paper trade closes (SL/TP/time-stop).
    Looks up the live order with matching paper_trade_id, places a reduce-only
    market order to close that qty on Bybit, marks the live record as closed.

    This is what makes LIVE follow PAPER (the user's explicit requirement):
    whatever paper decides, live mirrors immediately.

    Returns dict with status: closed / no_live_record / already_flat / error
    """
    if not _is_enabled() or BybitClient is None:
        return {"status": "disabled"}
    if not _env("BYBIT_API_KEY"):
        return {"status": "no_credentials"}

    state = _load_state()
    open_orders = state.get("open_orders", [])

    # Find matching live record
    live_idx = None
    live_order = None
    for i, o in enumerate(open_orders):
        if o.get("paper_trade_id") == paper_trade_id:
            live_idx = i
            live_order = o
            break
    if live_order is None:
        return {"status": "no_live_record",
                "paper_trade_id": paper_trade_id}

    try:
        client = BybitClient()
        pos = client.get_position(_symbol())
        if pos is None or float(pos.get("size", 0)) < 0.0001:
            # Bybit already closed it (probably hit SL/TP server-side or another
            # paper close already cleared the aggregate). Just mark our record.
            close_state = "ALREADY_FLAT"
            bybit_exit = None
        else:
            # Place reduce-only market order opposite to live position side
            opposite = "Sell" if live_order["bybit_side"] == "Buy" else "Buy"
            close_qty = min(float(live_order["qty"]), float(pos["size"]))
            # Round to 0.001 step
            close_qty = round(close_qty, 3)
            if close_qty < 0.001:
                close_state = "QTY_TOO_SMALL"
                bybit_exit = None
            else:
                client.place_market_order(
                    symbol=_symbol(),
                    side=opposite,
                    qty=close_qty,
                    reduce_only=True,
                )
                # Capture exit price (last ticker — actual fill differs slightly)
                import time as _t
                _t.sleep(1.5)
                try:
                    bybit_exit = client.get_ticker(_symbol())
                except Exception:
                    bybit_exit = None
                close_state = "PAPER_CLOSE"

        # Mark live record closed
        live_order["closed_at"] = datetime.now(timezone.utc).isoformat()
        live_order["status"] = close_state
        live_order["paper_exit_price"] = exit_price
        live_order["paper_exit_reason"] = exit_reason
        live_order["bybit_exit_approx"] = bybit_exit

        state["open_orders"] = open_orders[:live_idx] + open_orders[live_idx + 1:]
        state["closed_orders"] = state.get("closed_orders", []) + [live_order]
        _save_state(state)

        print(f"[live_trader] CLOSED live {live_order['live_id']} "
              f"({live_order['strategy']}) reason={exit_reason} "
              f"state={close_state}")
        return {"status": "closed", "close_state": close_state,
                "live_id": live_order["live_id"]}
    except Exception as e:
        err_msg = str(e)
        print(f"[live_trader] close_paper_trade FAILED for {paper_trade_id}: {err_msg}")
        _post_live_alert(
            title=f"⚠ Live force-close FAILED — paper_id={paper_trade_id[:12]}",
            description=(
                f"**Strategy:** {live_order.get('strategy', '?')}\n"
                f"**Reason for close:** {exit_reason}\n"
                f"**Error:** `{err_msg}`\n"
                f"\n_Live position may still be open on Bybit. "
                f"Bybit's backstop SL/TP will catch it eventually._"
            ),
        )
        return {"status": "error", "err": err_msg}


def partial_close_paper_trade(paper_trade_id: str,
                               fraction_of_original: float,
                               exit_price: Optional[float] = None,
                               exit_reason: str = "PARTIAL") -> Dict:
    """Mirror a paper partial close (TP1/TP2) into Bybit.

    Paper closes a fraction of original qty (e.g., 33% at TP1). This
    function closes the SAME fraction on Bybit via reduce-only market
    order, so live qty stays in sync with paper qty.

    Called by paper_trader.py on TP1/TP2 partial-close events.
    """
    if not _is_enabled() or BybitClient is None:
        return {"status": "disabled"}
    if not _env("BYBIT_API_KEY"):
        return {"status": "no_credentials"}

    state = _load_state()
    open_orders = state.get("open_orders", [])
    live_order = next(
        (o for o in open_orders if o.get("paper_trade_id") == paper_trade_id),
        None,
    )
    if live_order is None:
        return {"status": "no_live_record",
                "paper_trade_id": paper_trade_id}

    original_qty = float(live_order.get("qty_original", live_order["qty"]))
    live_order["qty_original"] = original_qty   # ensure stored

    qty_to_close = round(original_qty * fraction_of_original, 3)
    if qty_to_close < 0.001:
        return {"status": "qty_too_small", "qty_to_close": qty_to_close}

    # ── IDEMPOTENCY CHECK (added 2026-05-11 by audit) ────────────────
    # If we've already closed >= this fraction (e.g., duplicate call from
    # both services seeing the same TP1), skip. Prevents over-closing.
    already_closed = float(live_order.get("qty_closed_so_far", 0))
    if already_closed + qty_to_close > original_qty * 1.01:
        # 1% tolerance for rounding
        print(f"[live_trader] partial_close IDEMPOTENT skip: "
              f"already_closed={already_closed} would_close={qty_to_close} "
              f"original={original_qty}")
        return {"status": "already_closed_this_fraction",
                "already_closed": already_closed, "original": original_qty}
    # Check this specific fraction hasn't already been recorded
    # Tolerance 0.02 = 2% — catches 0.33 vs 0.333 vs other minor float drift
    for prev in live_order.get("partial_closes_live", []):
        if abs(prev.get("fraction_of_original", 0) - fraction_of_original) < 0.02 \
           and prev.get("reason") == exit_reason:
            print(f"[live_trader] partial_close IDEMPOTENT skip: "
                  f"fraction {fraction_of_original} reason {exit_reason} "
                  f"already executed at {prev.get('ts')}")
            return {"status": "duplicate_fraction_skip",
                    "previous_ts": prev.get("ts")}

    try:
        client = BybitClient()
        pos = client.get_position(_symbol())
        if pos is None or float(pos.get("size", 0)) < 0.0001:
            # Position already flat — nothing to close
            return {"status": "already_flat"}

        # Cap at remaining Bybit size (safety)
        qty_to_close = min(qty_to_close, float(pos["size"]))
        qty_to_close = round(qty_to_close, 3)
        if qty_to_close < 0.001:
            return {"status": "qty_too_small_after_cap"}

        opposite = "Sell" if live_order["bybit_side"] == "Buy" else "Buy"
        client.place_market_order(
            symbol=_symbol(),
            side=opposite,
            qty=qty_to_close,
            reduce_only=True,
        )

        # Track the partial close on the live record
        live_order["qty_closed_so_far"] = (
            float(live_order.get("qty_closed_so_far", 0)) + qty_to_close
        )
        live_order.setdefault("partial_closes_live", []).append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "fraction_of_original": fraction_of_original,
            "qty_closed": qty_to_close,
            "reason": exit_reason,
            "price": exit_price,
        })
        _save_state(state)

        print(f"[live_trader] PARTIAL_CLOSE {qty_to_close} BTC ({exit_reason}) "
              f"paper_id={paper_trade_id[:8]} closed_so_far="
              f"{live_order['qty_closed_so_far']:.3f}/{original_qty:.3f}")
        return {"status": "partial_closed", "qty": qty_to_close}
    except Exception as e:
        err_msg = str(e)
        print(f"[live_trader] partial_close_paper_trade FAILED: {err_msg}")
        _post_live_alert(
            title=f"⚠ Partial close FAILED — {exit_reason}",
            description=(
                f"**Paper trade id:** {paper_trade_id[:12]}\n"
                f"**Fraction:** {fraction_of_original}\n"
                f"**Error:** `{err_msg}`\n"
                f"\n_Live position is bigger than paper now. Final close "
                f"will reconcile, but P&L diverges in the meantime._"
            ),
        )
        return {"status": "error", "err": err_msg}


def update_live_sl(paper_trade_id: str, new_sl: float,
                    take_profit: Optional[float] = None) -> Dict:
    """Update the SL on Bybit for a paper trade. Called when paper moves
    SL (e.g., BE move at +1R or at TP1 for SMC). Keeps Bybit's UI in sync
    so the user sees the protection that paper logic provides.
    """
    if not _is_enabled() or BybitClient is None:
        return {"status": "disabled"}
    if not _env("BYBIT_API_KEY"):
        return {"status": "no_credentials"}

    state = _load_state()
    live_order = next(
        (o for o in state.get("open_orders", [])
         if o.get("paper_trade_id") == paper_trade_id),
        None,
    )
    if live_order is None:
        return {"status": "no_live_record"}

    try:
        client = BybitClient()
        pos = client.get_position(_symbol())
        if pos is None:
            return {"status": "no_bybit_position"}
        # Keep existing TP if not overridden
        tp = take_profit if take_profit else (
            float(pos["take_profit"]) if pos.get("take_profit") else None
        )
        client.set_position_sl_tp(
            _symbol(),
            stop_loss=round(new_sl, 2),
            take_profit=tp if tp else None,
        )
        live_order.setdefault("sl_updates", []).append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "new_sl": round(new_sl, 2),
        })
        _save_state(state)
        print(f"[live_trader] SL → ${new_sl:.2f} for paper {paper_trade_id[:8]}")
        return {"status": "updated", "new_sl": new_sl}
    except Exception as e:
        err = str(e)
        print(f"[live_trader] update_live_sl FAILED: {err}")
        return {"status": "error", "err": err}


def _force_close_paper_record(paper_trade_id: str, exit_price: float,
                               exit_reason: str) -> Dict:
    """Find and force-close a paper trade record by ID across BOTH paper files.

    Called from reconcile when live position closed but paper hadn't yet.
    Fixes the "ghost open paper trade" bug after Bybit closes via SL/aggregation.

    Returns dict describing what was found and updated.
    """
    import json as _j
    from datetime import datetime as _dt, timezone as _tz
    candidates = [
        ("/home/ubuntu/bot/logs/paper_trades.json", "smc"),
        ("/home/ubuntu/bot/logs/paper_trades_book.json", "book"),
    ]
    for path, source in candidates:
        try:
            with open(path) as f:
                d = _j.load(f)
        except Exception:
            continue
        opens = d.get("open_trades", [])
        for i, t in enumerate(opens):
            if str(t.get("id", "")) == str(paper_trade_id):
                # Compute paper-side P&L using paper's own qty (NOT live qty)
                paper_qty = (float(t.get("units_remaining", 0))
                             or float(t.get("position_size", 0))
                             or float(t.get("position_value", 0)) /
                                 float(t.get("entry", 1) or 1))
                entry = float(t.get("entry", 0))
                side = t.get("side", "buy")
                if side in ("buy", "long"):
                    pnl = paper_qty * (exit_price - entry)
                else:
                    pnl = paper_qty * (entry - exit_price)
                # Update trade record
                t["closed_at"] = _dt.now(_tz.utc).isoformat()
                t["exit_price"] = round(exit_price, 2)
                prior_pnl = float(t.get("realized_pnl", 0))
                t["realized_pnl"] = round(prior_pnl + pnl, 4)
                t["status"] = f"LIVE_FORCED_{exit_reason}"
                # Update bankroll
                d["balance"] = round(float(d.get("balance", 0)) + pnl, 4)
                t["balance_after"] = d["balance"]
                # Move from open to closed
                d["open_trades"] = opens[:i] + opens[i+1:]
                d.setdefault("closed_trades", []).append(t)
                # Atomic save
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    _j.dump(d, f, indent=2, default=str)
                import os as _os
                _os.replace(tmp, path)
                print(f"[live_trader] force-closed paper {paper_trade_id[:12]} "
                      f"in {source} bankroll: pnl=${pnl:+.2f} status=LIVE_FORCED")
                return {"status": "closed", "source": source,
                        "pnl": pnl, "balance_after": d["balance"]}
    return {"status": "no_paper_record", "paper_trade_id": paper_trade_id}


def _lookup_actual_close(client, live_order: Dict,
                         shared_pool: Optional[List[Dict]] = None,
                         consumed: Optional[set] = None) -> Dict:
    """For a live order we believe Bybit just closed, fetch the AUTHORITATIVE
    exit price and realized PnL from Bybit's /v5/position/closed-pnl endpoint.

    Matching priority (best to worst):
      1. Exact qty match (within 0.001 BTC), within 1h of open
      2. Earliest close event chronologically after open with side match,
         qty <= 1.5x ours (could be aggregate)
      3. Ticker fallback

    `consumed` (set of id(pnl_event)) lets caller prevent the same Bybit
    event from being matched to multiple live records in one reconcile pass.
    """
    result = {"exit_price": None, "entry_price": None,
              "closed_pnl_usdt": None, "source": "unknown"}
    sym = _symbol()
    opened_at_iso = live_order.get("opened_at", "")
    qty = float(live_order.get("qty", 0) or 0)
    bybit_side_open = live_order.get("bybit_side", "Buy")
    close_side = "Sell" if bybit_side_open == "Buy" else "Buy"

    open_ms = 0
    try:
        dt = datetime.fromisoformat(opened_at_iso.replace("Z", "+00:00"))
        open_ms = int(dt.timestamp() * 1000)
    except Exception:
        pass

    if shared_pool is not None:
        pool = shared_pool
    else:
        try:
            pool = client.get_closed_pnl(sym, limit=50)
        except Exception as e:
            pool = []
            print(f"[live_trader] closed-pnl fetch failed: {e}")

    if consumed is None:
        consumed = set()

    # Build candidates: same side, after our open, not yet consumed
    candidates = []
    for p in pool:
        if id(p) in consumed:
            continue
        if p.get("side") != close_side:
            continue
        try:
            updated_ms = int(p.get("updatedTime") or p.get("createdTime") or 0)
        except Exception:
            updated_ms = 0
        if updated_ms < open_ms:
            continue
        ev_qty = float(p.get("qty", 0) or 0)
        candidates.append((updated_ms, ev_qty, p))

    if not candidates:
        try:
            tk = client.get_ticker(sym)
            result["exit_price"] = tk
            result["source"] = "ticker_fallback"
        except Exception:
            pass
        return result

    # Tier 1: exact qty match (tolerance 0.001 BTC), prefer earliest
    candidates.sort(key=lambda x: x[0])  # by time
    chosen = None
    for ms, ev_qty, p in candidates:
        if abs(ev_qty - qty) <= 0.0011:
            chosen = (ms, p)
            break
    # Tier 2: aggregate match — earliest event where our qty <= event qty
    if chosen is None:
        for ms, ev_qty, p in candidates:
            if ev_qty >= qty - 0.0001:
                chosen = (ms, p)
                break
    # Tier 3: just take earliest (best effort)
    if chosen is None:
        ms, _, p = candidates[0]
        chosen = (ms, p)

    chosen_ms, chosen_p = chosen
    consumed.add(id(chosen_p))

    avg_exit = float(chosen_p.get("avgExitPrice", 0) or 0)
    avg_entry = float(chosen_p.get("avgEntryPrice", 0) or 0)
    closed_qty = float(chosen_p.get("qty", 0) or 0)
    closed_pnl_total = float(chosen_p.get("closedPnl", 0) or 0)

    result["exit_price"] = avg_exit if avg_exit > 0 else None
    result["entry_price"] = avg_entry if avg_entry > 0 else None
    result["bybit_closed_pnl_event_qty"] = closed_qty
    result["bybit_closed_pnl_event_total"] = closed_pnl_total

    if closed_qty > 0 and qty > 0:
        share = min(qty / closed_qty, 1.0)
        result["closed_pnl_usdt"] = round(closed_pnl_total * share, 4)
    else:
        result["closed_pnl_usdt"] = closed_pnl_total

    result["source"] = "bybit_closed_pnl"
    result["bybit_close_event_ms"] = chosen_ms
    return result


def reconcile_open_positions() -> Dict:
    """Check Bybit for closures of our open live trades. Called every scan.

    Improvements (2026-05-11 audit):
      1. Two-poll confirmation — single API hiccup won't wipe state
      2. Orphan detection — alert if Bybit has position with no record
      3. Reverse sync — force-close paper trade when live closed by Bybit
      4. Authoritative exit prices via /v5/position/closed-pnl (2026-05-13) —
         old code used get_ticker() at reconcile-time which could drift
         seconds-to-minutes after the actual fill, producing wrong P&L.
      5. Running-loss watchdog (2026-05-13) — if aggregate unrealized PnL
         exceeds 1.5× total budget, flatten everything BEFORE the SL
         slips through and turns a $5 budget into a $17 actual loss.
    """
    if not _is_enabled() or BybitClient is None:
        return {"status": "disabled"}
    if not _env("BYBIT_API_KEY"):
        return {"status": "no_credentials"}

    state = _load_state()
    open_orders = state.get("open_orders", [])
    closed_orders = state.get("closed_orders", [])

    try:
        client = BybitClient()
        position = client.get_position(_symbol())
    except BybitError as e:
        err_msg = str(e)
        print(f"[live_trader] reconcile_fail: {err_msg}")
        _post_live_alert(
            title="⚠️ Live position reconcile FAILED",
            description=(f"Bybit query failed.\n**Error:** `{err_msg}`\n"
                         f"**Open live trades:** {len(open_orders)}"),
        )
        return {"status": "error", "err": err_msg}

    # ─── RUNNING-LOSS WATCHDOG ───────────────────────────────────────
    # If aggregate unrealized PnL goes deeper than 1.5× total budget,
    # flatten the entire position NOW (don't wait for SL — it may slip
    # through and add more damage). Each open order's risk_usdt sums
    # to the budget; we cap aggregate loss at 1.5×.
    if (position is not None and open_orders
            and float(position.get("size", 0)) > 0.0001):
        unr = float(position.get("unrealized_pnl", 0) or 0)
        total_budget = sum(float(o.get("risk_usdt", 0) or 0)
                           for o in open_orders)
        watchdog_cap = total_budget * 1.5
        if total_budget > 0 and unr < -watchdog_cap:
            print(f"[live_trader] WATCHDOG TRIGGERED: unrealized "
                  f"${unr:.2f} < -${watchdog_cap:.2f} "
                  f"(budget=${total_budget:.2f}×1.5) — flattening")
            try:
                client.close_position(_symbol())
                _post_live_alert(
                    title="🚨 WATCHDOG flattened — loss > 1.5× budget",
                    description=(
                        f"**Unrealized P&L:** ${unr:.2f}\n"
                        f"**Budget:** ${total_budget:.2f} × 1.5 = "
                        f"${watchdog_cap:.2f}\n"
                        f"**Position:** {position.get('size')} BTC @ "
                        f"${position.get('entry',0):,.2f}\n"
                        f"\nClosed all {len(open_orders)} live trades to "
                        f"prevent further damage. Reconcile will mark "
                        f"records BYBIT_CLOSED next pass."
                    ),
                    color=0xE74C3C,
                )
                # Re-poll position — should be None now
                import time as _wt
                _wt.sleep(1.2)
                try:
                    position = client.get_position(_symbol())
                except Exception:
                    position = None
            except Exception as _we:
                print(f"[live_trader] WATCHDOG close FAILED: {_we}")
                _post_live_alert(
                    title="⚠ WATCHDOG close FAILED",
                    description=(
                        f"Tried to flatten on loss=${unr:.2f} but: `{_we}`.\n"
                        f"Position still open — Bybit SL will still backstop, "
                        f"but actual loss may exceed ${watchdog_cap:.2f}."
                    ),
                    color=0xE74C3C,
                )

    # ─── ORPHAN DETECTION ────────────────────────────────────────────
    # Bybit has a position but we have NO record for it. This could happen
    # if order placement succeeded but state save failed (rare bug).
    if position is not None and not open_orders:
        msg = (f"Bybit shows {position['side']} {position['size']} BTC "
               f"@ ${position['entry']:,.2f} but we have NO live records.")
        print(f"[live_trader] ORPHAN POSITION: {msg}")
        _post_live_alert(
            title="🚨 ORPHAN POSITION DETECTED",
            description=(
                f"{msg}\n\n"
                f"_Either order placement succeeded but state write failed, "
                f"or the position was opened outside the bot. Investigate immediately._"
            ),
        )
        return {"status": "orphan", "position": position}

    if not open_orders:
        return {"status": "no_open_orders"}

    # ─── TWO-POLL CONFIRMATION ───────────────────────────────────────
    # If position seems empty (or much smaller), poll once more after 3s
    # to confirm before marking records closed. Defends against single
    # API hiccup wiping all state.
    needs_confirm = position is None or position["size"] < 0.0001
    if needs_confirm:
        import time as _t
        _t.sleep(3)
        try:
            position2 = client.get_position(_symbol())
        except BybitError:
            position2 = position   # if 2nd poll also fails, treat as same
        if position2 is not None and float(position2.get("size", 0)) > 0.0001:
            # First poll was wrong — position actually exists
            print("[live_trader] reconcile: first poll showed empty, "
                  "second poll showed position — using second")
            position = position2

    closed_now = []
    still_open = []
    # Fetch closed-PnL pool ONCE — shared across all orders being closed
    # (Bybit reports per-position-close, so this is the same data for everyone).
    shared_pnl_pool = []
    consumed_events = set()  # track id(event) so 2 orders don't share 1 event
    if position is None or float(position.get("size", 0)) < 0.001:
        try:
            shared_pnl_pool = client.get_closed_pnl(_symbol(), limit=50)
        except Exception as e:
            print(f"[live_trader] closed-pnl prefetch failed: {e}")
    for o in open_orders:
        # Threshold: position fully gone OR shrunk well past this record's
        # contribution (50% threshold for partial-close detection)
        original_qty = float(o.get("qty_original", o["qty"]))
        already_closed = float(o.get("qty_closed_so_far", 0))
        remaining_in_record = original_qty - already_closed
        # Trade is "closed" if Bybit position is None OR effectively zero
        if position is None or float(position.get("size", 0)) < 0.001:
            o["closed_at"] = datetime.now(timezone.utc).isoformat()
            o["status"] = "BYBIT_CLOSED"
            # AUTHORITATIVE LOOKUP — use Bybit's own closed-pnl record
            # (avgExitPrice + closedPnl in USDT) instead of ticker at
            # reconcile-time. Falls back to ticker if lookup fails.
            actual = _lookup_actual_close(client, o,
                                          shared_pool=shared_pnl_pool,
                                          consumed=consumed_events)
            last_price = actual.get("exit_price")
            o["exit_price"] = last_price       # canonical field
            o["exit_price_approx"] = last_price  # legacy alias for compat
            o["exit_source"] = actual.get("source")
            if actual.get("entry_price"):
                # Replace signal-price with ACTUAL Bybit fill price for entry
                o["entry_price_actual"] = actual["entry_price"]
            if actual.get("closed_pnl_usdt") is not None:
                o["realized_pnl_usdt"] = actual["closed_pnl_usdt"]
                # Recompute realized R = pnl / risk
                risk = float(o.get("risk_usdt", 0) or 0)
                if risk > 0:
                    o["realized_R"] = round(
                        actual["closed_pnl_usdt"] / risk, 3)
            if not last_price:
                # Fall back: use entry as exit so paper closes at ~breakeven
                # (better than leaving paper ghost-open silently)
                fallback = float(o.get("entry_signal", 0) or 0)
                if fallback > 0:
                    last_price = fallback
                    o["exit_price_fallback"] = fallback
                    _post_live_alert(
                        title="⚠ Exit-price lookup failed during reconcile",
                        description=(
                            f"Live trade closed but couldn't get exit price "
                            f"from Bybit closed-pnl OR ticker.\n"
                            f"Used entry ${fallback:,.0f} as fallback for paper "
                            f"close. P&L will be 0 in records — verify via "
                            f"Bybit web UI."
                        ),
                        color=0xFFB300,
                    )
            closed_orders.append(o)
            closed_now.append(o)
            # REVERSE SYNC: force-close the paper trade too (always, even
            # with fallback price — silent ghost is worse than imprecise P&L)
            paper_id = o.get("paper_trade_id")
            if paper_id and last_price:
                try:
                    fc_result = _force_close_paper_record(
                        paper_id, last_price, "BYBIT_RECONCILE")
                    print(f"[live_trader] reverse-sync: {fc_result}")
                except Exception as e:
                    print(f"[live_trader] force_close_paper FAIL: {e}")
                    _post_live_alert(
                        title=f"⚠ Reverse-sync FAILED for paper {paper_id[:8]}",
                        description=f"`{e}` — paper trade may be stale-open",
                    )
            # Post live close alert with REAL P&L
            try:
                pnl_str = (f"${o.get('realized_pnl_usdt', 0):+.2f} "
                           f"({o.get('realized_R', 0):+.2f}R)") \
                    if o.get("realized_pnl_usdt") is not None else "(P&L unknown)"
                _post_live_alert(
                    title=f"🔴 Live closed: {o.get('strategy', '?')}",
                    description=(
                        f"**P&L:** {pnl_str}\n"
                        f"**Entry:** ${o.get('entry_signal', 0):,.2f}  "
                        f"**Exit:** ${last_price:,.2f}\n"
                        f"**Qty:** {o.get('qty')} BTC  "
                        f"**Source:** {o.get('exit_source', '?')}"
                    ),
                    color=(0x2ECC71 if (o.get("realized_pnl_usdt") or 0) > 0
                           else 0xE74C3C),
                )
            except Exception:
                pass
        else:
            still_open.append(o)

    state["open_orders"] = still_open
    state["closed_orders"] = closed_orders
    _save_state(state)

    return {"status": "ok", "closed_count": len(closed_now),
            "still_open": len(still_open)}


def status() -> Dict:
    """Quick health-check: is live trading on, what's open, any errors?"""
    out = {
        "enabled": _is_enabled(),
        "strategies": _live_strategies(),
        "risk_usdt": _risk_usdt(),
        "max_positions": _max_positions(),
        "symbol": _symbol(),
        "testnet": _env("BYBIT_TESTNET", "false").lower() in ("1", "true", "yes"),
    }
    state = _load_state()
    out["open_count"] = len(state.get("open_orders", []))
    out["closed_count"] = len(state.get("closed_orders", []))
    if out["enabled"] and BybitClient is not None and _env("BYBIT_API_KEY"):
        try:
            c = BybitClient()
            out["balance_usdt"] = c.get_balance("USDT")
            pos = c.get_position(_symbol())
            out["bybit_position"] = pos
        except BybitError as e:
            out["bybit_error"] = str(e)
    return out


if __name__ == "__main__":
    print(json.dumps(status(), indent=2, default=str))
