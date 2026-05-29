"""
shadow_book.py — Run S2/S3/S5 from the SMC book in SHADOW MODE.

Each scan: pull live Bybit BTC 1h data, run all three strategies, log
any newly-detected signals (de-duped by entry timestamp) to a JSONL.
The simulate() function inside the strategies resolves outcome (TP, SL,
or time stop) using actual price action — so by the time a signal is
60 bars old, its outcome is already recorded.

NO Discord alerts. NO paper trades. NO risk budget consumed. Pure
observation to validate the book's reported edge against live Bybit
data feeding the bot.

Output: /home/ubuntu/common/shadow_book_signals.jsonl
State:  /home/ubuntu/common/shadow_book_state.json
"""
from __future__ import annotations

import os
import sys
import json
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List

sys.path.insert(0, "/home/ubuntu/common")
sys.path.insert(0, "/home/ubuntu/bot")

import pandas as pd

LOG_FILE = "/home/ubuntu/common/shadow_book_signals.jsonl"
STATE_FILE = "/home/ubuntu/common/shadow_book_state.json"

# Strategies to shadow-test (S2, S3, S5 — the ones whose reported numbers
# held up on independent Bybit data per the verification backtest)
ENABLED = ("s2_mss", "s3_ote", "s5_ftr")


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"seen_keys": []}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"seen_keys": []}


def _save_state(s: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, default=str)
    os.replace(tmp, STATE_FILE)


def _fetch_bybit_btc_1h(n_bars: int = 1500) -> pd.DataFrame:
    """Same Bybit fetcher as zone_alerter — 1h candles, paginated."""
    rows = []
    end_ts = int(time.time() * 1000)
    while len(rows) < n_bars:
        n = min(1000, n_bars - len(rows))
        url = (f"https://api.bybit.com/v5/market/kline?"
               f"category=linear&symbol=BTCUSDT&interval=60"
               f"&limit={n}&end={end_ts}")
        req = urllib.request.Request(url, headers={"User-Agent": "shadow-book/1.0"})
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


def _trade_key(strategy_name: str, t, df) -> str:
    """Stable de-dupe key for a Trade: strategy + entry timestamp + side."""
    ts_ms = int(df.index[t.entry_idx].timestamp() * 1000)
    return f"{strategy_name}|{ts_ms}|{t.side}|{round(t.entry, 1)}"


def scan_once() -> Dict:
    """One scan: fetch fresh data, run strategies, log new signals."""
    try:
        import smc_book_strategies as smc
    except Exception as e:
        return {"status": "import_fail", "err": str(e)}

    df = _fetch_bybit_btc_1h(1500)
    if df is None or len(df) < 200:
        return {"status": "no_data"}

    state = _load_state()
    seen = set(state.get("seen_keys", []))

    fn_map = {"s2_mss": smc.s2_mss, "s3_ote": smc.s3_ote, "s5_ftr": smc.s5_ftr}
    results = {"per_strategy": {}, "new_signals": 0, "total_signals": 0}

    new_logs: List[dict] = []
    for name in ENABLED:
        try:
            trades = fn_map[name](df)
        except Exception as e:
            results["per_strategy"][name] = {"error": str(e)}
            continue
        # Only consider trades where outcome is RESOLVED (not None / not 'flat')
        resolved = [t for t in trades if t.R is not None]
        per = {"total_resolved": len(resolved), "new_this_scan": 0}
        for t in resolved:
            key = _trade_key(name, t, df)
            if key in seen:
                continue
            seen.add(key)
            per["new_this_scan"] += 1
            ts_ms = int(df.index[t.entry_idx].timestamp() * 1000)
            new_logs.append({
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "strategy": name,
                "side": t.side,
                "entry_ts": datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc).isoformat(),
                "entry": round(t.entry, 2),
                "stop": round(t.stop, 2),
                "target": round(t.target, 2),
                "R": round(t.R, 3),
                "outcome": t.outcome,
                "key": key,
            })
        results["per_strategy"][name] = per
        results["total_signals"] += len(resolved)
        results["new_signals"] += per["new_this_scan"]

    # Append new logs
    if new_logs:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            for entry in new_logs:
                f.write(json.dumps(entry, default=str) + "\n")

    state["seen_keys"] = sorted(seen)
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)
    results["status"] = "ok"
    return results


def summary() -> Dict:
    """Aggregate stats from shadow_book_signals.jsonl."""
    if not os.path.exists(LOG_FILE):
        return {"n": 0}
    by_strat: Dict[str, list] = {}
    with open(LOG_FILE) as f:
        for ln in f:
            try:
                r = json.loads(ln)
                by_strat.setdefault(r["strategy"], []).append(r)
            except Exception:
                pass
    out = {"per_strategy": {}, "n_total": sum(len(v) for v in by_strat.values())}
    for s, trades in by_strat.items():
        wins = [t for t in trades if t.get("outcome") == "win"]
        losses = [t for t in trades if t.get("outcome") == "loss"]
        Rs = [t["R"] for t in trades if t.get("R") is not None]
        if not Rs:
            continue
        avg_R = sum(Rs) / len(Rs)
        total_R = sum(Rs)
        out["per_strategy"][s] = {
            "n": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "wr_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "avg_R": round(avg_R, 3),
            "total_R": round(total_R, 2),
        }
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        print(json.dumps(summary(), indent=2, default=str))
    else:
        print(json.dumps(scan_once(), indent=2, default=str))
