"""
VOLUME RESET — Full Parameter Sweep
Base: +$2,315, 14 trades, PF 3.11, 4/5 checks
Challenge: Need 20+ trades for robustness. Sweep wider params.

Logic: Volume dries up near EMA21 (accumulation by smart money),
then expands on bullish candle (distribution/markup begins).

Sweep: vol_dry_ratio, vol_expand_mult, ema_dist_max, dry_bars,
       dry_count_min, trail_atr, ts_bars, body_filter
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
def atr_f(h, l, c, p=14):
    n = len(h); tr = np.zeros(n)
    for i in range(1, n): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    return pd.Series(tr).rolling(p, min_periods=p).mean().values

def get_month(i):
    if isinstance(df_4h.index, pd.DatetimeIndex): return str(df_4h.index[i])[:7]
    elif "open_time" in df_4h.columns: return str(df_4h["open_time"].iloc[i])[:7]
    return "?"

print("Computing indicators...")
ema21 = ema(c4, 21); ema55 = ema(c4, 55); ema50 = ema(c4, 50)
atr14 = atr_f(h4, l4, c4, 14)

avg_vol = pd.Series(v4).rolling(20, min_periods=20).mean().values
body = np.abs(c4 - o4)
avg_body = pd.Series(body).rolling(20, min_periods=20).mean().values

ema50_slope = np.zeros(n4h)
for i in range(6, n4h):
    if ema50[i-6] > 0:
        ema50_slope[i] = (ema50[i] - ema50[i-6]) / ema50[i-6] * 100

FRICTION = 20; START = 60; RISK = 0.01

def run_volreset(vdr, vem, edm, dry_look, dry_min, trail_atr, ts_bars, body_filt, cd_bars):
    """
    vdr: vol dry ratio (bar vol < vdr * avg_vol counts as "dry")
    vem: vol expand mult (entry bar vol > vem * avg_vol)
    edm: ema distance max (close within edm * ATR of EMA21)
    dry_look: how many bars back to check for dry volume
    dry_min: minimum dry bars required in lookback
    trail_atr: trailing stop multiplier
    ts_bars: time stop in bars
    body_filt: minimum body mult vs avg (0 = no filter)
    cd_bars: cooldown bars after trade
    """
    balance = 10000.0; trades = []; active = None; cooldown = 0

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
            cooldown = i + cd_bars
            trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2), "entry_i": active["ei"]})
            active = None; continue

        if i < cooldown: continue
        if np.isnan(atr14[i]) or atr14[i] <= 0: continue
        if np.isnan(avg_vol[i]) or avg_vol[i] <= 0: continue
        if not (ema21[i] > ema55[i] and ema50_slope[i] > 0.1): continue

        # VOLUME DRY-UP: count low-volume bars in lookback
        dry_count = 0
        for j in range(1, dry_look + 1):
            if i - j >= 0 and v4[i-j] < vdr * avg_vol[i]:
                dry_count += 1
        if dry_count < dry_min: continue

        # VOLUME EXPANSION: current bar has above-threshold volume
        if v4[i] < vem * avg_vol[i]: continue

        # Price near EMA21
        dist_to_ema = abs(c4[i] - ema21[i])
        if dist_to_ema > edm * atr14[i]: continue

        # Bullish candle, close above EMA21
        if c4[i] <= o4[i]: continue
        if c4[i] <= ema21[i]: continue

        # Optional body filter
        if body_filt > 0 and body[i] < body_filt * avg_body[i]: continue

        entry = c4[i]
        sl = entry - 1.5 * atr14[i]
        sl_dist = entry - sl
        if sl_dist <= 0: continue

        units = (balance * RISK) / sl_dist
        active = {"entry": entry, "ei": i, "sl": sl, "trail": sl,
                  "ts": i + ts_bars, "units": units}

    if active:
        pnl = (c4[-1] - active["entry"]) * active["units"] - FRICTION
        balance += pnl
        trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2), "entry_i": active["ei"]})

    n = len(trades)
    if n < 8: return None

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
    if n >= 15: score += 1
    if n >= 20: score += 2
    if n >= 35: score += 1
    if pf > 1.2: score += 1
    if pf > 1.5: score += 1
    if pf > 2.0: score += 1
    if rdd > 0.5: score += 1
    if rdd > 1.0: score += 1
    if wm > tm * 0.4: score += 1

    return {"n": n, "pnl": pnl_t, "wr": wr, "ev": ev, "pf": pf, "mdd": mdd, "rdd": rdd,
            "conc": conc, "wf": wf, "h1": h1, "h2": h2, "wm": wm, "tm": tm,
            "balance": balance, "score": score,
            "params": (vdr, vem, edm, dry_look, dry_min, trail_atr, ts_bars, body_filt, cd_bars)}

# ══════════════════════════════════════════════════════════════════════
#  GRID — Wide sweep to find robust configs with 20+ trades
# ══════════════════════════════════════════════════════════════════════
vdr_list = [0.7, 0.8, 0.9]         # vol dry threshold
vem_list = [1.0, 1.2, 1.5]         # vol expansion threshold
edm_list = [0.8, 1.0, 1.5, 2.0]   # EMA distance max
dry_look_list = [3, 4, 5]          # lookback for dry bars
dry_min_list = [1, 2]              # min dry bars required
trail_list = [2.5, 3.0, 3.5, 4.0] # trailing stop ATR mult
ts_list = [36, 48, 60]            # time stop bars
body_list = [0, 0.8, 1.0]         # body filter (0=none)
cd_list = [2, 3]                   # cooldown bars

total = (len(vdr_list) * len(vem_list) * len(edm_list) * len(dry_look_list) *
         len(dry_min_list) * len(trail_list) * len(ts_list) * len(body_list) * len(cd_list))
print("\nRunning %d configs..." % total)

results = []; count = 0
for vdr, vem, edm, dl, dm, ta, ts, bf, cd in product(
        vdr_list, vem_list, edm_list, dry_look_list, dry_min_list,
        trail_list, ts_list, body_list, cd_list):
    count += 1
    r = run_volreset(vdr, vem, edm, dl, dm, ta, ts, bf, cd)
    if r: results.append(r)
    if count % 500 == 0:
        print("  %d/%d... (%d valid)" % (count, total, len(results)))

print("Done. %d valid / %d total\n" % (len(results), total))

profitable = [r for r in results if r["pnl"] > 0]
full_pass = [r for r in results if r["pnl"] > 0 and r["conc"] and r["wf"] and r["n"] >= 15 and r["pf"] > 1.2]

print("=" * 95)
print("  VOLUME RESET SWEEP")
print("=" * 95)
print("  Valid: %d | Profitable: %d (%.0f%%) | Full pass: %d (%.0f%%)" % (
    len(results), len(profitable), len(profitable)/len(results)*100 if results else 0,
    len(full_pass), len(full_pass)/len(results)*100 if results else 0))

results.sort(key=lambda x: (-x["score"], -x["pnl"]))

print("\n  TOP 20:")
print("  %-65s | %-4s | %-3s | %-8s | %-4s | %-3s | %-3s | %-5s" % (
    "Params","Trds","WR","PnL","PF","C?","WF","Score"))
print("  " + "-" * 100)
for r in results[:20]:
    p = r["params"]
    print("  vdr=%.1f vem=%.1f edm=%.1f dl=%d dm=%d ta=%.1f ts=%d bf=%.1f cd=%d | %3d | %2.0f%% | $%+6.0f | %.2f | %s | %s | %d" % (
        p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8],
        r["n"], r["wr"]*100, r["pnl"], r["pf"],
        "Y" if r["conc"] else "N", "Y" if r["wf"] else "N", r["score"]))

if full_pass:
    # Sort by score then PnL
    full_pass.sort(key=lambda x: (-x["score"], -x["pnl"]))
    best = full_pass[0]
    p = best["params"]
    print("\n  BEST FULL PASS:")
    print("  vdr=%.1f vem=%.1f edm=%.1f dl=%d dm=%d ta=%.1f ts=%d bf=%.1f cd=%d" % p)
    print("  %d trades | WR %.0f%% | $%+.0f | PF %.2f | MDD $%.0f | R/DD %.2f" % (
        best["n"], best["wr"]*100, best["pnl"], best["pf"], best["mdd"], best["rdd"]))
    print("  WF: H1=$%+.0f H2=$%+.0f | Win months: %d/%d" % (best["h1"], best["h2"], best["wm"], best["tm"]))

    nb_count = 0; nb_profit = 0
    for r in results:
        diffs = sum(1 for a, b in zip(p, r["params"]) if a != b)
        if diffs == 1: nb_count += 1; nb_profit += 1 if r["pnl"] > 0 else 0
    print("  Neighbors: %d/%d profitable (%.0f%%)" % (nb_profit, nb_count, nb_profit/nb_count*100 if nb_count else 0))

    # Also show best with 20+ trades
    pass20 = [r for r in full_pass if r["n"] >= 20]
    if pass20:
        pass20.sort(key=lambda x: (-x["score"], -x["pnl"]))
        best20 = pass20[0]
        p20 = best20["params"]
        print("\n  BEST WITH 20+ TRADES:")
        print("  vdr=%.1f vem=%.1f edm=%.1f dl=%d dm=%d ta=%.1f ts=%d bf=%.1f cd=%d" % p20)
        print("  %d trades | WR %.0f%% | $%+.0f | PF %.2f | MDD $%.0f | R/DD %.2f" % (
            best20["n"], best20["wr"]*100, best20["pnl"], best20["pf"], best20["mdd"], best20["rdd"]))
        print("  WF: H1=$%+.0f H2=$%+.0f | Win months: %d/%d" % (best20["h1"], best20["h2"], best20["wm"], best20["tm"]))

        nb_count2 = 0; nb_profit2 = 0
        for r in results:
            diffs = sum(1 for a, b in zip(p20, r["params"]) if a != b)
            if diffs == 1: nb_count2 += 1; nb_profit2 += 1 if r["pnl"] > 0 else 0
        print("  Neighbors: %d/%d profitable (%.0f%%)" % (nb_profit2, nb_count2, nb_profit2/nb_count2*100 if nb_count2 else 0))

print("\nDone.")
