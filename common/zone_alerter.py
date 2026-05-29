"""
zone_alerter.py — Approach (heads-up) alerter for SMC zones.

Runs every minute. Fetches 15m / 1h / 4h / daily klines, detects zones
via the LuxAlgo port (luxalgo_smc), measures distance + ETA from
current live price, fires a Discord embed when price approaches a
zone moving toward it.

Per-TF thresholds (price needs to be inside one of these to alert):

  TF       ETA threshold     Distance threshold
  ──────   ────────────      ─────────────────
  daily    60 min            0.6%
  4h       30 min            0.5%
  1h       15 min            0.4%
  15m       5 min            0.2%

Per-zone cooldown: 30 min. Direction filter: only alert when current
price is moving toward the zone (recent 5-min net move > 0 toward it).

Discord destination: DISCORD_APPROACH_WEBHOOK env var (separate channel
from #signals — falls back to DISCORD_WATCH_WEBHOOK if unset).

State persists across runs in /home/ubuntu/common/zone_alerter_state.json.
"""
from __future__ import annotations

import os
import sys
import json
import time
import urllib.request
import hashlib
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, "/home/ubuntu/bot")
sys.path.insert(0, "/home/ubuntu/common")

import market_data
import luxalgo_smc as lux
import pandas as pd
from srs_logger import get_logger

# ─── Exchange selection (Binance default, Bybit perp opt-in) ──────────
# Set ZONE_ALERTER_EXCHANGE=bybit in /home/ubuntu/bot/.env to switch.
# Bybit symbol = "BTCUSDT" perpetual on the linear (USDT) category;
# this is what TradingView shows as "BYBIT:BTCUSDT.P".
EXCHANGE = os.environ.get("ZONE_ALERTER_EXCHANGE", "binance").lower()


def _fetch_bybit_klines(symbol: str, interval: str,
                       limit: int) -> "pd.DataFrame":
    """Fetch klines from Bybit v5 API (linear/perp). Paginated."""
    iv_map = {"15m": "15", "1h": "60", "4h": "240", "1d": "D"}
    iv = iv_map.get(interval, interval)
    all_rows: list = []
    end_ts = int(time.time() * 1000)
    while len(all_rows) < limit:
        n = min(1000, limit - len(all_rows))
        url = (f"https://api.bybit.com/v5/market/kline"
               f"?category=linear&symbol={symbol}&interval={iv}"
               f"&limit={n}&end={end_ts}")
        req = urllib.request.Request(url, headers={"User-Agent": "srs-zone/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        if data.get("retCode", -1) != 0:
            raise RuntimeError(f"bybit_error: {data.get('retMsg')}")
        rows = data.get("result", {}).get("list", [])
        if not rows:
            break
        # Bybit returns newest-first; prepend
        all_rows = rows + all_rows
        end_ts = int(rows[-1][0]) - 1
    out = []
    for r in all_rows:
        out.append({
            "timestamp": int(r[0]),
            "open":   float(r[1]),
            "high":   float(r[2]),
            "low":    float(r[3]),
            "close":  float(r[4]),
            "volume": float(r[5]),
        })
    df = pd.DataFrame(out)
    if df.empty:
        return df
    df = (df.sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"])
            .reset_index(drop=True))
    return df.tail(limit).reset_index(drop=True)


def _fetch_klines_for_alerter(interval: str, limit: int) -> "pd.DataFrame":
    """Exchange-aware kline fetch."""
    if EXCHANGE == "bybit":
        return _fetch_bybit_klines("BTCUSDT", interval, limit)
    df = market_data.download("BTCUSDT", interval, total_candles=limit)
    return df.reset_index(drop=True)

log = get_logger("zone_alerter")

STATE_FILE = "/home/ubuntu/common/zone_alerter_state.json"
COOLDOWN_SEC = 60 * 60                   # 60 min per cluster
DIRECTION_LOCK_SEC = 20 * 60             # don't flip direction within 20 min
MAX_ALERT_DISTANCE_PCT = 3.0             # ignore zones farther than this %
MIN_GLOBAL_FIRE_GAP_SEC = 5 * 60         # min gap between ANY two alerts

# (eta_min, dist_pct) — alert when EITHER eta < eta_min or distance < dist_pct
TF_THRESHOLDS = {
    "1d":  (60, 0.6),
    "4h":  (30, 0.5),
    "1h":  (15, 0.4),
    "15m": ( 5, 0.2),
}

# How many candles per TF (deeper = more historical zones detected)
TF_BARS = {
    "1d":  300,
    "4h":  500,
    "1h":  500,
    "15m": 500,
}

# Zone cache TTL per TF — how long detected zones stay valid before
# we re-download klines + re-detect. Rates trade-off Binance load vs.
# missing newly-formed zones. Klines only meaningfully change at candle
# close, so TTL ≈ candle interval is sufficient.
ZONE_CACHE_TTL_SEC = {
    "1d":  60 * 60,        # 1 hour
    "4h":  10 * 60,        # 10 min
    "1h":   2 * 60,        # 2 min
    "15m":      30,        # 30 sec
}
ZONE_CACHE_FILE = "/home/ubuntu/common/zone_alerter_zones_cache.json"


def _load_zone_cache() -> dict:
    if not os.path.exists(ZONE_CACHE_FILE):
        return {}
    try:
        with open(ZONE_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_zone_cache(cache: dict) -> None:
    try:
        tmp = ZONE_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, default=str)
        os.replace(tmp, ZONE_CACHE_FILE)
    except Exception as e:
        log.error("zone_cache_save_fail", err=str(e))


def _get_zones_cached(tf: str, n_bars: int, now_ts: float) -> List[dict]:
    """Return cached zones for TF if fresh, else re-detect + cache.
    Source exchange controlled by ZONE_ALERTER_EXCHANGE env var."""
    cache = _load_zone_cache()
    cache_tag = f"{EXCHANGE}_{tf}"
    entry = cache.get(cache_tag, {})
    ttl = ZONE_CACHE_TTL_SEC.get(tf, 60)
    if entry and (now_ts - float(entry.get("computed_at", 0))) < ttl:
        return entry.get("zones", [])
    df = _fetch_klines_for_alerter(tf, n_bars)
    if df is None or len(df) == 0:
        return []
    zones = _dedupe_zones(lux.all_active_zones(df))
    for z in zones:
        z["tf"] = tf
        z.pop("ts", None)
    cache[cache_tag] = {"zones": zones, "computed_at": now_ts}
    _save_zone_cache(cache)
    return zones

# How long the recent-velocity window is, in seconds
VELOCITY_WINDOW_SEC = 60 * 60   # 1 hour

# Direction filter: how many recent 1m moves to consider for "moving toward"
DIRECTION_LOOKBACK_SEC = 5 * 60   # last 5 minutes

# Color per zone kind for Discord embed
COLOR_OB_BULLISH   = 0x1848CC   # LuxAlgo blue
COLOR_OB_BEARISH   = 0xB22833   # LuxAlgo red
COLOR_FVG_BULLISH  = 0x089981   # green
COLOR_FVG_BEARISH  = 0xF23645   # red

WEBHOOK = (os.environ.get("DISCORD_APPROACH_WEBHOOK", "").strip()
           or os.environ.get("DISCORD_WATCH_WEBHOOK", "").strip()
           or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())

# Operator's saved TradingView layout — clickable in every alert.
TV_CHART_URL = os.environ.get(
    "TRADINGVIEW_CHART_URL",
    "https://www.tradingview.com/chart/pViMM9Zt/?symbol=BYBIT%3ABTCUSDT.P",
).strip()


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error("state_save_fail", err=str(e))


def _zone_key(tf: str, zone: dict) -> str:
    """Stable key for cooldown tracking. Same kind + similar price range
    in the same TF = same zone."""
    raw = f"{tf}|{zone['kind']}|{round(zone['low'], 0):.0f}|{round(zone['high'], 0):.0f}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _fetch_live_price(symbol: str = "BTCUSDT") -> Optional[float]:
    """Fetch live last-trade price from the configured exchange."""
    try:
        if EXCHANGE == "bybit":
            url = (f"https://api.bybit.com/v5/market/tickers"
                   f"?category=linear&symbol={symbol}")
            req = urllib.request.Request(url, headers={"User-Agent": "srs-zone/1.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            if data.get("retCode", -1) != 0:
                raise RuntimeError(data.get("retMsg", "bybit_error"))
            tickers = data.get("result", {}).get("list", [])
            if not tickers:
                raise RuntimeError("no_ticker")
            return float(tickers[0]["lastPrice"])
        else:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
            req = urllib.request.Request(url, headers={"User-Agent": "srs-zone/1.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return float(json.loads(r.read())["price"])
    except Exception as e:
        log.warn("live_price_fail", exchange=EXCHANGE, err=str(e))
        return None


def _estimate_velocity(state: dict, current_price: float, now_ts: float
                      ) -> Tuple[float, float, int]:
    """Returns (abs_velocity_per_min, recent_signed_move, history_size).

    abs_velocity_per_min: average |Δprice| per minute over last hour
    recent_signed_move:   net price change over last DIRECTION_LOOKBACK_SEC
                          (positive = moving up, negative = down)
    history_size:         number of price points in the 2h history.
                          Used by callers to decide whether direction is
                          trustworthy yet (need ≥3 points for that).
    """
    history = state.setdefault("price_history", [])
    history.append({"ts": now_ts, "price": current_price})
    cutoff = now_ts - 2 * 3600
    history = [p for p in history if p["ts"] >= cutoff]
    state["price_history"] = history

    win_cut = now_ts - VELOCITY_WINDOW_SEC
    in_win = [p for p in history if p["ts"] >= win_cut]
    abs_velocity = 0.0
    if len(in_win) >= 2:
        deltas = []
        for i in range(1, len(in_win)):
            dt_min = (in_win[i]["ts"] - in_win[i-1]["ts"]) / 60.0
            if dt_min <= 0:
                continue
            deltas.append(abs(in_win[i]["price"] - in_win[i-1]["price"]) / dt_min)
        abs_velocity = (sum(deltas) / len(deltas)) if deltas else 0.0

    dir_cut = now_ts - DIRECTION_LOOKBACK_SEC
    in_dir = [p for p in history if p["ts"] >= dir_cut]
    signed_move = 0.0
    if len(in_dir) >= 2:
        signed_move = in_dir[-1]["price"] - in_dir[0]["price"]
    return abs_velocity, signed_move, len(history)


def _compute_zone_proximity(zone: dict, current_price: float,
                            abs_velocity: float, signed_move: float,
                            direction_trustworthy: bool = True
                            ) -> Optional[Dict]:
    """For a single zone, compute distance %, ETA, direction.

    direction_trustworthy: if False (e.g. we don't have enough price
    history yet), the direction filter is skipped — distance threshold
    alone decides.
    """
    z_low = zone["low"]
    z_high = zone["high"]
    if z_low <= current_price <= z_high:
        return None    # already inside zone — no pre-alert needed

    if current_price < z_low:
        distance = z_low - current_price
        approach_side = "below"
        moving_toward = signed_move > 0
        moving_away_strongly = signed_move < -current_price * 0.0005
    else:
        distance = current_price - z_high
        approach_side = "above"
        moving_toward = signed_move < 0
        moving_away_strongly = signed_move > current_price * 0.0005

    # Direction filter only blocks if we know price is moving AWAY strongly.
    # If signed_move is small (flat / no info), allow.
    if direction_trustworthy and moving_away_strongly:
        return None

    distance_pct = distance / current_price * 100.0
    if abs_velocity > 0 and moving_toward:
        eta_min = distance / abs_velocity
    else:
        eta_min = float("inf")

    return {
        "distance": distance,
        "distance_pct": distance_pct,
        "eta_min": eta_min,
        "approach_side": approach_side,
        "moving_toward": moving_toward,
    }


def _should_alert(tf: str, prox: Dict) -> bool:
    eta_thresh, dist_thresh = TF_THRESHOLDS.get(tf, (15, 0.4))
    return prox["eta_min"] < eta_thresh or prox["distance_pct"] < dist_thresh


def _post_multipart(webhook_url: str, payload: dict,
                    png_bytes: bytes,
                    filename: str = "chart.png") -> bool:
    """POST a Discord webhook with a PNG attachment via multipart/form-data."""
    boundary = "srsboundary" + str(int(time.time() * 1000))
    parts = [
        ("--" + boundary).encode(),
        b'Content-Disposition: form-data; name="payload_json"',
        b"Content-Type: application/json", b"",
        json.dumps(payload).encode(),
        ("--" + boundary).encode(),
        f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"'.encode(),
        b"Content-Type: image/png", b"",
        png_bytes,
        ("--" + boundary + "--").encode(),
    ]
    body = b"\r\n".join(parts)
    req = urllib.request.Request(
        webhook_url, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                 "User-Agent": "srs-zone-alerter/1.0"})
    urllib.request.urlopen(req, timeout=10).read()
    return True


def _post_cluster_alert(cluster: List[dict], cls: dict, current_price: float,
                        prox: dict, abs_velocity: float, signed_move: float,
                        path_fvgs: List[dict],
                        all_zones: Optional[List[dict]] = None) -> bool:
    if not WEBHOOK:
        log.warn("no_webhook")
        return False

    direction = cls["direction"]
    action = "potential LONG" if direction == "bullish" else "potential SHORT"
    arrow = "↓ down to" if direction == "bullish" else "↑ up to"

    # Zones in cluster — listed compactly
    zones_lines = []
    for z in sorted(cluster, key=lambda x: -x["low"]):
        z_kind = z["kind"].replace("_", " ")
        zones_lines.append(
            f"• `{z['tf']}` **{z_kind}** `${z['low']:,.0f}` – `${z['high']:,.0f}`")
    zones_block = "\n".join(zones_lines)

    eta_str = f"~{prox['eta_min']:.1f} min" if prox["eta_min"] < 999 else "—"

    # Path FVG line — only if directional confirmation present
    path_line = ""
    if path_fvgs:
        path_descs = []
        for f in path_fvgs[:3]:   # cap at 3
            path_descs.append(
                f"`{f['tf']}` {f['kind'].replace('_', ' ')} "
                f"${f['low']:,.0f}–${f['high']:,.0f}")
        path_line = ("\n**🧭 FVG in path:** " + " · ".join(path_descs)
                     + f" {'(' + str(len(path_fvgs)) + ' total)' if len(path_fvgs) > 3 else ''}"
                     + "\n_Price is being magnetized toward this zone "
                     "(unfilled FVG between current and OB)._")

    tier_note = {
        1:   "_Most OBs hit. High-probability heads-up._",
        2:   "_OB + FVG combo — better to enter when this triggers._",
        2.5: "_Multiple timeframes aligned — strong setup. Better to enter._",
        3:   "_PRIME setup: multi-TF + OB + FVG all confluencing. Best to enter._",
    }.get(cls["tier"], "")

    body = (
        f"**Cluster range:** `${prox['cluster_low']:,.0f}` — "
        f"`${prox['cluster_high']:,.0f}` ({direction.upper()})\n"
        f"**Distance:** `{prox['distance_pct']:.2f}%` {prox['approach_side']} cluster\n"
        f"**ETA:** {eta_str} at current pace · velocity ${abs_velocity:.0f}/min\n"
        f"**TFs:** {' · '.join(cls['tfs'])}\n"
        f"\n**Zones in cluster:**\n{zones_block}"
        f"{path_line}"
        f"\n\n{cls['subtitle']}\n{tier_note}"
    )

    title = f"{cls['title_emoji']} BTCUSDT {arrow} {cls['title']}"
    # Append the TradingView chart link to the alert body
    body_with_link = body + f"\n\n📊 [Open chart]({TV_CHART_URL})"

    embed = {
        "title": title,
        "url": TV_CHART_URL,
        "description": body_with_link,
        "color": cls["color"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Educational · approach pre-alert · tier {cls['tier']}"},
    }
    payload = {
        "content": "@everyone",
        "allowed_mentions": {"parse": ["everyone"]},
        "username": "👁️ Zone Approach",
        "embeds": [embed],
    }

    # ─── Render chart PNG and attach via multipart ───────────────────
    png = b""
    try:
        import zone_chart
        df_15m = _fetch_klines_for_alerter("15m", 200).reset_index(drop=True)
        png = zone_chart.render_zone_chart(
            df_15m, current_price, cluster,
            all_zones or [],
            direction=cls["direction"],
            tier=cls["tier"],
            tfs=cls["tfs"])
    except Exception as e:
        log.warn("chart_render_fail", err=str(e))
        png = b""

    try:
        if png:
            embed["image"] = {"url": "attachment://chart.png"}
            return _post_multipart(WEBHOOK, payload, png, "chart.png")
        else:
            req = urllib.request.Request(
                WEBHOOK, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "srs-zone-alerter/1.0"})
            urllib.request.urlopen(req, timeout=8).read()
            return True
    except Exception as e:
        log.error("post_fail", err=str(e))
        return False


def _dedupe_zones(zones: List[dict]) -> List[dict]:
    """Drop near-identical zones (LuxAlgo can detect same zone twice from
    different pivots). De-dupe by (kind, rounded low, rounded high)."""
    seen = set()
    out = []
    for z in zones:
        key = (z["kind"], round(z["low"], 0), round(z["high"], 0))
        if key in seen:
            continue
        seen.add(key)
        out.append(z)
    return out


def _zones_overlap(a: dict, b: dict, slack_pct: float = 0.10) -> bool:
    """Two zones overlap if their price ranges intersect (with slack)."""
    mid = (a["high"] + a["low"] + b["high"] + b["low"]) / 4.0
    slack = mid * slack_pct / 100.0
    return (a["low"] - slack) <= b["high"] and (b["low"] - slack) <= a["high"]


def _direction_of(zone: dict) -> str:
    return "bullish" if "bullish" in zone["kind"] else "bearish"


# Max % spread within a single cluster. Without this cap, transitive
# overlap-chaining merges all bullish zones across $3-5k ranges into
# one mega-cluster, which mis-represents what's actually a single
# tradable zone. Tightening: a zone can join a cluster only if its
# centroid is within MAX_CLUSTER_SPREAD_PCT of the cluster's centroid.
MAX_CLUSTER_SPREAD_PCT = 1.5
# Hard cap on TOTAL cluster range (in case long chains slip past the
# per-join centroid check). Cluster simply not extended past this.
MAX_CLUSTER_TOTAL_RANGE_PCT = 2.0


def _zone_mid(z: dict) -> float:
    return (z["high"] + z["low"]) / 2.0


def _cluster_centroid(cluster: List[dict]) -> float:
    mids = [_zone_mid(z) for z in cluster]
    return sum(mids) / len(mids)


def _cluster_zones(all_zones: List[dict]) -> List[List[dict]]:
    """Group overlapping zones — same direction AND centroid-proximity gated.

    A zone joins a cluster only if (a) it overlaps SOME zone in the
    cluster AND (b) its centroid is within MAX_CLUSTER_SPREAD_PCT of
    the cluster's running centroid. Prevents long chains like
    [zone at $74k] ↔ [overlap chain] ↔ [zone at $76.6k] from collapsing
    into one false super-cluster.
    """
    clusters: List[List[dict]] = []
    for direction in ("bullish", "bearish"):
        same_dir = [z for z in all_zones if _direction_of(z) == direction]
        # Sort by midpoint so cluster expansion goes through nearest neighbours first
        same_dir.sort(key=_zone_mid)
        used = [False] * len(same_dir)
        for i, seed in enumerate(same_dir):
            if used[i]:
                continue
            cluster = [seed]
            used[i] = True
            changed = True
            while changed:
                changed = False
                centroid = _cluster_centroid(cluster)
                for j, other in enumerate(same_dir):
                    if used[j]:
                        continue
                    other_mid = _zone_mid(other)
                    centroid_dist_pct = (abs(other_mid - centroid)
                                         / max(centroid, 1.0) * 100.0)
                    if centroid_dist_pct > MAX_CLUSTER_SPREAD_PCT:
                        continue
                    if any(_zones_overlap(c, other) for c in cluster):
                        # Hard cap: if joining would push cluster total
                        # range past MAX_CLUSTER_TOTAL_RANGE_PCT, reject
                        new_low = min(min(c["low"] for c in cluster), other["low"])
                        new_high = max(max(c["high"] for c in cluster), other["high"])
                        new_mid = (new_low + new_high) / 2.0
                        new_range_pct = (new_high - new_low) / max(new_mid, 1.0) * 100.0
                        if new_range_pct > MAX_CLUSTER_TOTAL_RANGE_PCT:
                            continue
                        cluster.append(other)
                        used[j] = True
                        changed = True
            clusters.append(cluster)
    return clusters


def _classify_cluster(cluster: List[dict]) -> Optional[Dict]:
    """Decide the alert tier + framing for a cluster.
    Returns None if the cluster shouldn't alert (e.g. FVG-only).
    """
    has_ob = any("OB" in z["kind"] for z in cluster)
    has_fvg = any("FVG" in z["kind"] for z in cluster)
    tfs = sorted({z["tf"] for z in cluster})
    is_multi_tf = len(tfs) > 1
    direction = _direction_of(cluster[0])

    # Per user spec: NO alert on FVG-only clusters.
    if not has_ob:
        return None

    # Determine tier
    if is_multi_tf and has_fvg:
        tier = 3
        title_emoji = "🔥"
        title = "PRIME · Multi-TF OB+FVG Combo"
        subtitle = ("Multiple timeframes confluencing with both OB and "
                    "FVG — strongest possible setup.")
        color = 0xFFD700  # gold
    elif is_multi_tf:
        tier = 2.5
        title_emoji = "⭐"
        title = "Multi-TF OB Confluence"
        subtitle = "OBs from different timeframes aligned at same price area."
        color = 0xFFA500  # orange
    elif has_fvg:
        tier = 2
        title_emoji = "🎯"
        title = "OB + FVG Combo"
        subtitle = "Better to enter — OB and FVG overlapping in this zone."
        color = 0x00C8FF  # cyan
    else:
        tier = 1
        title_emoji = "👁️"
        title = "OB Approach"
        subtitle = "Most of these hit — heads up."
        # Use the OB's native color
        if direction == "bullish":
            color = COLOR_OB_BULLISH
        else:
            color = COLOR_OB_BEARISH

    return {
        "tier": tier,
        "title_emoji": title_emoji,
        "title": title,
        "subtitle": subtitle,
        "color": color,
        "tfs": tfs,
        "is_multi_tf": is_multi_tf,
        "has_combo": has_fvg,
        "direction": direction,
    }


def _cluster_key(cluster: List[dict]) -> str:
    """Stable cooldown key — direction + ANCHORED-OB price bucket.

    Anchors on the largest OB in the cluster (OBs are fixed historical
    zones, more stable than mid which drifts as cluster membership
    changes). Falls back to cluster mid if no OB. Bucket size widened
    to $500 ($1000 above 100k) so small drift cannot escape cooldown.
    """
    direction = _direction_of(cluster[0])
    obs = [z for z in cluster if "OB" in z["kind"]]
    if obs:
        # Use the OB with the most-volume / widest range as anchor
        ob = max(obs, key=lambda z: z["high"] - z["low"])
        anchor = (ob["high"] + ob["low"]) / 2.0
    else:
        anchor = (min(z["low"] for z in cluster)
                  + max(z["high"] for z in cluster)) / 2.0
    step = 500.0 if anchor < 100_000 else 1000.0
    bucket = int(anchor / step)
    raw = f"{direction}|{bucket}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _cluster_proximity(cluster: List[dict], current_price: float,
                       abs_velocity: float, signed_move: float,
                       direction_trustworthy: bool
                       ) -> Optional[Dict]:
    """Distance to NEAREST edge of any zone in cluster + ETA + direction."""
    cluster_low = min(z["low"] for z in cluster)
    cluster_high = max(z["high"] for z in cluster)

    if cluster_low <= current_price <= cluster_high:
        return None  # already inside

    if current_price < cluster_low:
        distance = cluster_low - current_price
        approach_side = "below"
        moving_toward = signed_move > 0
        moving_away_strongly = signed_move < -current_price * 0.0005
    else:
        distance = current_price - cluster_high
        approach_side = "above"
        moving_toward = signed_move < 0
        moving_away_strongly = signed_move > current_price * 0.0005

    if direction_trustworthy and moving_away_strongly:
        return None

    distance_pct = distance / current_price * 100.0
    if abs_velocity > 0 and moving_toward:
        eta_min = distance / abs_velocity
    else:
        eta_min = float("inf")

    return {
        "distance": distance,
        "distance_pct": distance_pct,
        "eta_min": eta_min,
        "approach_side": approach_side,
        "moving_toward": moving_toward,
        "cluster_low": cluster_low,
        "cluster_high": cluster_high,
    }


def _cluster_thresholds(cluster: List[dict]) -> Tuple[float, float]:
    """Use the most generous thresholds among the TFs in the cluster
    (since the cluster represents one price area, the higher TF wins)."""
    tfs = {z["tf"] for z in cluster}
    eta = max(TF_THRESHOLDS.get(tf, (15, 0.4))[0] for tf in tfs)
    dist = max(TF_THRESHOLDS.get(tf, (15, 0.4))[1] for tf in tfs)
    return eta, dist


def _fvgs_in_path(cluster: List[dict], current_price: float,
                  all_zones: List[dict]) -> List[dict]:
    """Find FVGs sitting in the price path between current_price and the
    cluster's nearest edge — directional confirmation that price is being
    magnetized toward the OB.

    Excludes FVGs already part of the cluster.
    """
    cluster_low = min(z["low"] for z in cluster)
    cluster_high = max(z["high"] for z in cluster)
    if current_price > cluster_high:
        path_low, path_high = cluster_high, current_price
    elif current_price < cluster_low:
        path_low, path_high = current_price, cluster_low
    else:
        return []   # current price already inside cluster

    cluster_ids = {(z["kind"], round(z["low"], 0), round(z["high"], 0))
                   for z in cluster}
    in_path: List[dict] = []
    for z in all_zones:
        if "FVG" not in z["kind"]:
            continue
        z_id = (z["kind"], round(z["low"], 0), round(z["high"], 0))
        if z_id in cluster_ids:
            continue
        z_mid = (z["low"] + z["high"]) / 2.0
        # Any part of FVG inside the path region counts
        if z["high"] >= path_low and z["low"] <= path_high:
            in_path.append(z)
    return in_path


def scan_once() -> Dict:
    """Run one scan iteration. Returns summary dict."""
    state = _load_state()
    now_ts = time.time()

    cur = _fetch_live_price("BTCUSDT")
    if cur is None:
        return {"status": "no_live_price", "fired": 0}

    abs_velocity, signed_move, hist_size = _estimate_velocity(state, cur, now_ts)
    # Need ≥3 price points spanning ≥2 minutes to trust direction
    direction_trustworthy = hist_size >= 3

    summary = {
        "status": "ok",
        "current_price": cur,
        "velocity_per_min": abs_velocity,
        "signed_move_5m": signed_move,
        "history_size": hist_size,
        "direction_trustworthy": direction_trustworthy,
        "fired": 0,
        "checked_clusters": 0,
        "fvg_only_skipped": 0,
        "alertable_clusters": 0,
    }

    # ─── Step 1: gather all zones from all TFs (cached) ───────────────
    all_zones: List[dict] = []
    per_tf_counts: Dict[str, int] = {}
    for tf, n_bars in TF_BARS.items():
        try:
            zones = _get_zones_cached(tf, n_bars, now_ts)
            all_zones.extend(zones)
            per_tf_counts[tf] = len(zones)
        except Exception as e:
            log.error("scan_tf_fail", tf=tf, err=str(e))
            per_tf_counts[tf] = 0
    summary["zones_per_tf"] = per_tf_counts

    # ─── Step 2: cluster overlapping same-direction zones ─────────────
    clusters = _cluster_zones(all_zones)
    summary["total_clusters"] = len(clusters)

    # ─── Step 3: classify, proximity-check, alert ─────────────────────
    # Direction-lock: if we recently fired in one direction, suppress
    # the opposite direction for DIRECTION_LOCK_SEC. Stops "up/down/up/down"
    # rapid alternation when price chops near a midpoint between zones.
    last_fire = state.setdefault("last_fire", {})
    last_bull_fire = float(last_fire.get("bullish", 0))
    last_bear_fire = float(last_fire.get("bearish", 0))
    last_any_fire = max(last_bull_fire, last_bear_fire,
                        float(last_fire.get("any", 0)))

    # Global gap: if ANY alert fired within MIN_GLOBAL_FIRE_GAP_SEC,
    # block all alerts this scan. Closest cluster wins overall.
    if now_ts - last_any_fire < MIN_GLOBAL_FIRE_GAP_SEC:
        gap_remaining = int(MIN_GLOBAL_FIRE_GAP_SEC - (now_ts - last_any_fire))
        log.info("global_gap_active", remaining_sec=gap_remaining)
        _save_state(state)
        summary["status"] = "global_gap_active"
        summary["gap_remaining_sec"] = gap_remaining
        return summary

    # Per-scan limit: at most 1 alert per direction per scan. Pick the
    # cluster CLOSEST to current price first (most actionable).
    fired_this_scan = {"bullish": False, "bearish": False}

    # Pre-evaluate clusters and sort by distance to current price
    cluster_evaluations = []
    for cluster in clusters:
        cls = _classify_cluster(cluster)
        if cls is None:
            summary["fvg_only_skipped"] += 1
            continue
        prox = _cluster_proximity(cluster, cur, abs_velocity,
                                  signed_move, direction_trustworthy)
        if prox is None:
            continue
        cluster_evaluations.append((prox["distance_pct"], cluster, cls, prox))
    cluster_evaluations.sort(key=lambda x: x[0])  # nearest first

    for distance_pct, cluster, cls, prox in cluster_evaluations:
        summary["checked_clusters"] += 1

        # Skip zones far from current — operator can't see them on chart
        if prox["distance_pct"] > MAX_ALERT_DISTANCE_PCT:
            continue

        eta_thr, dist_thr = _cluster_thresholds(cluster)
        if not (prox["eta_min"] < eta_thr or prox["distance_pct"] < dist_thr):
            continue
        summary["alertable_clusters"] += 1

        # Per-scan limit: skip if we already fired this direction
        if fired_this_scan[cls["direction"]]:
            continue

        # Cooldown (per-cluster)
        ckey = _cluster_key(cluster)
        last = float(state.get("last_alert", {}).get(ckey, 0))
        if now_ts - last < COOLDOWN_SEC:
            continue

        # Direction lock — don't flip-flop
        opp_fire = last_bear_fire if cls["direction"] == "bullish" else last_bull_fire
        if now_ts - opp_fire < DIRECTION_LOCK_SEC:
            log.info("direction_locked",
                     direction=cls["direction"],
                     opp_age_sec=int(now_ts - opp_fire))
            continue

        # Path-FVG enhancement: directional confirmation
        path_fvgs = _fvgs_in_path(cluster, cur, all_zones)

        # Tier upgrade: OB-alone (tier 1) gets bumped to "OB + FVG-in-path"
        # if there are path FVGs — directional magnet
        if cls["tier"] == 1 and path_fvgs:
            cls = dict(cls)
            cls["tier"] = 1.5
            cls["title_emoji"] = "🎯"
            cls["title"] = "OB Approach · FVG in path"
            cls["subtitle"] = ("FVG sits between current price and the OB — "
                               "price likely magnetized toward the zone.")
            cls["color"] = 0x00B7FF  # bright cyan

        ok = _post_cluster_alert(cluster, cls, cur, prox,
                                 abs_velocity, signed_move, path_fvgs,
                                 all_zones=all_zones)
        if ok:
            state.setdefault("last_alert", {})[ckey] = now_ts
            last_fire[cls["direction"]] = now_ts
            last_fire["any"] = now_ts          # global gap anchor
            fired_this_scan[cls["direction"]] = True
            if cls["direction"] == "bullish":
                last_bull_fire = now_ts
            else:
                last_bear_fire = now_ts
            summary["fired"] += 1
            # After firing once globally, stop iterating — give the gap
            # window before the next alert (closest already wins because
            # we sorted by distance).
            break
            log.info("cluster_alert_fired",
                     tier=cls["tier"], direction=cls["direction"],
                     tfs=",".join(cls["tfs"]),
                     n_zones=len(cluster), path_fvgs=len(path_fvgs),
                     low=prox["cluster_low"], high=prox["cluster_high"],
                     distance_pct=round(prox["distance_pct"], 2),
                     eta_min=round(prox["eta_min"], 1))

    _save_state(state)
    log.info("scan_complete",
             price=cur, velocity=round(abs_velocity, 1),
             clusters=summary["total_clusters"],
             checked=summary["checked_clusters"],
             alertable=summary["alertable_clusters"],
             fvg_only_skipped=summary["fvg_only_skipped"],
             fired=summary["fired"])
    return summary


if __name__ == "__main__":
    res = scan_once()
    print(json.dumps(res, indent=2, default=str))
