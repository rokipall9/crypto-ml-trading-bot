"""
SMART TIMING TEST — "Better decision of WHEN to trade"
========================================================
Teacher's insight: you don't need more strategies, you need
better timing of WHEN to activate them.

Tests 5 modes:
  A: CURRENT    — uptrend filter only (baseline)
  B: ADX-GATE   — uptrend + ADX>20 (confirmed trend)
  C: ADX-STRICT  — uptrend + ADX>25 (strong trend only)
  D: BULL-ONLY   — longs in BULL, OFF in bear/chop, no shorts
  E: BULL+SHORT  — longs in BULL, 1 short in BEAR, OFF in chop

Which mode gives the best CONSISTENCY (not just most profit)?
"""
import sys, warnings
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, "/home/ubuntu/bot")
import market_data

print("=" * 70)
print("  SMART TIMING: When to trade > What to trade")
print("=" * 70)

# ═══════════════════════════════════════════════════════════════
#  DATA + INDICATORS
# ═══════════════════════════════════════════════════════════════
print("\n[1/4] Loading data...")
df = market_data.download("BTCUSDT", "4h", total_candles=5000)
NN = len(df)

dates = df["open_time"].values if "open_time" in df.columns else [None]*NN
def gd(i):
    if i < len(dates) and dates[i] is not None: return str(dates[i])[:10]
    return "bar_%d" % i

H = df["high"].values.astype(float)
L = df["low"].values.astype(float)
C = df["close"].values.astype(float)
O = df["open"].values.astype(float)
V = df["volume"].values.astype(float)

def ema(a, p): return pd.Series(a).ewm(span=p, adjust=False).mean().values
def calc_atr(h, l, c, p=14):
    n = len(h); tr = np.zeros(n)
    for i in range(1, n): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    return pd.Series(tr).rolling(p, min_periods=p).mean().values

e21 = ema(C, 21); e30 = ema(C, 30); e40 = ema(C, 40)
e50 = ema(C, 50); e55 = ema(C, 55)
ATR = calc_atr(H, L, C, 14)
vol20 = pd.Series(V).rolling(20, min_periods=20).mean().values
bd = np.abs(C - O)
bd20 = pd.Series(bd).rolling(20, min_periods=20).mean().values
hi30 = pd.Series(H).rolling(30, min_periods=30).max().values
hi8 = pd.Series(H).rolling(8, min_periods=8).max().values

slp = np.zeros(NN)
for i in range(6, NN):
    if e50[i-6] > 0: slp[i] = (e50[i] - e50[i-6]) / e50[i-6] * 100

# ADX
def calc_adx(h, l, c, p=14):
    n = len(h); pdm = np.zeros(n); mdm = np.zeros(n); tr = np.zeros(n)
    for i in range(1, n):
        u = h[i]-h[i-1]; d = l[i-1]-l[i]
        if u > d and u > 0: pdm[i] = u
        if d > u and d > 0: mdm[i] = d
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    atr_s = pd.Series(tr).ewm(span=p, adjust=False).mean().values
    sp = pd.Series(pdm).ewm(span=p, adjust=False).mean().values
    sm = pd.Series(mdm).ewm(span=p, adjust=False).mean().values
    pdi = np.where(atr_s > 0, sp/atr_s*100, 0)
    mdi = np.where(atr_s > 0, sm/atr_s*100, 0)
    dx = np.where(pdi+mdi > 0, np.abs(pdi-mdi)/(pdi+mdi)*100, 0)
    return pd.Series(dx).ewm(span=p, adjust=False).mean().values

ADX = calc_adx(H, L, C, 14)
ema_map = {21: e21, 30: e30, 40: e40, 50: e50, 55: e55}

ST = 60; FR = 20; RSK = 0.01

def is_up(i): return e21[i] > e55[i] and slp[i] > 0.1
def is_dn(i): return e21[i] < e55[i] and slp[i] < -0.1

print("  %d candles: %s to %s" % (NN, gd(0), gd(NN-1)))

# ═══════════════════════════════════════════════════════════════
#  5 LONG STRATEGIES (unchanged)
# ═══════════════════════════════════════════════════════════════

def chk_brk(i):
    if np.isnan(hi30[i]) or np.isnan(vol20[i]) or vol20[i] <= 0: return None
    if C[i] <= hi30[i-1] or V[i] < vol20[i]*1.5 or C[i] <= O[i]: return None
    return {"s": "BRK", "e": C[i], "sl": C[i]-1.5*ATR[i], "ta": 3.0, "ts": 48}

def chk_mom(i):
    if np.isnan(bd20[i]) or bd20[i] <= 0 or np.isnan(vol20[i]) or vol20[i] <= 0: return None
    if C[i] <= O[i] or bd[i] < bd20[i]*2 or V[i] < vol20[i]*2 or C[i] <= e21[i]: return None
    return {"s": "MOM", "e": C[i], "sl": C[i]-1.5*ATR[i], "ta": 3.0, "ts": 48}

def chk_dip(i):
    if np.isnan(hi8[i]) or hi8[i] <= 0 or np.isnan(bd20[i]) or bd20[i] <= 0: return None
    dp = (hi8[i]-L[i])/hi8[i]*100
    if dp < 3 or C[i] <= O[i] or bd[i] < bd20[i]*1.5 or C[i] < e55[i]: return None
    return {"s": "DIP", "e": C[i], "sl": C[i]-1.5*ATR[i], "ta": 3.5, "ts": 48}

def chk_plb(i):
    a = ATR[i]
    if np.isnan(a) or a <= 0 or np.isnan(e40[i]): return None
    d = L[i]-e40[i]
    if d < -0.3*a or d > 1.2*a or L[i] > e21[i] or C[i] <= O[i] or C[i] <= e21[i]: return None
    sl = min(L[i], e40[i])-0.3*a
    if C[i]-sl <= 0 or C[i]-sl > 3*a: return None
    return {"s": "PLB", "e": C[i], "sl": sl, "ta": 3.5, "ts": 48}

def chk_vrs(i):
    a = ATR[i]
    if np.isnan(a) or a <= 0 or np.isnan(vol20[i]) or vol20[i] <= 0: return None
    dc = sum(1 for j in range(1, 4) if i-j >= 0 and V[i-j] < 0.8*vol20[i])
    if dc < 1 or V[i] < 1.2*vol20[i] or abs(C[i]-e21[i]) > 0.8*a: return None
    if C[i] <= O[i] or C[i] <= e21[i]: return None
    return {"s": "VRS", "e": C[i], "sl": C[i]-1.5*a, "ta": 4.0, "ts": 60}

LONGS = [("BRK", chk_brk), ("MOM", chk_mom), ("DIP", chk_dip),
         ("PLB", chk_plb), ("VRS", chk_vrs)]

# 1 short strategy (the only one that worked)
def chk_rej_s(i):
    a = ATR[i]
    if np.isnan(a) or a <= 0: return None
    ev = e30[i]
    if np.isnan(ev): return None
    dist = ev - H[i]
    if dist > 0.5*a or dist < -1.5*a: return None
    if C[i] >= O[i] or C[i] >= ev: return None
    sl = max(H[i], ev) + 0.3*a
    risk = sl - C[i]
    if risk <= 0 or risk > 3*a: return None
    return {"s": "REJ_S", "e": C[i], "sl": sl, "ta": 4.0, "ts": 48}


# ═══════════════════════════════════════════════════════════════
#  UNIFIED ENGINE — supports all 5 modes
# ═══════════════════════════════════════════════════════════════

def run_mode(start, end, mode="A"):
    """
    Modes:
      A: current (uptrend filter only)
      B: uptrend + ADX>20
      C: uptrend + ADX>25
      D: longs in BULL only (same as A but explicit regime gate, no shorts)
      E: longs in BULL + 1 short in BEAR
    """
    bal = 10000.0; active = []; trades = []
    cds = {s: 0 for s in ["BRK","MOM","DIP","PLB","VRS","REJ_S"]}
    cd_n = {"BRK":3,"MOM":3,"DIP":3,"PLB":3,"VRS":2,"REJ_S":3}

    for i in range(max(start, ST), end):
        # ── EXITS ──
        still = []
        for t in active:
            a = ATR[i]; pnl = None
            if t["side"] == "buy":
                if not np.isnan(a) and a > 0:
                    nt = C[i] - t["ta"]*a
                    if nt > t["tr"]: t["tr"] = nt
                if L[i] <= t["sl"]:
                    pnl = (t["sl"]-t["e"])*t["u"]-FR
                elif t["tr"] > t["e"] and L[i] <= t["tr"]:
                    pnl = (t["tr"]-t["e"])*t["u"]-FR
                elif i >= t["tsi"]:
                    pnl = (C[i]-t["e"])*t["u"]-FR
            else:
                if not np.isnan(a) and a > 0:
                    nt = C[i] + t["ta"]*a
                    if nt < t["tr"]: t["tr"] = nt
                if H[i] >= t["sl"]:
                    pnl = (t["e"]-t["sl"])*t["u"]-FR
                elif t["tr"] < t["e"] and H[i] >= t["tr"]:
                    pnl = (t["e"]-t["tr"])*t["u"]-FR
                elif i >= t["tsi"]:
                    pnl = (t["e"]-C[i])*t["u"]-FR
            if pnl is not None:
                bal += pnl; cds[t["s"]] = i + cd_n[t["s"]]
                trades.append({"s":t["s"],"pnl":round(pnl,2),"ei":t["ei"],
                               "xi":i,"side":t["side"]})
            else: still.append(t)
        active = still
        if np.isnan(ATR[i]) or ATR[i] <= 0: continue

        # ── LONG ENTRY GATE (depends on mode) ──
        allow_long = False
        if mode == "A":
            allow_long = is_up(i)
        elif mode == "B":
            allow_long = is_up(i) and ADX[i] > 20
        elif mode == "C":
            allow_long = is_up(i) and ADX[i] > 25
        elif mode == "D":
            allow_long = is_up(i)  # same gate, no shorts
        elif mode == "E":
            allow_long = is_up(i)  # longs in BULL

        if allow_long:
            for sn, fn in LONGS:
                if i < cds[sn] or any(t["s"]==sn for t in active) or len(active) >= 5: continue
                sig = fn(i)
                if sig and sig["e"]-sig["sl"] > 0:
                    u = (bal*RSK)/(sig["e"]-sig["sl"])
                    active.append({"s":sig["s"],"e":sig["e"],"ei":i,"sl":sig["sl"],
                        "tr":sig["sl"],"ta":sig["ta"],"tsi":i+sig["ts"],
                        "u":u,"side":"buy"})

        # ── SHORT ENTRY (mode E only) ──
        if mode == "E" and is_dn(i):
            sn = "REJ_S"
            if i >= cds[sn] and not any(t["s"]==sn for t in active) and len(active) < 5:
                sig = chk_rej_s(i)
                if sig:
                    risk_amt = sig["sl"] - sig["e"]
                    if risk_amt > 0:
                        u = (bal*RSK)/risk_amt
                        active.append({"s":"REJ_S","e":sig["e"],"ei":i,"sl":sig["sl"],
                            "tr":sig["sl"],"ta":sig["ta"],"tsi":i+sig["ts"],
                            "u":u,"side":"sell"})

    # Close remaining
    for t in active:
        if t["side"] == "buy":
            pnl = (C[end-1]-t["e"])*t["u"]-FR
        else:
            pnl = (t["e"]-C[end-1])*t["u"]-FR
        bal += pnl
        trades.append({"s":t["s"],"pnl":round(pnl,2),"ei":t["ei"],"xi":end-1,"side":t["side"]})
    return trades, bal


def stats(trades):
    if not trades: return 0, 0, 0, 0
    pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = wins/len(trades)
    ws = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    ls = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = ws/ls if ls > 0 else 0
    return pnl, wr, pf, len(trades)

def calc_drawdown(trades):
    eq = [10000.0]
    for t in trades: eq.append(eq[-1] + t["pnl"])
    peak = eq[0]; mx = 0
    for e in eq:
        if e > peak: peak = e
        dd = (peak-e)/peak*100
        if dd > mx: mx = dd
    return mx

def group_q(trades):
    qs = {}
    for t in trades:
        d = gd(t["ei"])
        if len(d) >= 7:
            q = "%s-Q%d" % (d[:4], (int(d[5:7])-1)//3+1)
        else: q = "?"
        qs.setdefault(q, []).append(t)
    return qs


# ═══════════════════════════════════════════════════════════════
#  2. RUN ALL 5 MODES
# ═══════════════════════════════════════════════════════════════
print("\n[2/4] Running 5 timing modes on full data...")

modes = {
    "A": "CURRENT (uptrend only)",
    "B": "ADX>20 gate",
    "C": "ADX>25 gate (strict)",
    "D": "BULL-ONLY (no shorts)",
    "E": "BULL + 1 SHORT in bear",
}

results = {}
for m in modes:
    t, b = run_mode(0, NN, m)
    pnl, wr, pf, n = stats(t)
    dd = calc_drawdown(t)
    short_n = sum(1 for x in t if x["side"] == "sell")
    results[m] = {"trades": t, "pnl": pnl, "wr": wr, "pf": pf, "n": n,
                  "dd": dd, "shorts": short_n}

print("\n  %-6s %-25s | %5s | %3s | %8s | %4s | %5s | %5s" % (
    "Mode", "Description", "Trades", "WR", "PnL", "PF", "MaxDD", "Shrts"))
print("  " + "-" * 80)
for m in modes:
    r = results[m]
    print("  %-6s %-25s | %5d | %2.0f%% | $%+6.0f | %.2f | %4.1f%% | %5d" % (
        m, modes[m], r["n"], r["wr"]*100, r["pnl"], r["pf"], r["dd"], r["shorts"]))


# ═══════════════════════════════════════════════════════════════
#  3. WALK-FORWARD — 6 CHUNKS (each mode)
# ═══════════════════════════════════════════════════════════════
print("\n[3/4] Walk-forward (6 chunks per mode)...")

chunk = NN // 6

for m in modes:
    print("\n  MODE %s: %s" % (m, modes[m]))
    print("  %-5s | %-10s  %-10s | %4s | %8s | %4s | %s" % (
        "Chunk", "From", "To", "Trds", "PnL", "PF", "BTC"))
    print("  " + "-" * 70)
    prof = 0; chunk_pnls = []
    for ci in range(6):
        s = ci*chunk; e = (ci+1)*chunk if ci < 5 else NN
        t, _ = run_mode(s, e, m)
        p, w, pf, n = stats(t)
        btc = (C[e-1]-C[max(s,ST)])/C[max(s,ST)]*100
        if p > 0: prof += 1
        chunk_pnls.append(p)
        sn = sum(1 for x in t if x["side"] == "sell")
        extra = " (%dS)" % sn if sn > 0 else ""
        print("  W%-4d | %-10s  %-10s | %4d%s | $%+6.0f | %.2f | BTC %+.0f%%" % (
            ci+1, gd(max(s,ST)), gd(e-1), n, extra, p, pf, btc))
    worst = min(chunk_pnls)
    print("  Profitable: %d/6 | Worst chunk: $%+.0f" % (prof, worst))
    results[m]["prof_chunks"] = prof
    results[m]["worst_chunk"] = worst


# ═══════════════════════════════════════════════════════════════
#  4. QUARTERLY CONSISTENCY
# ═══════════════════════════════════════════════════════════════
print("\n[4/4] Quarterly consistency...")

for m in modes:
    qs = group_q(results[m]["trades"])
    losing = sum(1 for q in qs.values() if sum(t["pnl"] for t in q) < 0)
    total_q = len(qs)
    results[m]["losing_q"] = losing
    results[m]["total_q"] = total_q

print("\n  %-6s %-25s | %6s | %6s | %8s" % (
    "Mode", "Description", "LoseQ", "Prof/6", "WorstChk"))
print("  " + "-" * 65)
for m in modes:
    r = results[m]
    print("  %-6s %-25s | %d/%d  | %d/6   | $%+6.0f" % (
        m, modes[m], r["losing_q"], r["total_q"],
        r["prof_chunks"], r["worst_chunk"]))


# ═══════════════════════════════════════════════════════════════
#  SCORING — which mode is BEST overall?
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  SMART TIMING SCORECARD")
print("=" * 70)

# Score each mode (higher = better)
# Weights: consistency > profit > drawdown
print("\n  Scoring: PnL(25%%) + PF(20%%) + Consistency(30%%) + Drawdown(25%%)")
print()

max_pnl = max(r["pnl"] for r in results.values())
max_pf = max(r["pf"] for r in results.values())
max_prof = max(r["prof_chunks"] for r in results.values())
min_dd = min(r["dd"] for r in results.values())
min_lq = min(r["losing_q"] for r in results.values())

for m in modes:
    r = results[m]
    # Normalize 0-100
    s_pnl = (r["pnl"] / max_pnl * 100) if max_pnl > 0 else 0
    s_pf = (r["pf"] / max_pf * 100) if max_pf > 0 else 0
    s_consist = (r["prof_chunks"] / 6 * 50) + ((r["total_q"] - r["losing_q"]) / r["total_q"] * 50) if r["total_q"] > 0 else 0
    s_dd = (min_dd / r["dd"] * 100) if r["dd"] > 0 else 100

    total = s_pnl * 0.25 + s_pf * 0.20 + s_consist * 0.30 + s_dd * 0.25
    results[m]["score"] = total

    print("  Mode %s %-22s: PnL=%2.0f PF=%2.0f Consist=%2.0f DD=%2.0f => TOTAL: %.1f" % (
        m, modes[m], s_pnl, s_pf, s_consist, s_dd, total))

best_mode = max(results, key=lambda m: results[m]["score"])
print("\n  WINNER: Mode %s — %s (score %.1f)" % (best_mode, modes[best_mode], results[best_mode]["score"]))


# ═══════════════════════════════════════════════════════════════
#  FINAL COMPARISON TABLE
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  FINAL RECOMMENDATION")
print("=" * 70)

bm = results[best_mode]
curr = results["A"]

print("""
  CURRENT (Mode A):
    %d trades | $%+.0f | PF %.2f | MaxDD %.1f%% | %d/6 chunks | %d losing Q

  RECOMMENDED (Mode %s — %s):
    %d trades | $%+.0f | PF %.2f | MaxDD %.1f%% | %d/6 chunks | %d losing Q

  Changes:
    PnL:         $%+.0f (%s)
    PF:          %.2f -> %.2f (%s)
    Drawdown:    %.1f%% -> %.1f%% (%s)
    Consistency: %d/6 -> %d/6 chunks profitable
    Losing Q:    %d -> %d (%s)
""" % (
    curr["n"], curr["pnl"], curr["pf"], curr["dd"], curr["prof_chunks"], curr["losing_q"],
    best_mode, modes[best_mode],
    bm["n"], bm["pnl"], bm["pf"], bm["dd"], bm["prof_chunks"], bm["losing_q"],
    bm["pnl"]-curr["pnl"], "better" if bm["pnl"]>curr["pnl"] else "worse",
    curr["pf"], bm["pf"], "better" if bm["pf"]>curr["pf"] else "worse",
    curr["dd"], bm["dd"], "better" if bm["dd"]<curr["dd"] else "worse",
    curr["prof_chunks"], bm["prof_chunks"],
    curr["losing_q"], bm["losing_q"],
    "improved" if bm["losing_q"]<curr["losing_q"] else "same" if bm["losing_q"]==curr["losing_q"] else "worse",
))

# What to actually deploy
print("  WHAT TO DEPLOY:")
if best_mode == "A":
    print("    Keep current system. No timing changes needed.")
elif best_mode in ("B", "C"):
    adx_thresh = 20 if best_mode == "B" else 25
    print("    Add ADX > %d gate to uptrend filter." % adx_thresh)
    print("    Change is_uptrend() to: EMA21>EMA55 AND slope>0.1 AND ADX>%d" % adx_thresh)
    print("    This filters out %d weak-trend trades." % (curr["n"] - bm["n"]))
elif best_mode == "D":
    print("    Keep longs as-is. No shorts needed.")
    print("    Teacher was right: 'no-trade mode' IS the upgrade.")
elif best_mode == "E":
    print("    Keep longs + add EMA_REJECTION short in BEAR regime.")
    print("    Params: ema=30, max_dist=0.5, sl=1.5xATR, trail=4.0xATR, ts=48 bars")

print("\nDone.")
