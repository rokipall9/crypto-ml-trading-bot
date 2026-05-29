"""
STRATEGY 5 HUNT — Finding the best 5th edge
==============================================
Current 4 strategies cover:
  S1: Breakout (new highs + volume)
  S2: Momentum (explosive candle + volume spike)
  S3: Panic Dip (crash + bounce)
  S4: EMA Pullback (gradual drift to EMA40 + bounce)

GAP: No strategy catches LOW-VOLATILITY CONSOLIDATION → EXPANSION.
After trending, BTC often coils tight (decreasing range/volume) then
springs into the next leg. This is the highest win-rate pattern in trends.

Candidates:
  C1: SQUEEZE BREAKOUT — ATR compresses below average, then bullish expansion candle
  C2: VOLUME RESET — Volume dries up near EMA21, then re-expands bullishly
  C3: HIGHER LOW BREAK — After pullback, forms higher low + breaks recent range

All use same framework: 4H uptrend, trail stop, 1.5x ATR SL, 48-bar time stop.
"""
import sys
sys.path.insert(0, "/home/ubuntu/bot")
import market_data
import numpy as np
import pandas as pd

print("Downloading data...")
df_4h = market_data.download("BTCUSDT", "4h", total_candles=5000)
n4h = len(df_4h)
print("4h: %d candles (%d days)" % (n4h, n4h // 6))

h4 = df_4h["high"].values.astype(float)
l4 = df_4h["low"].values.astype(float)
c4 = df_4h["close"].values.astype(float)
o4 = df_4h["open"].values.astype(float)
v4 = df_4h["volume"].values.astype(float)

def ema(a, p): return pd.Series(a).ewm(span=p, adjust=False).mean().values
def sma(a, p): return pd.Series(a).rolling(p, min_periods=p).mean().values
def atr_f(h, l, c, p=14):
    n = len(h); tr = np.zeros(n)
    for i in range(1, n): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    return pd.Series(tr).rolling(p, min_periods=p).mean().values

def rsi_f(c, p=14):
    delta = np.diff(c, prepend=c[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).ewm(span=p, adjust=False).mean().values
    avg_loss = pd.Series(loss).ewm(span=p, adjust=False).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.ones_like(avg_gain)*50, where=avg_loss>0)
    return 100 - 100 / (1 + rs)

def get_month(i):
    if isinstance(df_4h.index, pd.DatetimeIndex): return str(df_4h.index[i])[:7]
    elif "open_time" in df_4h.columns: return str(df_4h["open_time"].iloc[i])[:7]
    return "?"

print("Computing indicators...")
ema21 = ema(c4, 21)
ema55 = ema(c4, 55)
ema50 = ema(c4, 50)
ema40 = ema(c4, 40)
atr14 = atr_f(h4, l4, c4, 14)
atr50 = atr_f(h4, l4, c4, 50)  # longer ATR for squeeze detection
rsi14 = rsi_f(c4, 14)

# Volume & body averages
avg_vol = pd.Series(v4).rolling(20, min_periods=20).mean().values
avg_body = pd.Series(np.abs(c4 - o4)).rolling(20, min_periods=20).mean().values
body = np.abs(c4 - o4)

# EMA50 slope
ema50_slope = np.zeros(n4h)
for i in range(6, n4h):
    if ema50[i-6] > 0:
        ema50_slope[i] = (ema50[i] - ema50[i-6]) / ema50[i-6] * 100

# Range over N bars
def range_n(h, l, n_bars):
    """Rolling range: max(high) - min(low) over last n bars."""
    rh = pd.Series(h).rolling(n_bars, min_periods=n_bars).max().values
    rl = pd.Series(l).rolling(n_bars, min_periods=n_bars).min().values
    return rh - rl, rh, rl

FRICTION = 20; START = 60; RISK = 0.01

def evaluate(trades, label):
    n = len(trades)
    if n < 5:
        print("  %s: %d trades (too few)" % (label, n))
        return None

    pnl_t = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = wins / n; ev = pnl_t / n

    eq = [10000.0]
    for t in trades: eq.append(eq[-1] + t["pnl"])
    ea = np.array(eq)
    mdd = abs(np.min(ea - np.maximum.accumulate(ea)))
    rdd = pnl_t / mdd if mdd > 0 else 0

    ws = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    ls = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = ws / ls if ls > 0 else 0

    monthly = {}
    for t in trades: monthly.setdefault(t["month"], []).append(t)
    mpnl = {m: sum(t["pnl"] for t in mt) for m, mt in monthly.items()}
    bm = max(mpnl.values()) if mpnl else 0
    conc = (pnl_t - bm) > 0
    half = n // 2
    h1 = sum(t["pnl"] for t in trades[:half])
    h2 = sum(t["pnl"] for t in trades[half:])
    wf = h1 > 0 and h2 > 0
    wm = sum(1 for v in mpnl.values() if v > 0); tm = len(mpnl)

    score = 0
    if pnl_t > 0: score += 1
    if conc: score += 1
    if wf: score += 1
    if n >= 20: score += 1
    if pf > 1.2: score += 1

    print("  %s: %d trades | WR %.0f%% | $%+.0f | PF %.2f | MDD $%.0f | R/DD %.2f" % (
        label, n, wr*100, pnl_t, pf, mdd, rdd))
    print("    Conc=%s WF=%s (H1=$%+.0f H2=$%+.0f) WinMo=%d/%d | Score: %d/5" % (
        "Y" if conc else "N", "Y" if wf else "N", h1, h2, wm, tm, score))
    return {"label": label, "n": n, "pnl": pnl_t, "wr": wr, "pf": pf, "mdd": mdd,
            "rdd": rdd, "score": score, "conc": conc, "wf": wf, "h1": h1, "h2": h2}


# ══════════════════════════════════════════════════════════════════════
#  CANDIDATE 1: SQUEEZE BREAKOUT
#  ATR compresses (current ATR < 0.8x long ATR), then expands with
#  bullish candle. Catching the spring after consolidation.
# ══════════════════════════════════════════════════════════════════════

def run_squeeze(squeeze_ratio, body_mult, trail_atr, lookback):
    balance = 10000.0; trades = []; active = None
    for i in range(START, n4h):
        if active is not None:
            new_t = c4[i] - trail_atr * atr14[i]
            if not np.isnan(new_t) and new_t > active["trail"]: active["trail"] = new_t
            if l4[i] <= active["sl"]:
                pnl = (active["sl"] - active["entry"]) * active["units"] - FRICTION
            elif active["trail"] > active["entry"] and l4[i] <= active["trail"]:
                pnl = (active["trail"] - active["entry"]) * active["units"] - FRICTION
            elif i >= active["ts"]:
                pnl = (c4[i] - active["entry"]) * active["units"] - FRICTION
            else: continue
            balance += pnl
            trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2)})
            active = None; continue

        if np.isnan(atr14[i]) or atr14[i] <= 0: continue
        if np.isnan(atr50[i]) or atr50[i] <= 0: continue
        if not (ema21[i] > ema55[i] and ema50_slope[i] > 0.1): continue

        # SQUEEZE: current ATR well below longer-term ATR
        if atr14[i] >= squeeze_ratio * atr50[i]: continue

        # EXPANSION: strong bullish candle breaking out of squeeze
        if c4[i] <= o4[i]: continue  # must be green
        if body[i] < body_mult * avg_body[i]: continue  # body must be expanding

        # Price in trend zone (close above EMA21)
        if c4[i] <= ema21[i]: continue

        # Breakout: close above recent range high
        rng, rng_high, rng_low = range_n(h4, l4, lookback)
        if np.isnan(rng_high[i-1]): continue
        if c4[i] <= rng_high[i-1]: continue  # must close above recent range

        entry = c4[i]
        sl = entry - 1.5 * atr14[i]
        sl_dist = entry - sl
        if sl_dist <= 0: continue

        units = (balance * RISK) / sl_dist
        active = {"entry": entry, "ei": i, "sl": sl, "trail": sl,
                  "ts": i + 48, "units": units}

    if active:
        pnl = (c4[-1] - active["entry"]) * active["units"] - FRICTION
        balance += pnl
        trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2)})

    return trades


# ══════════════════════════════════════════════════════════════════════
#  CANDIDATE 2: VOLUME RESET
#  Volume dries up near EMA21 (accumulation), then re-expands on
#  bullish candle. Smart money pattern.
# ══════════════════════════════════════════════════════════════════════

def run_vol_reset(vol_dry_ratio, vol_expand_mult, ema_dist_max, trail_atr):
    balance = 10000.0; trades = []; active = None
    for i in range(START, n4h):
        if active is not None:
            new_t = c4[i] - trail_atr * atr14[i]
            if not np.isnan(new_t) and new_t > active["trail"]: active["trail"] = new_t
            if l4[i] <= active["sl"]:
                pnl = (active["sl"] - active["entry"]) * active["units"] - FRICTION
            elif active["trail"] > active["entry"] and l4[i] <= active["trail"]:
                pnl = (active["trail"] - active["entry"]) * active["units"] - FRICTION
            elif i >= active["ts"]:
                pnl = (c4[i] - active["entry"]) * active["units"] - FRICTION
            else: continue
            balance += pnl
            trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2)})
            active = None; continue

        if np.isnan(atr14[i]) or atr14[i] <= 0: continue
        if np.isnan(avg_vol[i]) or avg_vol[i] <= 0: continue
        if not (ema21[i] > ema55[i] and ema50_slope[i] > 0.1): continue

        # VOLUME DRY-UP: check that recent bars had low volume (accumulation)
        # At least 2 of last 3 bars had below-average volume
        dry_count = 0
        for j in range(1, 4):
            if i-j >= 0 and v4[i-j] < vol_dry_ratio * avg_vol[i]:
                dry_count += 1
        if dry_count < 2: continue

        # VOLUME EXPANSION: current bar has above-average volume
        if v4[i] < vol_expand_mult * avg_vol[i]: continue

        # Price near EMA21 (within ema_dist_max ATR)
        dist_to_ema = abs(c4[i] - ema21[i])
        if dist_to_ema > ema_dist_max * atr14[i]: continue

        # Bullish candle
        if c4[i] <= o4[i]: continue
        # Close above EMA21
        if c4[i] <= ema21[i]: continue

        entry = c4[i]
        sl = entry - 1.5 * atr14[i]
        sl_dist = entry - sl
        if sl_dist <= 0: continue

        units = (balance * RISK) / sl_dist
        active = {"entry": entry, "ei": i, "sl": sl, "trail": sl,
                  "ts": i + 48, "units": units}

    if active:
        pnl = (c4[-1] - active["entry"]) * active["units"] - FRICTION
        balance += pnl
        trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2)})

    return trades


# ══════════════════════════════════════════════════════════════════════
#  CANDIDATE 3: HIGHER LOW BREAK
#  After pullback, price forms a higher low (above recent swing low)
#  then breaks above recent range. Classic trend continuation.
# ══════════════════════════════════════════════════════════════════════

def run_higher_low(lookback, min_pullback_atr, trail_atr):
    balance = 10000.0; trades = []; active = None
    for i in range(START, n4h):
        if active is not None:
            new_t = c4[i] - trail_atr * atr14[i]
            if not np.isnan(new_t) and new_t > active["trail"]: active["trail"] = new_t
            if l4[i] <= active["sl"]:
                pnl = (active["sl"] - active["entry"]) * active["units"] - FRICTION
            elif active["trail"] > active["entry"] and l4[i] <= active["trail"]:
                pnl = (active["trail"] - active["entry"]) * active["units"] - FRICTION
            elif i >= active["ts"]:
                pnl = (c4[i] - active["entry"]) * active["units"] - FRICTION
            else: continue
            balance += pnl
            trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2)})
            active = None; continue

        if np.isnan(atr14[i]) or atr14[i] <= 0: continue
        if not (ema21[i] > ema55[i] and ema50_slope[i] > 0.1): continue

        # Find swing low in lookback window (lowest low)
        if i < lookback + 3: continue
        window_lows = l4[i-lookback:i]
        swing_low_idx = np.argmin(window_lows)
        swing_low = window_lows[swing_low_idx]
        swing_low_abs_idx = i - lookback + swing_low_idx

        # Must have been a meaningful pullback (swing low below EMA21 at that time)
        # or at least min_pullback_atr below the high before it
        window_highs = h4[i-lookback:i]
        swing_high = np.max(window_highs)
        pullback_depth = (swing_high - swing_low) / atr14[i] if atr14[i] > 0 else 0
        if pullback_depth < min_pullback_atr: continue

        # Swing low shouldn't be the CURRENT bar (we need higher low forming)
        if swing_low_abs_idx >= i - 2: continue

        # HIGHER LOW: current low > swing low (price holding above previous low)
        if l4[i] <= swing_low: continue

        # Current low should be a local low (lower than neighbors)
        # Actually just need it near support, not necessarily the lowest
        # Let's check: recent bars (last 3) had a low near current level
        recent_low = min(l4[i-2:i+1])
        if recent_low <= swing_low: continue  # must be higher low

        # BREAK: close above recent 5-bar high (breaking up from the higher low)
        recent_high = max(h4[i-5:i])
        if c4[i] <= recent_high: continue

        # Bullish candle
        if c4[i] <= o4[i]: continue
        # Above EMA21
        if c4[i] <= ema21[i]: continue

        entry = c4[i]
        sl = min(l4[i], recent_low) - 0.3 * atr14[i]
        sl_dist = entry - sl
        if sl_dist <= 0 or sl_dist > 3 * atr14[i]: continue

        units = (balance * RISK) / sl_dist
        active = {"entry": entry, "ei": i, "sl": sl, "trail": sl,
                  "ts": i + 48, "units": units}

    if active:
        pnl = (c4[-1] - active["entry"]) * active["units"] - FRICTION
        balance += pnl
        trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2)})

    return trades


# ══════════════════════════════════════════════════════════════════════
#  RUN ALL CANDIDATES
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  STRATEGY 5 HUNT — 3 Candidates")
print("=" * 70)

# Test multiple param combos for each candidate
print("\n--- C1: SQUEEZE BREAKOUT ---")
best_c1 = None
for sq_r in [0.7, 0.8, 0.85]:
    for bm in [1.0, 1.3, 1.5]:
        for ta in [3.0, 3.5]:
            for lb in [6, 8, 10]:
                t = run_squeeze(sq_r, bm, ta, lb)
                if len(t) >= 10:
                    pnl = sum(x["pnl"] for x in t)
                    ws = sum(x["pnl"] for x in t if x["pnl"] > 0)
                    ls = abs(sum(x["pnl"] for x in t if x["pnl"] <= 0))
                    pf = ws/ls if ls > 0 else 0
                    if best_c1 is None or pnl > best_c1[1]:
                        best_c1 = (t, pnl, "sq=%.2f bm=%.1f ta=%.1f lb=%d" % (sq_r, bm, ta, lb), pf)

if best_c1:
    print("  Best params: %s (PF=%.2f)" % (best_c1[2], best_c1[3]))
    evaluate(best_c1[0], "SQUEEZE BREAKOUT")
else:
    print("  No valid configs found")

print("\n--- C2: VOLUME RESET ---")
best_c2 = None
for vdr in [0.6, 0.7, 0.8]:
    for vem in [1.2, 1.5, 1.8]:
        for edm in [0.8, 1.0, 1.5, 2.0]:
            for ta in [3.0, 3.5]:
                t = run_vol_reset(vdr, vem, edm, ta)
                if len(t) >= 10:
                    pnl = sum(x["pnl"] for x in t)
                    ws = sum(x["pnl"] for x in t if x["pnl"] > 0)
                    ls = abs(sum(x["pnl"] for x in t if x["pnl"] <= 0))
                    pf = ws/ls if ls > 0 else 0
                    if best_c2 is None or pnl > best_c2[1]:
                        best_c2 = (t, pnl, "vdr=%.1f vem=%.1f edm=%.1f ta=%.1f" % (vdr, vem, edm, ta), pf)

if best_c2:
    print("  Best params: %s (PF=%.2f)" % (best_c2[2], best_c2[3]))
    evaluate(best_c2[0], "VOLUME RESET")
else:
    print("  No valid configs found")

print("\n--- C3: HIGHER LOW BREAK ---")
best_c3 = None
for lb in [8, 10, 12, 15]:
    for mpa in [1.0, 1.5, 2.0]:
        for ta in [3.0, 3.5, 4.0]:
            t = run_higher_low(lb, mpa, ta)
            if len(t) >= 10:
                pnl = sum(x["pnl"] for x in t)
                ws = sum(x["pnl"] for x in t if x["pnl"] > 0)
                ls = abs(sum(x["pnl"] for x in t if x["pnl"] <= 0))
                pf = ws/ls if ls > 0 else 0
                if best_c3 is None or pnl > best_c3[1]:
                    best_c3 = (t, pnl, "lb=%d mpa=%.1f ta=%.1f" % (lb, mpa, ta), pf)

if best_c3:
    print("  Best params: %s (PF=%.2f)" % (best_c3[2], best_c3[3]))
    evaluate(best_c3[0], "HIGHER LOW BREAK")
else:
    print("  No valid configs found")

# ══════════════════════════════════════════════════════════════════════
#  SUMMARY
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  RESULTS SUMMARY")
print("=" * 70)
results = []
if best_c1: results.append(("SQUEEZE BREAKOUT", best_c1[1], best_c1[3], best_c1[2], best_c1[0]))
if best_c2: results.append(("VOLUME RESET", best_c2[1], best_c2[3], best_c2[2], best_c2[0]))
if best_c3: results.append(("HIGHER LOW BREAK", best_c3[1], best_c3[3], best_c3[2], best_c3[0]))

results.sort(key=lambda x: -x[1])
for name, pnl, pf, params, trades in results:
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    print("  %-20s | %3d trades | WR %3.0f%% | $%+6.0f | PF %.2f | %s" % (
        name, n, wins/n*100, pnl, pf, params))

if results:
    winner = results[0]
    print("\n  WINNER: %s → $%+.0f, PF %.2f" % (winner[0], winner[1], winner[2]))
    print("  Params: %s" % winner[3])

print("\nDone.")
