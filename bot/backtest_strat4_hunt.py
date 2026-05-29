"""
STRATEGY 4 HUNT — Find another edge different from existing 3
==============================================================
Current: Breakout (highs) + Vol Momentum (impulse) + Panic Dip (bounce)
Missing entries that trigger at DIFFERENT times:

S1: INSIDE BAR BREAKOUT
    4H bar completely inside previous bar (consolidation)
    → next bar breaks above high = compressed energy releasing
    Low risk entry because SL goes below the inside bar low

S2: EMA50 DEEP PULLBACK
    Price pulls all the way back to EMA50 in uptrend (not just EMA21)
    Deeper pullback = bigger bounce potential
    Different from panic dip: this is SLOW pullback, not crash

S3: ACCUMULATION BREAKOUT
    5+ bars of low volume (below 0.7x avg) = market resting
    Then ONE bar with close above the range high + volume spike
    Quiet before the storm pattern

All long-only, 4H, same proven formula (trail + tight SL)
"""
import sys
sys.path.insert(0, "/home/ubuntu/bot")
import market_data
import numpy as np
import pandas as pd

print("=" * 60)
print("  STRATEGY 4 HUNT")
print("=" * 60)

print("\nDownloading data...")
df_4h = market_data.download("BTCUSDT", "4h", total_candles=5000)
df_1d = market_data.download("BTCUSDT", "1d", total_candles=1000)

n4h = len(df_4h); n1d = len(df_1d)
print("4h: %d candles (%.0f days)" % (n4h, n4h / 6))

h4, l4, c4, o4, v4 = [df_4h[c].values.astype(float) for c in ["high","low","close","open","volume"]]

def get_month(i):
    if isinstance(df_4h.index, pd.DatetimeIndex):
        return str(df_4h.index[i])[:7]
    elif "open_time" in df_4h.columns:
        return str(df_4h["open_time"].iloc[i])[:7]
    return "?"

def ema(a, p): return pd.Series(a).ewm(span=p, adjust=False).mean().values
def sma(a, p): return pd.Series(a).rolling(p, min_periods=p).mean().values

def atr_f(h, l, c, p=14):
    n = len(h); tr = np.zeros(n)
    for i in range(1, n): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    return pd.Series(tr).rolling(p, min_periods=p).mean().values

print("Computing indicators...")
ema21 = ema(c4, 21); ema50 = ema(c4, 50); ema55 = ema(c4, 55)
atr14 = atr_f(h4, l4, c4, 14)
avg_vol = sma(v4, 20)
body = np.abs(c4 - o4)
avg_body = sma(body, 20)

ema50_slope = np.zeros(n4h)
for i in range(6, n4h):
    if ema50[i-6] > 0: ema50_slope[i] = (ema50[i] - ema50[i-6]) / ema50[i-6] * 100

FRICTION = 20; START = 60; RISK = 0.01

def is_uptrend(i):
    return ema21[i] > ema55[i] and ema50_slope[i] > 0.1


# ======================================================================
#  S1: INSIDE BAR BREAKOUT
#  Bar i-1 is inside bar (high < prev high, low > prev low)
#  Bar i breaks above high of the inside bar + bullish
# ======================================================================
def strat1_inside_bar():
    balance = 10000.0; peak = balance; trades = []; active = None; cooldown = 0

    for i in range(START, n4h):
        if active is not None:
            new_t = c4[i] - active["ta"] * atr14[i]
            if not np.isnan(new_t) and new_t > active["trail"]: active["trail"] = new_t

            if l4[i] <= active["sl"]:
                pnl = (active["sl"] - active["entry"]) * active["units"] - FRICTION; res = "SL"
            elif active["trail"] > active["entry"] and l4[i] <= active["trail"]:
                pnl = (active["trail"] - active["entry"]) * active["units"] - FRICTION; res = "TRAIL"
            elif i >= active["ts"]:
                pnl = (c4[i] - active["entry"]) * active["units"] - FRICTION; res = "TIME"
            else: continue

            if balance > peak: peak = balance
            cooldown = i + 3
            trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2),
                "result": res, "bars": i - active["ei"], "entry_i": active["ei"]})
            active = None; continue

        if i < cooldown: continue
        if np.isnan(atr14[i]) or atr14[i] <= 0: continue
        if not is_uptrend(i): continue

        # Check if bar i-1 was an inside bar
        if i < 2: continue
        ib_high = h4[i-1]; ib_low = l4[i-1]
        prev_high = h4[i-2]; prev_low = l4[i-2]

        # Inside bar: high lower, low higher than previous
        if not (ib_high < prev_high and ib_low > prev_low): continue

        # Current bar breaks above inside bar high
        if c4[i] <= ib_high: continue

        # Bullish close
        if c4[i] <= o4[i]: continue

        # Close above EMA21
        if c4[i] <= ema21[i]: continue

        entry = c4[i]
        sl = ib_low - 0.3 * atr14[i]  # SL below inside bar low
        sl_dist = entry - sl
        if sl_dist <= 0 or sl_dist > 3 * atr14[i]: continue

        units = (balance * RISK) / sl_dist
        active = {"entry": entry, "ei": i, "sl": sl, "trail": sl,
                  "ts": i + 48, "units": units, "ta": 3.0}

    if active:
        pnl = (c4[-1] - active["entry"]) * active["units"] - FRICTION
        balance += pnl
        trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2),
            "result": "OPEN", "bars": n4h-1-active["ei"], "entry_i": active["ei"]})
    return trades, balance, peak


# ======================================================================
#  S2: EMA50 DEEP PULLBACK
#  Price pulls back to EMA50 zone (within 0.8 ATR) in uptrend
#  Then bounces with green candle above EMA21
#  Different from dip: slow pullback, not crash
# ======================================================================
def strat2_ema50_pullback():
    balance = 10000.0; peak = balance; trades = []; active = None; cooldown = 0

    for i in range(START, n4h):
        if active is not None:
            new_t = c4[i] - active["ta"] * atr14[i]
            if not np.isnan(new_t) and new_t > active["trail"]: active["trail"] = new_t

            if l4[i] <= active["sl"]:
                pnl = (active["sl"] - active["entry"]) * active["units"] - FRICTION; res = "SL"
            elif active["trail"] > active["entry"] and l4[i] <= active["trail"]:
                pnl = (active["trail"] - active["entry"]) * active["units"] - FRICTION; res = "TRAIL"
            elif i >= active["ts"]:
                pnl = (c4[i] - active["entry"]) * active["units"] - FRICTION; res = "TIME"
            else: continue

            if balance > peak: peak = balance
            cooldown = i + 3
            trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2),
                "result": res, "bars": i - active["ei"], "entry_i": active["ei"]})
            active = None; continue

        if i < cooldown: continue
        if np.isnan(atr14[i]) or atr14[i] <= 0: continue
        if not is_uptrend(i): continue

        # Deep pullback: low within 0.8 ATR of EMA50
        dist = l4[i] - ema50[i]
        if dist < -0.5 * atr14[i]: continue   # crashed below EMA50
        if dist > 0.8 * atr14[i]: continue     # didn't pull back enough

        # But NOT close to EMA21 (that would be shallow pullback, covered by other strats)
        dist21 = l4[i] - ema21[i]
        if dist21 > 0: continue  # didn't even reach EMA21, too shallow

        # Bounce: green candle closing above EMA21
        if c4[i] <= o4[i]: continue
        if c4[i] <= ema21[i]: continue

        entry = c4[i]
        sl = min(l4[i], ema50[i]) - 0.3 * atr14[i]
        sl_dist = entry - sl
        if sl_dist <= 0 or sl_dist > 3 * atr14[i]: continue

        units = (balance * RISK) / sl_dist
        active = {"entry": entry, "ei": i, "sl": sl, "trail": sl,
                  "ts": i + 48, "units": units, "ta": 3.5}

    if active:
        pnl = (c4[-1] - active["entry"]) * active["units"] - FRICTION
        balance += pnl
        trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2),
            "result": "OPEN", "bars": n4h-1-active["ei"], "entry_i": active["ei"]})
    return trades, balance, peak


# ======================================================================
#  S3: ACCUMULATION BREAKOUT
#  5+ consecutive bars with volume below 0.7x avg (quiet zone)
#  Then: bar with close above the highest high of quiet zone + vol > 1.5x
# ======================================================================
def strat3_accumulation():
    balance = 10000.0; peak = balance; trades = []; active = None; cooldown = 0

    for i in range(START, n4h):
        if active is not None:
            new_t = c4[i] - active["ta"] * atr14[i]
            if not np.isnan(new_t) and new_t > active["trail"]: active["trail"] = new_t

            if l4[i] <= active["sl"]:
                pnl = (active["sl"] - active["entry"]) * active["units"] - FRICTION; res = "SL"
            elif active["trail"] > active["entry"] and l4[i] <= active["trail"]:
                pnl = (active["trail"] - active["entry"]) * active["units"] - FRICTION; res = "TRAIL"
            elif i >= active["ts"]:
                pnl = (c4[i] - active["entry"]) * active["units"] - FRICTION; res = "TIME"
            else: continue

            if balance > peak: peak = balance
            cooldown = i + 4
            trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2),
                "result": res, "bars": i - active["ei"], "entry_i": active["ei"]})
            active = None; continue

        if i < cooldown: continue
        if np.isnan(atr14[i]) or atr14[i] <= 0: continue
        if np.isnan(avg_vol[i]) or avg_vol[i] <= 0: continue
        if not is_uptrend(i): continue

        # Check for 5+ quiet bars before this one
        quiet_count = 0
        for j in range(1, 12):
            if i - j < 0: break
            if np.isnan(avg_vol[i-j]) or avg_vol[i-j] <= 0: break
            if v4[i-j] < avg_vol[i-j] * 0.7:
                quiet_count += 1
            else:
                break  # must be consecutive

        if quiet_count < 5: continue

        # Range high of quiet zone
        range_high = max(h4[i-quiet_count:i])

        # Current bar breaks above range + volume expansion
        if c4[i] <= range_high: continue
        if v4[i] < avg_vol[i] * 1.5: continue
        if c4[i] <= o4[i]: continue  # bullish

        entry = c4[i]
        sl = entry - 1.5 * atr14[i]
        sl_dist = entry - sl
        if sl_dist <= 0: continue

        units = (balance * RISK) / sl_dist
        active = {"entry": entry, "ei": i, "sl": sl, "trail": sl,
                  "ts": i + 48, "units": units, "ta": 3.0}

    if active:
        pnl = (c4[-1] - active["entry"]) * active["units"] - FRICTION
        balance += pnl
        trades.append({"month": get_month(active["ei"]), "pnl": round(pnl, 2),
            "result": "OPEN", "bars": n4h-1-active["ei"], "entry_i": active["ei"]})
    return trades, balance, peak


# ======================================================================
#  ANALYZE
# ======================================================================
def analyze(name, trades, balance, peak):
    n = len(trades)
    print("\n" + "=" * 80)
    print("  %s" % name)
    print("=" * 80)
    if n == 0:
        print("  NO TRADES"); return {"name": name, "n": 0, "pnl": 0, "pass": False}

    pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = wins / n; ev = pnl / n

    eq = [10000.0]
    for t in trades: eq.append(eq[-1] + t["pnl"])
    ea = np.array(eq)
    mdd = abs(np.min(ea - np.maximum.accumulate(ea)))
    rdd = pnl / mdd if mdd > 0 else 0

    ws = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    ls = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = ws / ls if ls > 0 else 0
    avg_win = ws / wins if wins > 0 else 0
    avg_loss = ls / (n - wins) if (n - wins) > 0 else 0

    print("  Trades: %d | WR: %.1f%% | PnL: $%+.0f | EV: $%+.1f/trade" % (n, wr*100, pnl, ev))
    print("  PF: %.2f | MaxDD: $%.0f (%.1f%%) | Return/DD: %.2f" % (pf, mdd, mdd/100, rdd))
    print("  Avg win: $%.0f | Avg loss: $%.0f | W:L: %.1f:1" % (avg_win, avg_loss, avg_win/avg_loss if avg_loss > 0 else 0))
    print("  Final balance: $%.0f (%+.1f%%)" % (balance, (balance-10000)/100))

    res = {}
    for t in trades: res[t["result"]] = res.get(t["result"], 0) + 1
    print("  Exits: %s" % " | ".join("%s=%d" % (k,v) for k,v in sorted(res.items())))

    monthly = {}
    for t in trades:
        m = t["month"]; monthly.setdefault(m, []).append(t)

    print("\n  %-8s | %-4s | %-4s | %-10s | %-10s" % ("Month","Trds","WR","PnL","Cum"))
    cum = 0; wm = 0
    for m in sorted(monthly.keys()):
        mt = monthly[m]; mw = sum(1 for x in mt if x["pnl"] > 0)
        mp = sum(x["pnl"] for x in mt); cum += mp
        if mp > 0: wm += 1
        print("  %-8s | %3d  | %3.0f%% | $%+8.0f | $%+8.0f" % (
            m, len(mt), mw/len(mt)*100 if mt else 0, mp, cum))
    tm = len(monthly)
    print("  Win months: %d/%d (%.0f%%)" % (wm, tm, wm/tm*100 if tm else 0))

    checks = 0
    print("\n  --- ROBUSTNESS ---")
    mpnl = {m: sum(t["pnl"] for t in mt) for m, mt in monthly.items()}
    bm = max(mpnl.values()) if mpnl else 0
    conc = (pnl - bm) > 0
    if conc: checks += 1
    print("  [%s] Concentration: Remove best ($%+.0f) -> $%+.0f" % ("PASS" if conc else "FAIL", bm, pnl-bm))

    half = n // 2
    h1p = sum(t["pnl"] for t in trades[:half])
    h2p = sum(t["pnl"] for t in trades[half:])
    wf = h1p > 0 and h2p > 0
    if wf: checks += 1
    print("  [%s] Walk-forward: H1=$%+.0f | H2=$%+.0f" % ("PASS" if wf else "FAIL", h1p, h2p))

    ss = n >= 20;
    if ss: checks += 1
    print("  [%s] Sample: %d (need 20+)" % ("PASS" if ss else "FAIL", n))
    pf_ok = pf > 1.2;
    if pf_ok: checks += 1
    print("  [%s] PF: %.2f (need >1.2)" % ("PASS" if pf_ok else "FAIL", pf))
    ev_ok = ev > 0;
    if ev_ok: checks += 1
    print("  [%s] EV: $%+.1f" % ("PASS" if ev_ok else "FAIL", ev))

    passed = checks >= 4 and pnl > 0
    print("\n  Checks: %d/5 >>> %s <<<" % (checks, "APPROVED" if passed else "NOT APPROVED"))
    return {"name": name, "n": n, "pnl": pnl, "wr": wr, "ev": ev, "pf": pf,
            "mdd": mdd, "rdd": rdd, "pass": passed, "checks": checks,
            "conc": conc, "wf": wf, "trades": trades}


# RUN
print("\n" + "-" * 60)
print("Running S1: Inside Bar Breakout...")
t1, b1, p1 = strat1_inside_bar()
print("  %d trades, $%.0f" % (len(t1), b1))

print("Running S2: EMA50 Deep Pullback...")
t2, b2, p2 = strat2_ema50_pullback()
print("  %d trades, $%.0f" % (len(t2), b2))

print("Running S3: Accumulation Breakout...")
t3, b3, p3 = strat3_accumulation()
print("  %d trades, $%.0f" % (len(t3), b3))

r1 = analyze("S1: INSIDE BAR BREAKOUT", t1, b1, p1)
r2 = analyze("S2: EMA50 DEEP PULLBACK", t2, b2, p2)
r3 = analyze("S3: ACCUMULATION BREAKOUT (quiet zone + expansion)", t3, b3, p3)

# SCOREBOARD
print("\n\n" + "=" * 95)
print("  STRATEGY 4 HUNT — SCOREBOARD")
print("=" * 95)
print("%-55s | %-4s | %-4s | %-10s | %-6s | %-4s | %-4s | %-4s" % (
    "Strategy","Trds","WR","PnL","PF","C?","WF?","Chk"))
print("-" * 95)
for r in [r1, r2, r3]:
    if r["n"] == 0:
        print("%-55s | %3d  |  -   |     -      |   -  |  -  |  -  |  - " % (r["name"][:55], 0))
    else:
        print("%-55s | %3d  | %3.0f%% | $%+8.0f | %4.2f | %s  | %s  | %d/5" % (
            r["name"][:55], r["n"], r["wr"]*100, r["pnl"], r["pf"],
            "Y" if r.get("conc") else "N", "Y" if r.get("wf") else "N", r["checks"]))

winners = [r for r in [r1, r2, r3] if r["pass"]]
if winners:
    best = max(winners, key=lambda x: x["pnl"])
    print("\n  WINNER: %s" % best["name"])
    print("  $%+.0f | %d trades | PF %.2f | %d/5 checks" % (best["pnl"], best["n"], best["pf"], best["checks"]))
    print("  EXISTING 3 STRATEGIES: ~$11,500")
    print("  + NEW S4: $%+.0f" % best["pnl"])
    print("  ESTIMATED 4-STRATEGY TOTAL: ~$%+.0f" % (11500 + best["pnl"]))
else:
    print("\n  No winners. Closest:")
    best = max([r1,r2,r3], key=lambda x: x.get("checks",0)*10000 + x.get("pnl",-99999))
    print("  %s: %d/5, $%+.0f" % (best["name"], best.get("checks",0), best.get("pnl",0)))

print("\nDone.")
