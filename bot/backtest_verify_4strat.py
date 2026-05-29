"""
VERIFICATION BACKTEST — S1+S2+S3+S4 combined on BTC
=====================================================
Confirms that dropping S5 (VOL_RESET) and S6 (REJECTION_SHORT) doesn't
cripple combined performance. Compares against the prior Mode E (6-strategy)
benchmark: 262 trades, PF 1.92, +$45,634.

Pass criteria:
  trades  >= 100    (statistical robustness)
  PF      >= 1.6    (keep most of the 1.92 original edge)
  MaxDD   <= 20%    (risk stays bounded)
  PnL     >= $15k   (meaningful edge over ~833 days)
"""
import sys, warnings
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, "/home/ubuntu/bot")
import market_data

print("=" * 70)
print("  VERIFICATION: 4-STRATEGY BTC-ONLY CONFIG")
print("=" * 70)
print("  Strategies: S1 BREAKOUT + S2 VOL_MOMENTUM + S3 PANIC_DIP + S4 EMA_PULLBACK")
print("  Gate: BULL only (EMA21>55, slope>0.1%%)")
print("  Dropped: S5 VOL_RESET (under-sampled), S6 REJECTION_SHORT (drag)")
print()

df = market_data.download("BTCUSDT", "4h", total_candles=5000)
n = len(df)
H = df["high"].values.astype(float)
L = df["low"].values.astype(float)
C = df["close"].values.astype(float)
O = df["open"].values.astype(float)
V = df["volume"].values.astype(float)
dates = df["open_time"].values if "open_time" in df.columns else [None]*n

def gd(i):
    if i < len(dates) and dates[i] is not None: return str(dates[i])[:10]
    return "bar_%d" % i

def ema(a, p): return pd.Series(a).ewm(span=p, adjust=False).mean().values
def atr_f(h, l, c, p=14):
    tr = np.zeros(len(h))
    for i in range(1, len(h)):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    return pd.Series(tr).rolling(p, min_periods=p).mean().values

e21 = ema(C, 21); e40 = ema(C, 40); e50 = ema(C, 50); e55 = ema(C, 55)
ATR = atr_f(H, L, C, 14)
vol20 = pd.Series(V).rolling(20, min_periods=20).mean().values
bd = np.abs(C - O); bd20 = pd.Series(bd).rolling(20, min_periods=20).mean().values
hi30 = pd.Series(H).rolling(30, min_periods=30).max().values
hi8 = pd.Series(H).rolling(8, min_periods=8).max().values
slp = np.zeros(n)
for i in range(6, n):
    if e50[i-6] > 0: slp[i] = (e50[i]-e50[i-6])/e50[i-6]*100

ST = 60; FR = 20; RSK = 0.01

def is_up(i): return e21[i] > e55[i] and slp[i] > 0.1

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

LONGS = [("BRK", chk_brk), ("MOM", chk_mom), ("DIP", chk_dip), ("PLB", chk_plb)]

def run():
    bal = 10000.0; active = []; trades = []
    cds = {s: 0 for s in ["BRK", "MOM", "DIP", "PLB"]}
    cd_n = {"BRK": 3, "MOM": 3, "DIP": 3, "PLB": 3}

    for i in range(ST, n):
        # exits
        still = []
        for t in active:
            a = ATR[i]
            pnl = None
            if not np.isnan(a) and a > 0:
                nt = C[i] - t["ta"]*a
                if nt > t["tr"]: t["tr"] = nt
            if L[i] <= t["sl"]: pnl = (t["sl"]-t["e"])*t["u"]-FR
            elif t["tr"] > t["e"] and L[i] <= t["tr"]: pnl = (t["tr"]-t["e"])*t["u"]-FR
            elif i >= t["tsi"]: pnl = (C[i]-t["e"])*t["u"]-FR
            if pnl is not None:
                bal += pnl; cds[t["s"]] = i + cd_n[t["s"]]
                trades.append({"s": t["s"], "pnl": round(pnl, 2), "ei": t["ei"], "xi": i})
            else:
                still.append(t)
        active = still

        if np.isnan(ATR[i]) or ATR[i] <= 0: continue
        if not is_up(i): continue

        for sn, fn in LONGS:
            if i < cds[sn] or any(t["s"]==sn for t in active) or len(active) >= 5: continue
            sig = fn(i)
            if sig and sig["e"]-sig["sl"] > 0:
                u = (bal*RSK)/(sig["e"]-sig["sl"])
                active.append({"s": sig["s"], "e": sig["e"], "ei": i, "sl": sig["sl"],
                               "tr": sig["sl"], "ta": sig["ta"], "tsi": i+sig["ts"], "u": u})

    # close remaining
    for t in active:
        pnl = (C[n-1]-t["e"])*t["u"]-FR
        bal += pnl
        trades.append({"s": t["s"], "pnl": round(pnl, 2), "ei": t["ei"], "xi": n-1})
    return trades, bal

def stats(trades):
    pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = wins/len(trades) if trades else 0
    ws = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    ls = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = ws/ls if ls > 0 else 0
    eq = [10000.0]
    for t in trades: eq.append(eq[-1] + t["pnl"])
    peak = eq[0]; mx = 0
    for e in eq:
        if e > peak: peak = e
        dd = (peak-e)/peak*100
        if dd > mx: mx = dd
    return {"pnl": pnl, "pf": pf, "wr": wr, "n": len(trades), "dd": mx, "final": eq[-1]}

trades, final_bal = run()
st = stats(trades)

# per-strategy breakdown
by_s = {}
for t in trades:
    by_s.setdefault(t["s"], []).append(t)

print("  Period: %s → %s (%d bars)" % (gd(ST), gd(n-1), n-ST))
print()
print("  COMBINED RESULT")
print("  " + "-" * 58)
print("  Trades:       %d" % st["n"])
print("  Final bal:    $%.0f" % st["final"])
print("  PnL:          $%+.0f (%.1f%%)" % (st["pnl"], st["pnl"]/100))
print("  Profit factor: %.2f" % st["pf"])
print("  Win rate:     %.1f%%" % (st["wr"]*100))
print("  Max drawdown: %.1f%%" % st["dd"])
print()
print("  PER-STRATEGY CONTRIBUTION")
print("  " + "-" * 58)
print("  %-5s  %-6s  %-11s  %-5s  %-7s" % ("Strat", "Trades", "PnL", "PF", "WR"))
for s, ts_ in by_s.items():
    p = sum(t["pnl"] for t in ts_)
    w = sum(1 for t in ts_ if t["pnl"] > 0)
    ws_ = sum(t["pnl"] for t in ts_ if t["pnl"] > 0)
    ls_ = abs(sum(t["pnl"] for t in ts_ if t["pnl"] <= 0))
    pf_ = ws_/ls_ if ls_ > 0 else 0
    print("  %-5s  %6d  $%+9.0f  %5.2f  %6.1f%%" % (s, len(ts_), p, pf_, w/len(ts_)*100))

# Pass/fail
print()
print("  BENCHMARK COMPARISON")
print("  " + "-" * 58)
print("  Prior Mode E (6 strats): 262 trades  PF 1.92  +$45,634")
print("  New   4-strat (BTC):     %3d trades  PF %.2f  $%+.0f" % (st["n"], st["pf"], st["pnl"]))

print()
print("  PASS CRITERIA")
print("  " + "-" * 58)
t_ok = st["n"] >= 100
p_ok = st["pf"] >= 1.6
d_ok = st["dd"] <= 20
pnl_ok = st["pnl"] >= 15000

print("  Trades >= 100:    %s (%d)" % ("PASS" if t_ok else "FAIL", st["n"]))
print("  PF >= 1.60:       %s (%.2f)" % ("PASS" if p_ok else "FAIL", st["pf"]))
print("  DD <= 20%%:        %s (%.1f%%)" % ("PASS" if d_ok else "FAIL", st["dd"]))
print("  PnL >= $15k:      %s ($%+.0f)" % ("PASS" if pnl_ok else "FAIL", st["pnl"]))

all_pass = t_ok and p_ok and d_ok and pnl_ok
print()
print("  " + ("=" * 58))
print("  OVERALL: " + ("GREEN LIGHT — ship the patch" if all_pass else "RED — investigate before removing S5/S6"))
print("  " + ("=" * 58))
