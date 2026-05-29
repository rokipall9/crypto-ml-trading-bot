"""
price_alert.py — Independent technical-level alerter.

Subscribers / operator register price levels with conditions:
  "alert when BTCUSDT crosses ABOVE 100000"
  "alert when ETHUSDT crosses BELOW 3500"
  "alert when BTCUSDT moves >5% in 1h"

Runs every 60s via systemd timer, polls Binance ticker, fires Discord
post on triggered levels (one-shot — level deactivates after firing).

Independent of strategies. Independent of regime. Independent of ledger.
Pure utility. Works when nothing else does.

Storage: /home/ubuntu/common/price_levels.json (atomic).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("price_alert")

LEVELS_FILE = "/home/ubuntu/common/price_levels.json"
PRICE_HISTORY = "/home/ubuntu/common/price_history.jsonl"
HISTORY_KEEP_HOURS = 48

try:
    from dotenv import load_dotenv
    load_dotenv("/home/ubuntu/bot/.env")
except Exception:
    pass

WEBHOOK = (os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
           or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())

TV_CHART_URL = os.environ.get(
    "TRADINGVIEW_CHART_URL",
    "https://www.tradingview.com/chart/pViMM9Zt/?symbol=BYBIT%3ABTCUSDT.P",
).strip()


def _load() -> Dict[str, list]:
    if not os.path.exists(LEVELS_FILE):
        return {}
    try:
        with open(LEVELS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: Dict[str, list]) -> None:
    tmp = LEVELS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    os.replace(tmp, LEVELS_FILE)


def add_level(symbol: str, level: float, direction: str,
              note: str = "", rearm: bool = False,
              skip_sanity: bool = False) -> dict:
    """direction: 'above' or 'below'. Fires when price crosses the level.

    rearm=True: re-arm automatically after firing (good for S/R levels).
    skip_sanity=True: bypass the "would fire immediately" check.
    """
    if direction not in ("above", "below"):
        raise ValueError("direction must be 'above' or 'below'")
    sym_u = symbol.upper()
    # Sanity check: refuse to register a level that would fire on the
    # next tick (operator typo guard: "above 50000" when BTC is at 77000).
    if not skip_sanity:
        try:
            cur_map = fetch_prices([sym_u])
            cur = cur_map.get(sym_u, {}).get("price")
            if cur is not None:
                would_fire = ((direction == "above" and cur >= float(level))
                              or (direction == "below" and cur <= float(level)))
                if would_fire:
                    raise ValueError(
                        f"level {direction} ${float(level):,.2f} would fire "
                        f"immediately (current {sym_u}=${cur:,.2f}). "
                        f"Pass skip_sanity=True to override.")
        except ValueError:
            raise
        except Exception as e:
            log.warn("sanity_check_unreachable", err=str(e))
    d = _load()
    rec = {
        "id": f"{sym_u}_{direction}_{level}_{int(time.time())}",
        "symbol": sym_u,
        "level": float(level),
        "direction": direction,
        "note": note,
        "rearm": bool(rearm),
        "fire_count": 0,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "status": "armed",
    }
    d.setdefault(sym_u, []).append(rec)
    _save(d)
    log.info("level_added", symbol=symbol, price_level=level,
             direction=direction, rearm=rearm)
    return rec


def remove_level(level_id: str) -> bool:
    d = _load()
    for sym, recs in d.items():
        for r in recs:
            if r.get("id") == level_id:
                recs.remove(r)
                _save(d)
                return True
    return False


def list_levels(symbol: Optional[str] = None) -> List[dict]:
    d = _load()
    if symbol:
        return [r for r in d.get(symbol.upper(), [])
                if r.get("status") == "armed"]
    out = []
    for recs in d.values():
        out.extend(r for r in recs if r.get("status") == "armed")
    return out


def _fetch_one_ticker(sym: str) -> Tuple[str, Optional[dict]]:
    """Fetch a single 24h ticker. Used by fetch_prices in parallel."""
    try:
        url = (f"https://api.binance.com/api/v3/ticker/24hr"
               f"?symbol={sym}")
        req = urllib.request.Request(
            url, headers={"User-Agent": "srs-price-alert/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        return sym, {
            "price": float(data["lastPrice"]),
            "change_24h_pct": float(data["priceChangePercent"]),
            "high_24h": float(data["highPrice"]),
            "low_24h": float(data["lowPrice"]),
            "volume_24h": float(data["volume"]),
        }
    except Exception as e:
        log.warn("ticker_fail", symbol=sym, err=str(e))
        return sym, None


def fetch_prices(symbols: List[str]) -> Dict[str, dict]:
    """Get live ticker for each symbol in parallel.

    Returns {sym: {price, change_24h, high_24h, low_24h, volume_24h}}.
    Parallelism cuts dashboard latency from ~N×400ms to ~400ms.
    """
    if not symbols:
        return {}
    # ThreadPoolExecutor caps at 8 to avoid hammering Binance if someone
    # passes a huge symbol list.
    from concurrent.futures import ThreadPoolExecutor
    out: Dict[str, dict] = {}
    workers = min(len(symbols), 8)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for sym, info in pool.map(_fetch_one_ticker, symbols):
            if info is not None:
                out[sym] = info
    return out


def _record_history(prices: Dict[str, dict]) -> None:
    """Append snapshot to rolling history file."""
    try:
        with open(PRICE_HISTORY, "a", encoding="utf-8") as f:
            for sym, p in prices.items():
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "symbol": sym, **p}) + "\n")
    except Exception:
        pass


def _post_alert(rec: dict, current_price: float,
                ctx: Optional[dict] = None) -> bool:
    if not WEBHOOK:
        log.warn("no_webhook")
        return False
    direction_emoji = "📈" if rec["direction"] == "above" else "📉"
    color = 0x00C851 if rec["direction"] == "above" else 0xFF4444
    delta_pct = ((current_price - rec["level"]) / rec["level"] * 100.0
                 if rec["level"] else 0.0)
    # Body: trigger + current + how-far-past + 24h context
    body_parts = [
        f"**Current:** `${current_price:,.2f}` ({delta_pct:+.2f}% past trigger)",
        f"**Trigger:** `${rec['level']:,.2f}`",
    ]
    if ctx:
        h24 = ctx.get("high_24h")
        l24 = ctx.get("low_24h")
        ch24 = ctx.get("change_24h_pct")
        if h24 and l24:
            body_parts.append(
                f"**24h Range:** `${l24:,.0f}` – `${h24:,.0f}`"
                + (f" ({ch24:+.2f}%)" if ch24 is not None else ""))
    if rec.get("note"):
        body_parts.append(f"_{rec['note']}_")
    if rec.get("fire_count", 0) > 0:
        body_parts.append(f"*Re-arm fire #{rec['fire_count'] + 1}*")
    body_parts.append(f"\n📊 [Open chart]({TV_CHART_URL})")
    payload = {
        "username": "🚨 Price Alert",
        "embeds": [{
            "title": f"{direction_emoji} {rec['symbol']} crossed "
                     f"{rec['direction']} ${rec['level']:,.0f}",
            "url": TV_CHART_URL,
            "description": "\n".join(body_parts),
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Educational · price-level alerter"},
        }],
    }
    try:
        req = urllib.request.Request(
            WEBHOOK, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "srs-price-alert/1.0"})
        urllib.request.urlopen(req, timeout=5).read()
        return True
    except Exception as e:
        log.error("post_fail", err=str(e))
        return False


REARM_COOLDOWN_SEC = 30 * 60   # don't double-fire a rearmed level inside 30 min


def check_and_fire() -> dict:
    """Run periodically. Check all armed levels against current prices,
    fire alerts on triggered levels. By default deactivates fired levels;
    if rec.rearm is True, the level stays armed (with a cooldown) so it
    can fire on subsequent crossings — useful for support/resistance."""
    d = _load()
    if not d:
        return {"checked": 0, "fired": 0}
    symbols = list(d.keys())
    prices = fetch_prices(symbols)
    _record_history(prices)
    fired = 0
    now_ts = time.time()
    for sym, recs in d.items():
        if sym not in prices:
            continue
        ctx = prices[sym]
        cur = ctx["price"]
        for rec in recs:
            if rec.get("status") != "armed":
                continue
            # Rearm cooldown: skip if just fired
            last_fired_ts = rec.get("last_fired_ts", 0)
            if rec.get("rearm") and last_fired_ts and (
                    now_ts - last_fired_ts) < REARM_COOLDOWN_SEC:
                continue
            triggered = (
                (rec["direction"] == "above" and cur >= rec["level"]) or
                (rec["direction"] == "below" and cur <= rec["level"])
            )
            if triggered:
                if _post_alert(rec, cur, ctx):
                    rec["fire_count"] = rec.get("fire_count", 0) + 1
                    rec["fired_at"] = datetime.now(timezone.utc).isoformat()
                    rec["fired_at_price"] = cur
                    rec["last_fired_ts"] = now_ts
                    if not rec.get("rearm"):
                        rec["status"] = "fired"
                    fired += 1
                    log.info("level_fired", symbol=sym,
                             price_level=rec["level"],
                             direction=rec["direction"],
                             current=cur,
                             rearm=bool(rec.get("rearm")),
                             fire_count=rec["fire_count"])
    if fired:
        _save(d)
    log.info("check_complete", checked=len(prices),
             armed=sum(1 for recs in d.values()
                       for r in recs if r.get("status") == "armed"),
             fired=fired)
    return {"checked": len(prices), "fired": fired}


def clear_fired(symbol: Optional[str] = None) -> int:
    """Sweep out levels with status='fired'. Keeps the JSON tidy.
    Returns count removed. Run weekly (or after big alert bursts)."""
    d = _load()
    removed = 0
    for sym in list(d.keys()):
        if symbol and sym != symbol.upper():
            continue
        kept = [r for r in d[sym] if r.get("status") == "armed"]
        removed += len(d[sym]) - len(kept)
        if kept:
            d[sym] = kept
        else:
            del d[sym]
    if removed:
        _save(d)
        log.info("cleared_fired", removed=removed, symbol=symbol or "all")
    return removed


def cleanup_history(keep_hours: int = HISTORY_KEEP_HOURS) -> int:
    """Trim price history file. Run weekly."""
    if not os.path.exists(PRICE_HISTORY):
        return 0
    cutoff = (datetime.now(timezone.utc).timestamp()
              - keep_hours * 3600)
    keep = []
    with open(PRICE_HISTORY, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(
                    str(r["ts"]).replace("Z", "+00:00")).timestamp()
                if ts >= cutoff:
                    keep.append(line)
            except Exception:
                pass
    with open(PRICE_HISTORY, "w", encoding="utf-8") as f:
        f.writelines(keep)
    return len(keep)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        print(json.dumps(check_and_fire(), indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "list":
        print(json.dumps(list_levels(), indent=2, default=str))
    elif len(sys.argv) >= 5 and sys.argv[1] == "add":
        sym, level, direction = sys.argv[2], float(sys.argv[3]), sys.argv[4]
        note = " ".join(sys.argv[5:]) if len(sys.argv) > 5 else ""
        rec = add_level(sym, level, direction, note)
        print(json.dumps(rec, indent=2, default=str))
    else:
        print("Usage:")
        print("  price_alert.py check                    # poll + fire")
        print("  price_alert.py list                     # show armed")
        print("  price_alert.py add BTCUSDT 100000 above [note]")
