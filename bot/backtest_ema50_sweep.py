"""
EMA50 DEEP PULLBACK — Parameter Sweep
Base: +$2,273, 42 trades, PF 1.73, 5/5
Sweep: pullback distance, trail, sl, bounce requirement, time stop
"""
import sys
sys.path.insert(0, "/home/ubuntu/bot")
import market_data
import numpy as np
import pandas as pd
from itertools import product

print("Downloading data...")
df_4h = market_data.download("BTCUSDT", "4h", total_candles=5000)
n4h = len(df_4h)
print("4h: %d candles" % n4h)

h4, l4, c4, o4, v4 = [df_4h[c].values.astype(float) for c in ["high","low","close","open","volume"]]

def ema(a, p): return pd.Series(a).ewm(span=p, adjust=False).mean().values
def sma(a, p): return pd.Series(a).rolling(p, min_periods=p).mean().values
def atr_f(h, l, c, p=14):
    n = len(h); tr = np.zeros(n)
    for i in range(1, n): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    return pd.Series(tr).rolling(p, min_periods=p).mean().values

def get_month(i):
    if isinstance(df_4h.index, pd.DatetimeIndex): return str(df_4h.index[i])[:7]
    elif "open_time" in df_4h.columns: return str(df_4h["open_time"].iloc[i])[:7]
    return "?"

print("Computing indicators...")
ema21 = ema(c4, 21); ema55 = ema(c4, 55)
ema_arr = {}
for p in [40, 45, 50, 55]:
    ema_arr[p] = ema(c4, p)
atr14 = atr_f(h4, l4, c4, 14)

ema50_base = ema(c4, 50)
ema50_slope = np.zeros(n4h)
for i in range(6, n4h):
    if ema50_base[i-6] > 0: ema50_slope[i] = (ema50_base[i] - ema50_base[i-6]) / ema50_base[i-6] * 100

FRICTION = 20; START = 60; RISK = 0.01

def run_pullback(ema_p, max_below, max_above, trail_atr, sl_atr, ts_bars, cd_bars):
    balance = 10000.0; trades = []; active = None; cooldown = 0
    ema_line = ema_arr[ema_p]

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
            cooldown = i + cd_bars
            trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2), "entry_i": active["ei"]})
            active = None; continue

        if i < cooldown: continue
        if np.isnan(atr14[i]) or atr14[i] <= 0: continue
        if not (ema21[i] > ema55[i] and ema50_slope[i] > 0.1): continue

        dist = l4[i] - ema_line[i]
        if dist < -max_below * atr14[i]: continue
        if dist > max_above * atr14[i]: continue
        # Must have crossed below EMA21
        if l4[i] > ema21[i]: continue
        if c4[i] <= o4[i]: continue
        if c4[i] <= ema21[i]: continue

        entry = c4[i]
        sl = min(l4[i], ema_line[i]) - 0.3 * atr14[i]
        sl_dist = entry - sl
        if sl_dist <= 0 or sl_dist > 3 * atr14[i]: continue

        units = (balance * RISK) / sl_dist
        active = {"entry": entry, "ei": i, "sl": sl, "trail": sl,
                  "ts": i + ts_bars, "units": units}

    if active:
        pnl = (c4[-1] - active["entry"]) * active["units"] - FRICTION
        balance += pnl
        trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2), "entry_i": active["ei"]})

    n = len(trades)
    if n < 10: return None

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
    if pnl_t > 0: score += 2
    if conc: score += 3
    if wf: score += 3
    if n >= 20: score += 2
    if n >= 35: score += 1
    if pf > 1.2: score += 1
    if pf > 1.5: score += 1
    if rdd > 0.5: score += 1
    if rdd > 1.0: score += 1
    if wm > tm * 0.4: score += 1

    return {"n": n, "pnl": pnl_t, "wr": wr, "ev": ev, "pf": pf, "mdd": mdd, "rdd": rdd,
            "conc": conc, "wf": wf, "h1": h1, "h2": h2, "wm": wm, "tm": tm,
            "balance": balance, "score": score,
            "params": (ema_p, max_below, max_above, trail_atr, sl_atr, ts_bars, cd_bars)}

# GRID
ema_ps = [40, 45, 50, 55]
max_belows = [0.3, 0.5, 0.8]
max_aboves = [0.5, 0.8, 1.0, 1.2]
trail_atrs = [2.5, 3.0, 3.5, 4.0]
sl_atrs = [1.5]  # keep fixed (proven)
ts_list = [36, 48, 60]
cd_list = [3, 4]

total = len(ema_ps)*len(max_belows)*len(max_aboves)*len(trail_atrs)*len(sl_atrs)*len(ts_list)*len(cd_list)
print("\nRunning %d configs..." % total)

results = []; count = 0
for ep, mb, ma, ta, sa, ts, cd in product(ema_ps, max_belows, max_aboves, trail_atrs, sl_atrs, ts_list, cd_list):
    count += 1
    r = run_pullback(ep, mb, ma, ta, sa, ts, cd)
    if r: results.append(r)
    if count % 200 == 0: print("  %d/%d... (%d valid)" % (count, total, len(results)))

print("Done. %d valid / %d total\n" % (len(results), total))

profitable = [r for r in results if r["pnl"] > 0]
full_pass = [r for r in results if r["pnl"] > 0 and r["conc"] and r["wf"] and r["n"] >= 20 and r["pf"] > 1.2]

print("=" * 90)
print("  EMA50 PULLBACK SWEEP")
print("=" * 90)
print("  Valid: %d | Profitable: %d (%.0f%%) | Full pass: %d (%.0f%%)" % (
    len(results), len(profitable), len(profitable)/len(results)*100 if results else 0,
    len(full_pass), len(full_pass)/len(results)*100 if results else 0))

results.sort(key=lambda x: (-x["score"], -x["pnl"]))

print("\n  TOP 15:")
print("  %-50s | %-4s | %-4s | %-8s | %-4s | %-3s | %-3s | %-5s" % (
    "Params","Trds","WR","PnL","PF","C?","WF","Score"))
print("  " + "-" * 90)
for r in results[:15]:
    p = r["params"]
    print("  ema=%d mb=%.1f ma=%.1f ta=%.1f sa=%.1f ts=%d cd=%d | %3d | %3.0f%% | $%+6.0f | %.2f | %s | %s | %d" % (
        p[0], p[1], p[2], p[3], p[4], p[5], p[6],
        r["n"], r["wr"]*100, r["pnl"], r["pf"],
        "Y" if r["conc"] else "N", "Y" if r["wf"] else "N", r["score"]))

if full_pass:
    full_pass.sort(key=lambda x: -x["pnl"])
    best = full_pass[0]
    p = best["params"]
    print("\n  BEST: ema=%d, below=%.1f, above=%.1f, trail=%.1f, sl=%.1f, ts=%d, cd=%d" % p)
    print("  %d trades | WR %.0f%% | $%+.0f | PF %.2f | MDD $%.0f | R/DD %.2f" % (
        best["n"], best["wr"]*100, best["pnl"], best["pf"], best["mdd"], best["rdd"]))
    print("  WF: H1=$%+.0f H2=$%+.0f | Win months: %d/%d" % (best["h1"], best["h2"], best["wm"], best["tm"]))

    nb_count = 0; nb_profit = 0
    for r in results:
        diffs = sum(1 for a, b in zip(p, r["params"]) if a != b)
        if diffs == 1: nb_count += 1; nb_profit += 1 if r["pnl"] > 0 else 0
    print("  Neighbors: %d/%d profitable (%.0f%%)" % (nb_profit, nb_count, nb_profit/nb_count*100 if nb_count else 0))

print("\nDone.")
