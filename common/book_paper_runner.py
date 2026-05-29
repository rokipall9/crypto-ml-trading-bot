"""
book_paper_runner.py — Live paper trader for the SMC book strategies
(S2 MSS, S3 OTE, S5 FTR), running on a SEPARATE bankroll from the
existing SMC_CONFLUENCE bot so they cannot taint each other.

Runs every minute via systemd. Each scan:
  1. Pull live Bybit BTCUSDT 1h data (1500 bars)
  2. Run S2/S3/S5 detection
  3. For any FRESH signal (entry within last 3 closed bars, not seen yet):
       • Open a paper trade (sized 1% risk of book bankroll)
       • Post OPEN embed to per-strategy channel (OTE/MSS/FTR)
  4. For all open trades: check current price against SL/TP
       • Close on SL/TP hit OR after 60h time-stop
       • Post CLOSE embed to shared closed-trades channel
       • Update bankroll

State:  /home/ubuntu/bot/logs/paper_trades_book.json
Ledger: /home/ubuntu/common/forward_results_book.jsonl
"""
from __future__ import annotations

import os
import sys
import json
import time
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

sys.path.insert(0, "/home/ubuntu/common")
sys.path.insert(0, "/home/ubuntu/bot")

import pandas as pd

STATE_FILE = "/home/ubuntu/bot/logs/paper_trades_book.json"
LEDGER_FILE = "/home/ubuntu/common/forward_results_book.jsonl"

STARTING_BALANCE = 10_000.0
RISK_PCT = 0.01           # 1% per trade (book's spec)

# Timeframes to scan. Each: (tf_label, n_bars_to_fetch, time_stop_hours).
# Time stop matches simulate()'s 60-bar window per TF (60×1h=60h, 60×4h=240h).
# 4H added 2026-05-06 after backtest verification: all 4 strategies ROBUST,
# 4/4 quarters profitable for S2/S3/S5, 3/4 for S6, OOS PF ratios 0.90-2.03.
TIMEFRAMES = [
    ("1h", 1500, 60),
    ("4h",  800, 240),
]

WEBHOOK_OPEN = {
    "s2_mss":  os.environ.get("DISCORD_WEBHOOK_URL_MSS", "").strip(),
    "s3_ote":  os.environ.get("DISCORD_WEBHOOK_URL_OTE", "").strip(),
    "s5_ftr":  os.environ.get("DISCORD_WEBHOOK_URL_FTR", "").strip(),
    "s6_atr":  os.environ.get("DISCORD_WEBHOOK_URL_S6",  "").strip(),
    "s7_sma":  os.environ.get("DISCORD_WEBHOOK_URL_S7",  "").strip(),
    "s8_bisi": os.environ.get("DISCORD_WEBHOOK_URL_S8",  "").strip(),
}

# Per-strategy timeframe whitelist. Defaults to TIMEFRAMES for any
# strategy not listed here. Use this to restrict a strategy to a subset
# of TFs (e.g. S8 was verified ROBUST only on 4H — 1H failed top-5
# dependency check at 89%, 1D failed at 147%).
STRATEGY_TFS = {
    "s8_bisi": ("4h",),
}
WEBHOOK_CLOSED = os.environ.get("DISCORD_WEBHOOK_URL_BOOK_CLOSED", "").strip()
TV_CHART_URL = os.environ.get(
    "TRADINGVIEW_CHART_URL",
    "https://www.tradingview.com/chart/pViMM9Zt/?symbol=BYBIT%3ABTCUSDT.P",
).strip()

PRETTY_NAME = {
    "s2_mss":  ("MSS+IMB",       0x9B6BFF),  # purple
    "s3_ote":  ("OTE 0.7-0.8",   0x00D27E),  # green (the king)
    "s5_ftr":  ("FTR fast move", 0xFFCF3D),  # gold
    "s6_atr":  ("ATR Pullback",  0xFF6B6B),  # coral red
    "s7_sma":  ("SMA Pullback",  0x4FC3F7),  # sky blue
    "s8_bisi": ("BISI/CE Killzone", 0xE91E63),  # pink (ICT method)
}


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "balance": STARTING_BALANCE,
            "starting_balance": STARTING_BALANCE,
            "open_trades": [],
            "closed_trades": [],
            "seen_keys": [],
        }
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {
            "balance": STARTING_BALANCE,
            "starting_balance": STARTING_BALANCE,
            "open_trades": [],
            "closed_trades": [],
            "seen_keys": [],
        }


def _save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def _fetch_bybit_klines(interval: str, n: int = 1500) -> pd.DataFrame:
    iv_map = {"5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}
    iv = iv_map.get(interval, "60")
    rows = []
    end_ts = int(time.time() * 1000)
    while len(rows) < n:
        cnt = min(1000, n - len(rows))
        url = (f"https://api.bybit.com/v5/market/kline?"
               f"category=linear&symbol=BTCUSDT&interval={iv}"
               f"&limit={cnt}&end={end_ts}")
        req = urllib.request.Request(url, headers={"User-Agent": "book-runner/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        batch = data.get("result", {}).get("list", [])
        if not batch:
            break
        rows = batch + rows
        end_ts = int(batch[-1][0]) - 1
    rows.sort(key=lambda r: int(r[0]))
    out = []
    for r in rows:
        out.append({
            "ts": int(r[0]),
            "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]),
            "volume": float(r[5]),
        })
    df = pd.DataFrame(out).drop_duplicates(subset=["ts"]).reset_index(drop=True)
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df[["open", "high", "low", "close", "volume"]]


def _macro_regime(df_1d: pd.DataFrame) -> str:
    """Classify BTC daily trend → 'BULL' / 'BEAR' / 'CHOP'.

    Added 2026-05-06 — operator's rule:
      "if market is buy, the buy will win and the sell will lose"
    so we only fire the side aligned with the daily macro trend.

      BULL: EMA21 > EMA55 AND EMA21 slope > +0.5% over last 5 daily bars
      BEAR: EMA21 < EMA55 AND EMA21 slope < -0.5% over last 5 daily bars
      CHOP: anything in between → allow both directions (conservative default)

    Lags real reversals by 1-2 days (cost of using daily candles), but
    avoids 90% of micro-counter-trend false signals.
    """
    if df_1d is None or len(df_1d) < 60:
        return "CHOP"
    try:
        closes = df_1d["close"]
        ema21 = closes.ewm(span=21, adjust=False).mean()
        ema55 = closes.ewm(span=55, adjust=False).mean()
        e21 = float(ema21.iloc[-1])
        e55 = float(ema55.iloc[-1])
        e21_5d = float(ema21.iloc[-6])
        slope = (e21 - e21_5d) / e21_5d * 100 if e21_5d > 0 else 0
        if e21 > e55 and slope > 0.5:
            return "BULL"
        if e21 < e55 and slope < -0.5:
            return "BEAR"
        return "CHOP"
    except Exception:
        return "CHOP"


def _fetch_live_price() -> Optional[float]:
    try:
        url = ("https://api.bybit.com/v5/market/tickers?"
               "category=linear&symbol=BTCUSDT")
        req = urllib.request.Request(url, headers={"User-Agent": "book-runner/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return float(data["result"]["list"][0]["lastPrice"])
    except Exception:
        return None


def _post_webhook(url: str, payload: dict) -> bool:
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "book-runner/1.0"})
        urllib.request.urlopen(req, timeout=8).read()
        return True
    except Exception as e:
        print("[book_runner] post_fail:", e)
        return False


def _notify_tag():
    """Return (content, allowed_mentions) to tag the operator on paper opens.
    Reads NOTIFY_USER_ID from env. If unset or non-numeric, returns ("", {}).
    """
    uid = (os.environ.get("NOTIFY_USER_ID") or "").strip()
    if uid.isdigit():
        return ("<@%s>" % uid,
                {"users": [uid], "parse": []})
    return ("", {})


def _post_open(strategy: str, trade: dict) -> bool:
    base = _base_strategy(strategy)
    tf = _tf_label(strategy)
    pretty, color = PRETTY_NAME.get(base, (base, 0x5B8DEF))
    title_tf = f" [{tf}]" if tf else ""
    side_word = "BUY" if trade["side"] == "long" else "SELL"
    arrow = "📈" if trade["side"] == "long" else "📉"
    time_stop_h = trade.get("time_stop_hours", 60)
    embed = {
        "title": f"{arrow} {pretty}{title_tf} · {side_word} SIGNAL",
        "url": TV_CHART_URL,
        "color": color,
        "fields": [
            {"name": "Entry",  "value": f"${trade['entry']:,.2f}", "inline": True},
            {"name": "SL",     "value": f"${trade['stop']:,.2f}",  "inline": True},
            {"name": "Target", "value": f"${trade['target']:,.2f}", "inline": True},
            {"name": "R:R",    "value": f"{trade['target_R']:.2f}R", "inline": True},
            {"name": "Risk",   "value": f"1% (${trade['risk_amount']:.2f})", "inline": True},
            {"name": "Time stop", "value": f"{time_stop_h}h", "inline": True},
            {"name": "Chart",  "value": f"📊 [Open chart]({TV_CHART_URL})", "inline": False},
        ],
        "footer": {"text": f"Book strategies · separate bankroll · paper-traded"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = {"username": "📚 Book Bot", "embeds": [embed]}
    tag, am = _notify_tag()
    if tag:
        payload["content"] = tag
        payload["allowed_mentions"] = am
    return _post_webhook(WEBHOOK_OPEN.get(base, ""), payload)


def _post_close(strategy: str, trade: dict, exit_price: float, R: float,
                pnl: float, status: str, balance: float) -> bool:
    base = _base_strategy(strategy)
    tf = _tf_label(strategy)
    pretty, _ = PRETTY_NAME.get(base, (base, 0x5B8DEF))
    title_tf = f" [{tf}]" if tf else ""
    side_word = "BUY" if trade["side"] == "long" else "SELL"
    if pnl > 0:
        emoji, color = "PROFIT", 0x00C853
    elif pnl < 0:
        emoji, color = "LOSS", 0xF44336
    else:
        emoji, color = "FLAT", 0x6B7280
    embed = {
        "title": f"{emoji} · {pretty}{title_tf} {side_word} ({status})",
        "url": TV_CHART_URL,
        "color": color,
        "fields": [
            {"name": "Entry / Exit",
             "value": f"${trade['entry']:,.2f} → ${exit_price:,.2f}", "inline": True},
            {"name": "P&L",
             "value": f"${pnl:+,.2f} ({R:+.2f}R)", "inline": True},
            {"name": "Strategy", "value": f"{pretty}{title_tf}", "inline": True},
            {"name": "Balance after", "value": f"${balance:,.2f}", "inline": True},
            {"name": "Chart",  "value": f"📊 [Open chart]({TV_CHART_URL})", "inline": False},
        ],
        "footer": {"text": "Book strategies · separate bankroll"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return _post_webhook(WEBHOOK_CLOSED, {"username": "📚 Book Bot",
                                          "embeds": [embed]})


def _append_ledger(line: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)
        with open(LEDGER_FILE, "a") as f:
            f.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass


def _trade_key(strategy: str, side: str, entry_ts_ms: int,
               entry: float, stop: float = 0.0, target: float = 0.0) -> str:
    # `strategy` already includes _1h / _4h suffix so different TFs dedup separately.
    #
    # 2026-05-06 fix: dedupe by SETUP (entry+stop+target) instead of bar
    # timestamp. Reason: when a 1H bar closes between scans, the strategy
    # re-detects the same fib setup on the new bar (price re-enters the
    # zone) and the OLD ts-based key didn't match → re-fired the same
    # trade. Now same SL/TP/entry = same setup = same key.
    return (f"{strategy}|{side}|{round(entry, 0)}"
            f"|{round(stop, 0)}|{round(target, 0)}")


def _base_strategy(name: str) -> str:
    """Strip _1h / _4h suffix to get base strategy name (for webhook/pretty lookup)."""
    for suffix in ("_1h", "_4h"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _tf_label(name: str) -> str:
    """Extract TF label from strategy name (returns '1H' / '4H' / '')."""
    for suffix in ("_1h", "_4h"):
        if name.endswith(suffix):
            return suffix[1:].upper()
    return ""


def scan_once() -> Dict:
    try:
        import smc_book_strategies as smc
        import smc_book_s6 as s6_mod
        import smc_book_s7 as s7_mod
        import smc_book_s8 as s8_mod
    except Exception as e:
        return {"status": "import_fail", "err": str(e)}

    # Live trader is OPTIONAL — if not importable or disabled, paper continues
    # exactly as before. Live trading runs as a parallel layer alongside paper.
    try:
        import live_trader
    except Exception:
        live_trader = None

    # Fetch ALL configured timeframes upfront. Each (tf, n_bars, time_stop_h)
    # gets its own df. dfs[tf] is referenced both during fresh-signal detection
    # AND during SL/TP scanning of open trades.
    dfs: Dict[str, pd.DataFrame] = {}
    tf_meta: Dict[str, Dict] = {}
    for tf_label, n_bars, time_stop_h in TIMEFRAMES:
        df_tf = _fetch_bybit_klines(tf_label, n_bars)
        if df_tf is None or len(df_tf) < 200:
            print(f"[book/{tf_label}] insufficient data, skipping this TF")
            continue
        dfs[tf_label] = df_tf
        tf_meta[tf_label] = {"time_stop_h": time_stop_h}

    if not dfs:
        return {"status": "no_data"}

    # ─── Macro regime gate ──────────────────────────────────────────
    # Pull daily once (small) to classify BULL/BEAR/CHOP. This gates the
    # *direction* each strategy is allowed to fire, NOT whether they scan.
    # Strategies still detect both sides; we just block the wrong-side
    # signals before they post or open a paper trade.
    try:
        df_daily = _fetch_bybit_klines("1d", 200)
        regime = _macro_regime(df_daily)
    except Exception as e:
        print(f"[book/regime] fetch failed: {e} — defaulting to CHOP")
        regime = "CHOP"
    print(f"[book/regime] macro = {regime}")

    cur_price = _fetch_live_price()
    if cur_price is None:
        # Fallback: most recent close from the fastest TF available
        first_tf = next(iter(dfs))
        cur_price = float(dfs[first_tf]["close"].iloc[-1])
    now = datetime.now(timezone.utc)

    state = _load_state()
    seen = set(state.get("seen_keys", []))
    open_trades = state.get("open_trades", [])
    closed_trades = state.get("closed_trades", [])
    balance = float(state.get("balance", STARTING_BALANCE))

    summary = {"opened": 0, "closed": 0, "open_now": len(open_trades),
               "balance_before": balance, "tfs_scanned": list(dfs.keys())}

    # ─── Step 1: detect FRESH signals on EACH timeframe ──────────────
    fn_map = {"s2_mss": smc.s2_mss, "s3_ote": smc.s3_ote, "s5_ftr": smc.s5_ftr,
              "s6_atr": s6_mod.s6_atr, "s7_sma": s7_mod.s7_sma,
              "s8_bisi": s8_mod.s8_bisi}
    for tf_label, df in dfs.items():
        time_stop_h = tf_meta[tf_label]["time_stop_h"]
        for base_name, fn in fn_map.items():
            # Skip if this strategy isn't enabled on this TF (S8 is 4H-only).
            allowed = STRATEGY_TFS.get(base_name, tuple(t[0] for t in TIMEFRAMES))
            if tf_label not in allowed:
                continue
            tf_strat_name = f"{base_name}_{tf_label}"   # e.g. "s3_ote_4h"
            try:
                trades = fn(df)
            except Exception as e:
                print(f"[book/{tf_strat_name}] error: {e}")
                continue
            # We want signals where the entry bar is among the most recent ~3
            # bars AND haven't been seen before
            recent_threshold = len(df) - 4
            for t in trades:
                if t.entry_idx < recent_threshold:
                    continue
                entry_ts_ms = int(df.index[t.entry_idx].timestamp() * 1000)
                key = _trade_key(tf_strat_name, t.side, entry_ts_ms,
                                 t.entry, t.stop, t.target)
                if key in seen:
                    continue
                seen.add(key)

                # ── REGIME GATE: skip counter-trend signals ───────────
                # In BULL: only longs fire. In BEAR: only shorts fire.
                # CHOP: both fire (conservative default).
                if regime == "BULL" and t.side == "short":
                    print(f"[book/{tf_strat_name}] BLOCKED short "
                          f"(regime=BULL — counter-trend, skip)")
                    continue
                if regime == "BEAR" and t.side == "long":
                    print(f"[book/{tf_strat_name}] BLOCKED long "
                          f"(regime=BEAR — counter-trend, skip)")
                    continue

                risk = abs(t.entry - t.stop)
                if risk <= 0:
                    continue
                # Sanity: don't open if live price has already moved past SL or TP
                if t.side == "long":
                    if cur_price <= t.stop or cur_price >= t.target:
                        print(f"[book/{tf_strat_name}] STALE long signal: "
                              f"entry ${t.entry:.0f} SL ${t.stop:.0f} TP ${t.target:.0f} "
                              f"but live=${cur_price:.0f} — skip")
                        continue
                else:
                    if cur_price >= t.stop or cur_price <= t.target:
                        print(f"[book/{tf_strat_name}] STALE short signal: "
                              f"entry ${t.entry:.0f} SL ${t.stop:.0f} TP ${t.target:.0f} "
                              f"but live=${cur_price:.0f} — skip")
                        continue
                risk_amount = balance * RISK_PCT
                position_size = risk_amount / risk
                target_R = abs(t.target - t.entry) / risk
                trade_record = {
                    "id": "book_" + uuid.uuid4().hex[:8],
                    "strategy": tf_strat_name,        # tagged with TF
                    "tf": tf_label,                    # for SL/TP scan routing
                    "side": t.side,
                    "entry": float(t.entry),
                    "stop": float(t.stop),
                    "target": float(t.target),
                    "target_R": round(target_R, 2),
                    "risk_amount": round(risk_amount, 2),
                    "position_size": round(position_size, 8),
                    "opened_at": now.isoformat(),
                    "expires_at": (now + timedelta(hours=time_stop_h)).isoformat(),
                    "time_stop_hours": time_stop_h,
                    "entry_ts_ms": entry_ts_ms,
                    "key": key,
                    "status": "OPEN",
                }
                # ── ML FILTER (Phase 0 heuristic) ─────────────────────
                # Scores the signal + posts review to ML channel +
                # tags the trade record. Shadow mode: trade still opens.
                # Gate mode: only HIGH tier passes; rejected trades log+skip.
                try:
                    import ml_filter as _mf
                    import ml_alerter as _ma
                    ml_sig = {
                        "strategy": tf_strat_name,
                        "side":     trade_record["side"],
                        "entry":    trade_record["entry"],
                        "sl":       trade_record["stop"],
                        "tp":       trade_record["target"],
                    }
                    ml_score = _mf.score_signal(ml_sig)
                    trade_record["ml_score"] = ml_score.get("score")
                    trade_record["ml_tier"] = ml_score.get("tier")
                    trade_record["ml_model"] = ml_score.get("model")
                    trade_record["ml_mode"] = ml_score.get("mode")
                    try:
                        _ma.post_signal_review(ml_score, ml_sig)
                    except Exception as _pe:
                        print(f"[book/{tf_strat_name}] ml alert failed: {_pe}")
                    if not ml_score.get("allow", True):
                        print(f"[book/{tf_strat_name}] ML REJECTED "
                              f"tier={ml_score.get('tier')} "
                              f"score={ml_score.get('score'):.3f} — skipping")
                        continue
                except Exception as _me:
                    print(f"[book/{tf_strat_name}] ML filter failed "
                          f"(book continues): {_me}")

                open_trades.append(trade_record)
                summary["opened"] += 1
                ok = _post_open(tf_strat_name, trade_record)
                print(f"[book/{tf_strat_name}] OPENED {t.side} @ ${t.entry:.2f} "
                      f"SL ${t.stop:.2f} target ${t.target:.2f} (post={ok})")

                # ── LIVE shadow order (only if strategy whitelisted) ────────
                if live_trader is not None and live_trader.is_live_for(tf_strat_name):
                    live_result = live_trader.open_live_trade(
                        strategy=tf_strat_name, side=t.side,
                        entry=float(t.entry), stop=float(t.stop),
                        target=float(t.target),
                        paper_trade_id=trade_record["id"])
                    print(f"[book/{tf_strat_name}] LIVE: {live_result}")
                    trade_record["live_result"] = live_result
                _append_ledger({
                    "ts": now.isoformat(),
                    "event": "open",
                    "strategy": tf_strat_name,
                    "tf": tf_label,
                    **{k: trade_record[k] for k in ("id", "side", "entry",
                                                    "stop", "target", "target_R",
                                                    "risk_amount", "position_size")},
                })

    # ─── Step 2: check open trades for SL/TP/time-stop ────────────
    still_open = []
    for tr in open_trades:
        side = tr["side"]
        entry = float(tr["entry"])
        stop = float(tr["stop"])
        target = float(tr["target"])
        position_size = float(tr["position_size"])
        risk = abs(entry - stop)

        # Route SL/TP scan to the right TF's df. Default to "1h" for legacy
        # trades that pre-date the multi-TF tagging.
        tr_tf = tr.get("tf", "1h")
        df = dfs.get(tr_tf)
        if df is None:
            # TF not available this scan (e.g. fetch failed) — skip SL/TP scan
            # and let the live-price check handle the trade for now.
            df = next(iter(dfs.values()))

        # Check time-stop (default 60h for legacy trades without time_stop_hours)
        legacy_time_stop = tr.get("time_stop_hours", 60)
        try:
            expires = datetime.fromisoformat(str(tr["expires_at"]).replace("Z", "+00:00"))
        except Exception:
            expires = now + timedelta(hours=legacy_time_stop)

        # SL/TP check — ONLY on bars AFTER the entry bar (matches simulate()
        # which walks from entry_idx+1). This is the fix for the same-bar
        # immediate-stop bug.
        opened_at_ts = pd.to_datetime(tr["opened_at"], utc=True)
        post_entry = df[df.index > opened_at_ts]

        exit_price = None
        status = None
        if not post_entry.empty:
            after_high = float(post_entry["high"].max())
            after_low = float(post_entry["low"].min())
            if side == "long":
                if after_low <= stop:
                    exit_price = stop; status = "SL"
                elif after_high >= target:
                    exit_price = target; status = "TP"
            else:
                if after_high >= stop:
                    exit_price = stop; status = "SL"
                elif after_low <= target:
                    exit_price = target; status = "TP"

        # Live-price check — use ONLY if no closed bar has triggered SL/TP yet.
        # This catches intra-bar moves on the currently-forming bar.
        if exit_price is None:
            if side == "long":
                if cur_price <= stop:
                    exit_price = stop; status = "SL"
                elif cur_price >= target:
                    exit_price = target; status = "TP"
            else:
                if cur_price >= stop:
                    exit_price = stop; status = "SL"
                elif cur_price <= target:
                    exit_price = target; status = "TP"
        if exit_price is None and now >= expires:
            exit_price = cur_price
            status = "TIME"

        if exit_price is None:
            still_open.append(tr)
            continue

        # Realize the trade
        if side == "long":
            pnl = (exit_price - entry) * position_size
            R_realized = (exit_price - entry) / risk if risk > 0 else 0
        else:
            pnl = (entry - exit_price) * position_size
            R_realized = (entry - exit_price) / risk if risk > 0 else 0
        balance += pnl
        tr["closed_at"] = now.isoformat()
        tr["exit_price"] = round(exit_price, 2)
        tr["realized_pnl"] = round(pnl, 4)
        tr["realized_R"] = round(R_realized, 3)
        tr["status"] = status
        closed_trades.append(tr)
        summary["closed"] += 1

        # ── ML OUTCOME RECORDING ─────────────────────────────────────
        # Feed close back to the ML filter (training data) and post the
        # outcome card to the ML channel if this trade was scored.
        try:
            import ml_filter as _mf
            import ml_alerter as _ma
            outcome = {
                "strategy":      tr.get("strategy"),
                "side":          tr.get("side"),
                "exit_event":    status,
                "exit_price":    exit_price,
                "entry_price":   tr.get("entry"),
                "realized_pnl":  pnl,
                "realized_R":    R_realized,
                "ml_score":      tr.get("ml_score"),
                "ml_tier":       tr.get("ml_tier"),
                "opened_at":     tr.get("opened_at"),
            }
            _mf.record_outcome(str(tr.get("id", "")), outcome)
            if tr.get("ml_score") is not None:
                score_stub = {
                    "tier":     tr.get("ml_tier"),
                    "score":    tr.get("ml_score"),
                    "features": {"strategy": tr.get("strategy")},
                }
                outcome_stub = {
                    "realized_R":         R_realized,
                    "realized_pnl_usdt":  pnl,
                    "exit_price":         exit_price,
                }
                _ma.post_outcome(score_stub, outcome_stub)
        except Exception as _ml_e:
            print(f"[book/{tr['strategy']}] ml outcome fail: {_ml_e}")

        # ── LIVE: force-close the corresponding Bybit position ──────
        # Paper drives live. Whatever paper decides (SL/TP/TIME), bot
        # actively closes that trade's qty on Bybit so paper and live
        # outcomes align.
        if live_trader is not None:
            try:
                live_trader.close_paper_trade(
                    paper_trade_id=tr.get("id", ""),
                    exit_price=exit_price,
                    exit_reason=status,
                )
            except Exception as _e:
                print(f"[book/{tr['strategy']}] live force-close error: {_e}")

        ok = _post_close(tr["strategy"], tr, exit_price, R_realized, pnl,
                         status, balance)
        print(f"[book/{tr['strategy']}] CLOSED {status} @ ${exit_price:.2f} "
              f"pnl ${pnl:+.2f} ({R_realized:+.2f}R)  bal=${balance:.2f} "
              f"(post={ok})")
        _append_ledger({
            "ts": now.isoformat(),
            "event": "close",
            "strategy": tr["strategy"],
            "side": side,
            "id": tr["id"],
            "entry": entry,
            "exit": exit_price,
            "pnl": pnl,
            "r": R_realized,
            "status": status,
        })

    state["open_trades"] = still_open
    state["closed_trades"] = closed_trades
    state["balance"] = round(balance, 4)
    state["seen_keys"] = sorted(seen)
    state["last_scan"] = now.isoformat()
    _save_state(state)

    # ── LIVE: reconcile any Bybit-side closures (no-op if disabled) ──
    if live_trader is not None:
        try:
            live_recon = live_trader.reconcile_open_positions()
            if live_recon.get("status") == "ok" and live_recon.get("closed_count", 0) > 0:
                print(f"[book/live] reconciled {live_recon['closed_count']} closed live trades")
                summary["live_closed"] = live_recon["closed_count"]
        except Exception as e:
            print(f"[book/live] reconcile error: {e}")

    summary["balance_after"] = state["balance"]
    summary["open_now"] = len(still_open)
    summary["status"] = "ok"
    return summary


if __name__ == "__main__":
    print(json.dumps(scan_once(), indent=2, default=str))
