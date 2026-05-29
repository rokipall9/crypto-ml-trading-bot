"""
STRATEGY AUDIT — Validate each strategy in isolation
======================================================
Runs each of the 6 strategies solo against the same bar (trades>=30, PF>=1.3, DD<=25%)
that S1+S2 pass on BTC and ETH.

Purpose: identify which strategies are hitchhiking on the combined backtest
vs genuinely contributing edge.
"""
import sys, warnings
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, "/home/ubuntu/bot")
import market_data

SYMBOLS = ["BTCUSDT", "ETHUSDT"]   # only run on PASS-ing symbols from multi-sym
MIN_TRADES = 30
MIN_PF = 1.3
MAX_DD = 25.0

ST = 60; FR = 20; RSK = 0.01


def build_ind(df):
    n = len(df)
    H = df["high"].values.astype(float)
    L = df["low"].values.astype(float)
    C = df["close"].values.astype(float)
    O = df["open"].values.astype(float)
    V = df["volume"].values.astype(float)
    def ema(a, p): return pd.Series(a).ewm(span=p, adjust=False).mean().values
    def atr_f(h, l, c, p=14):
        tr = np.zeros(len(h))
        for i in range(1, len(h)):
            tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        return pd.Series(tr).rolling(p, min_periods=p).mean().values
    e21 = ema(C, 21); e30 = ema(C, 30); e40 = ema(C, 40)
    e50 = ema(C, 50); e55 = ema(C, 55)
    ATR = atr_f(H, L, C, 14)
    vol20 = pd.Series(V).rolling(20, min_periods=20).mean().values
    bd = np.abs(C - O); bd20 = pd.Series(bd).rolling(20, min_periods=20).mean().values
    hi30 = pd.Series(H).rolling(30, min_periods=30).max().values
    hi8 = pd.Series(H).rolling(8, min_periods=8).max().values
    slp = np.zeros(n)
    for i in range(6, n):
        if e50[i-6] > 0: slp[i] = (e50[i]-e50[i-6])/e50[i-6]*100
    return dict(n=n, H=H, L=L, C=C, O=O, V=V, e21=e21, e30=e30, e40=e40,
                e55=e55, ATR=ATR, vol20=vol20, bd=bd, bd20=bd20,
                hi30=hi30, hi8=hi8, slp=slp)


# ── Strategy check fns ──────────────────────────────────────────────
def chk_brk(d, i):
    if np.isnan(d["hi30"][i]) or np.isnan(d["vol20"][i]) or d["vol20"][i] <= 0: return None
    if d["C"][i] <= d["hi30"][i-1] or d["V"][i] < d["vol20"][i]*1.5 or d["C"][i] <= d["O"][i]: return None
    return ("BRK", d["C"][i], d["C"][i]-1.5*d["ATR"][i], 3.0, 48, "buy")

def chk_mom(d, i):
    if np.isnan(d["bd20"][i]) or d["bd20"][i] <= 0 or np.isnan(d["vol20"][i]) or d["vol20"][i] <= 0: return None
    if d["C"][i] <= d["O"][i] or d["bd"][i] < d["bd20"][i]*2 or d["V"][i] < d["vol20"][i]*2 or d["C"][i] <= d["e21"][i]: return None
    return ("MOM", d["C"][i], d["C"][i]-1.5*d["ATR"][i], 3.0, 48, "buy")

def chk_dip(d, i):
    if np.isnan(d["hi8"][i]) or d["hi8"][i] <= 0 or np.isnan(d["bd20"][i]) or d["bd20"][i] <= 0: return None
    dp = (d["hi8"][i]-d["L"][i])/d["hi8"][i]*100
    if dp < 3 or d["C"][i] <= d["O"][i] or d["bd"][i] < d["bd20"][i]*1.5 or d["C"][i] < d["e55"][i]: return None
    return ("DIP", d["C"][i], d["C"][i]-1.5*d["ATR"][i], 3.5, 48, "buy")

def chk_plb(d, i):
    a = d["ATR"][i]
    if np.isnan(a) or a <= 0 or np.isnan(d["e40"][i]): return None
    dd = d["L"][i]-d["e40"][i]
    if dd < -0.3*a or dd > 1.2*a or d["L"][i] > d["e21"][i] or d["C"][i] <= d["O"][i] or d["C"][i] <= d["e21"][i]: return None
    sl = min(d["L"][i], d["e40"][i])-0.3*a
    if d["C"][i]-sl <= 0 or d["C"][i]-sl > 3*a: return None
    return ("PLB", d["C"][i], sl, 3.5, 48, "buy")

def chk_vrs(d, i):
    a = d["ATR"][i]
    if np.isnan(a) or a <= 0 or np.isnan(d["vol20"][i]) or d["vol20"][i] <= 0: return None
    dc = sum(1 for j in range(1, 4) if i-j >= 0 and d["V"][i-j] < 0.8*d["vol20"][i])
    if dc < 1 or d["V"][i] < 1.2*d["vol20"][i] or abs(d["C"][i]-d["e21"][i]) > 0.8*a: return None
    if d["C"][i] <= d["O"][i] or d["C"][i] <= d["e21"][i]: return None
    return ("VRS", d["C"][i], d["C"][i]-1.5*a, 4.0, 60, "buy")

def chk_rej(d, i):
    """SHORT — bear regime only."""
    a = d["ATR"][i]
    if np.isnan(a) or a <= 0: return None
    ev = d["e30"][i]
    if np.isnan(ev): return None
    dist = ev - d["H"][i]
    if dist > 0.5*a or dist < -1.5*a: return None
    if d["C"][i] >= d["O"][i] or d["C"][i] >= ev: return None
    sl = max(d["H"][i], ev) + 0.3*a
    risk = sl - d["C"][i]
    if risk <= 0 or risk > 3*a: return None
    return ("REJ", d["C"][i], sl, 4.0, 48, "sell")


STRATS = {
    "S1_BREAKOUT":      (chk_brk, "buy"),
    "S2_VOL_MOMENTUM":  (chk_mom, "buy"),
    "S3_PANIC_DIP":     (chk_dip, "buy"),
    "S4_EMA_PULLBACK":  (chk_plb, "buy"),
    "S5_VOL_RESET":     (chk_vrs, "buy"),
    "S6_REJECTION_SHORT": (chk_rej, "sell"),
}


def is_up(d, i): return d["e21"][i] > d["e55"][i] and d["slp"][i] > 0.1
def is_dn(d, i): return d["e21"][i] < d["e55"][i] and d["slp"][i] < -0.1


def run_solo(d, chk_fn, side_type):
    """Run single strategy in its appropriate regime gate."""
    n = d["n"]
    bal = 10000.0
    active = []
    trades = []
    cd = 0

    for i in range(ST, n):
        # exits
        still = []
        for t in active:
            a = d["ATR"][i]
            pnl = None
            if t["side"] == "buy":
                if not np.isnan(a) and a > 0:
                    nt = d["C"][i] - t["ta"]*a
                    if nt > t["tr"]: t["tr"] = nt
                if d["L"][i] <= t["sl"]:
                    pnl = (t["sl"]-t["e"])*t["u"]-FR
                elif t["tr"] > t["e"] and d["L"][i] <= t["tr"]:
                    pnl = (t["tr"]-t["e"])*t["u"]-FR
                elif i >= t["tsi"]:
                    pnl = (d["C"][i]-t["e"])*t["u"]-FR
            else:  # sell
                if not np.isnan(a) and a > 0:
                    nt = d["C"][i] + t["ta"]*a
                    if nt < t["tr"]: t["tr"] = nt
                if d["H"][i] >= t["sl"]:
                    pnl = (t["e"]-t["sl"])*t["u"]-FR
                elif t["tr"] < t["e"] and d["H"][i] >= t["tr"]:
                    pnl = (t["e"]-t["tr"])*t["u"]-FR
                elif i >= t["tsi"]:
                    pnl = (t["e"]-d["C"][i])*t["u"]-FR
            if pnl is not None:
                bal += pnl
                cd = i + 3
                trades.append({"pnl": round(pnl, 2), "side": t["side"]})
            else:
                still.append(t)
        active = still

        if np.isnan(d["ATR"][i]) or d["ATR"][i] <= 0:
            continue

        # regime gate
        in_regime = is_up(d, i) if side_type == "buy" else is_dn(d, i)
        if not in_regime:
            continue

        if i < cd or len(active) >= 1:   # solo = only one concurrent position
            continue

        sig = chk_fn(d, i)
        if sig is None:
            continue
        s_name, entry, sl, ta, ts_bars, side = sig
        risk_dist = abs(entry - sl)
        if risk_dist <= 0:
            continue
        u = (bal * RSK) / risk_dist
        active.append({"s": s_name, "e": entry, "ei": i, "sl": sl,
                       "tr": sl, "ta": ta, "tsi": i+ts_bars, "u": u, "side": side})

    # close remaining
    for t in active:
        if t["side"] == "buy":
            pnl = (d["C"][n-1] - t["e"]) * t["u"] - FR
        else:
            pnl = (t["e"] - d["C"][n-1]) * t["u"] - FR
        bal += pnl
        trades.append({"pnl": round(pnl, 2), "side": t["side"]})

    return trades, bal


def calc_stats(trades):
    if not trades:
        return {"n": 0, "pnl": 0, "pf": 0, "wr": 0, "dd": 0}
    pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = wins / len(trades)
    ws = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    ls = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = ws / ls if ls > 0 else 0
    eq = [10000.0]
    for t in trades:
        eq.append(eq[-1] + t["pnl"])
    peak = eq[0]; dd_max = 0
    for e in eq:
        if e > peak: peak = e
        dd = (peak - e) / peak * 100
        if dd > dd_max: dd_max = dd
    return {"n": len(trades), "pnl": pnl, "pf": pf, "wr": wr, "dd": dd_max}


def verdict(st):
    if st["n"] < MIN_TRADES: return "FAIL (few trades)"
    if st["pf"] < MIN_PF:    return "FAIL (PF low)"
    if st["dd"] > MAX_DD:    return "FAIL (DD high)"
    return "PASS"


# =====================================================================
# MAIN
# =====================================================================
print("=" * 82)
print("  SOLO STRATEGY AUDIT — each strategy run in isolation")
print("=" * 82)
print("  Pass: trades>=%d  PF>=%.1f  MaxDD<=%.0f%%" % (MIN_TRADES, MIN_PF, MAX_DD))
print()

results = {}  # {sym: {strat: stats}}

for sym in SYMBOLS:
    print("\n--- %s ---" % sym)
    try:
        df = market_data.download(sym, "4h", total_candles=5000)
    except Exception as e:
        print("  download failed: %s" % e)
        continue
    d = build_ind(df)
    results[sym] = {}
    for name, (fn, side) in STRATS.items():
        trades, bal = run_solo(d, fn, side)
        st = calc_stats(trades)
        v = verdict(st)
        print("  %-22s trades=%3d  pnl=$%+7.0f  PF=%5.2f  WR=%5.1f%%  DD=%5.1f%%  [%s]" % (
            name, st["n"], st["pnl"], st["pf"], st["wr"]*100, st["dd"], v))
        results[sym][name] = {"st": st, "v": v}

# Combined verdict: strategy passes if it PASSES on BOTH BTC and ETH
print()
print("=" * 82)
print("  CROSS-SYMBOL VERDICT (strategy passes iff PASS on both BTC AND ETH)")
print("=" * 82)
print("  %-22s %-18s %-18s %s" % ("STRATEGY", "BTC", "ETH", "FINAL"))
print("  " + "-" * 78)
for name in STRATS:
    if "BTCUSDT" not in results or "ETHUSDT" not in results:
        print("  %-22s  missing data" % name); continue
    btc_st = results["BTCUSDT"][name]
    eth_st = results["ETHUSDT"][name]
    btc_v = btc_st["v"]
    eth_v = eth_st["v"]
    btc_label = "PASS  PF %.2f" % btc_st["st"]["pf"] if btc_v == "PASS" else btc_v
    eth_label = "PASS  PF %.2f" % eth_st["st"]["pf"] if eth_v == "PASS" else eth_v
    final = "KEEP" if (btc_v == "PASS" and eth_v == "PASS") else \
            "BTC-ONLY" if (btc_v == "PASS") else \
            "DROP"
    print("  %-22s %-18s %-18s %s" % (name, btc_label, eth_label, final))
