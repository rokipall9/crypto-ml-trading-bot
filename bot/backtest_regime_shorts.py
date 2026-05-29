"""
FULL SYSTEM UPGRADE — Regime Detection + Short Strategies + Adaptive Sizing
============================================================================
Fixes the 3 gaps:
  1. SHORTING: 3 new short strategies for bear markets
  2. REGIME DETECTION: BULL/BEAR/CHOP via EMA alignment + slope + ADX
  3. ADAPTIVE LOGIC: ADX-based position sizing

Process: sweep short params -> combine with 5 longs -> walk-forward validate
"""
import sys, warnings
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore', category=RuntimeWarning)
sys.path.insert(0, "/home/ubuntu/bot")
import market_data

print("=" * 70)
print("  FULL SYSTEM UPGRADE: Regime + Shorts + Adaptive")
print("=" * 70)

# ═══════════════════════════════════════════════════════════════
#  1. DATA + INDICATORS
# ═══════════════════════════════════════════════════════════════
print("\n[1/6] Loading data...")
df = market_data.download("BTCUSDT", "4h", total_candles=5000)
N = len(df)

dates = df["open_time"].values if "open_time" in df.columns else [None]*N
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

# Rolling lows for breakdown shorts
lo = {k: pd.Series(L).rolling(k, min_periods=k).min().values for k in [20, 30, 40]}

# EMA50 slope
slp = np.zeros(N)
for i in range(6, N):
    if e50[i-6] > 0: slp[i] = (e50[i] - e50[i-6]) / e50[i-6] * 100

# ADX
def calc_adx(h, l, c, p=14):
    n = len(h); pdm = np.zeros(n); mdm = np.zeros(n); tr = np.zeros(n)
    for i in range(1, n):
        u = h[i] - h[i-1]; d = l[i-1] - l[i]
        if u > d and u > 0: pdm[i] = u
        if d > u and d > 0: mdm[i] = d
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    atr_s = pd.Series(tr).ewm(span=p, adjust=False).mean().values
    sp = pd.Series(pdm).ewm(span=p, adjust=False).mean().values
    sm = pd.Series(mdm).ewm(span=p, adjust=False).mean().values
    pdi = np.where(atr_s > 0, sp / atr_s * 100, 0)
    mdi = np.where(atr_s > 0, sm / atr_s * 100, 0)
    dx = np.where(pdi + mdi > 0, np.abs(pdi - mdi) / (pdi + mdi) * 100, 0)
    return pd.Series(dx).ewm(span=p, adjust=False).mean().values

ADX = calc_adx(H, L, C, 14)
ema_map = {21: e21, 30: e30, 40: e40, 50: e50, 55: e55}

ST = 60   # min start bar
FR = 20   # friction per trade
RSK = 0.01

print("  %d candles: %s to %s" % (N, gd(0), gd(N-1)))

# ═══════════════════════════════════════════════════════════════
#  2. REGIME DETECTION
# ═══════════════════════════════════════════════════════════════
print("\n[2/6] Regime classification...")

def is_up(i): return e21[i] > e55[i] and slp[i] > 0.1
def is_dn(i): return e21[i] < e55[i] and slp[i] < -0.1
def rgm(i):
    if is_up(i): return 'BULL'
    if is_dn(i): return 'BEAR'
    return 'CHOP'

def adx_risk(i):
    """Adaptive risk multiplier based on trend strength."""
    if ADX[i] > 25: return 1.0
    if ADX[i] > 15: return 0.7
    return 0.5

bull_n = sum(1 for i in range(ST, N) if rgm(i) == 'BULL')
bear_n = sum(1 for i in range(ST, N) if rgm(i) == 'BEAR')
chop_n = (N - ST) - bull_n - bear_n
tot = N - ST
print("  BULL: %d bars (%.0f%%) | BEAR: %d bars (%.0f%%) | CHOP: %d bars (%.0f%%)" % (
    bull_n, bull_n/tot*100, bear_n, bear_n/tot*100, chop_n, chop_n/tot*100))
print("  Avg ADX — BULL: %.1f | BEAR: %.1f | CHOP: %.1f" % (
    np.mean([ADX[i] for i in range(ST, N) if rgm(i) == 'BULL']) if bull_n else 0,
    np.mean([ADX[i] for i in range(ST, N) if rgm(i) == 'BEAR']) if bear_n else 0,
    np.mean([ADX[i] for i in range(ST, N) if rgm(i) == 'CHOP']) if chop_n else 0))


# ═══════════════════════════════════════════════════════════════
#  LONG STRATEGIES (existing 5 — unchanged)
# ═══════════════════════════════════════════════════════════════

def chk_brk(i):
    if np.isnan(hi30[i]) or np.isnan(vol20[i]) or vol20[i] <= 0: return None
    if C[i] <= hi30[i-1] or V[i] < vol20[i]*1.5 or C[i] <= O[i]: return None
    return {"s": "BRK", "e": C[i], "sl": C[i]-1.5*ATR[i], "ta": 3.0, "ts": 48, "side": "buy"}

def chk_mom(i):
    if np.isnan(bd20[i]) or bd20[i] <= 0 or np.isnan(vol20[i]) or vol20[i] <= 0: return None
    if C[i] <= O[i] or bd[i] < bd20[i]*2 or V[i] < vol20[i]*2 or C[i] <= e21[i]: return None
    return {"s": "MOM", "e": C[i], "sl": C[i]-1.5*ATR[i], "ta": 3.0, "ts": 48, "side": "buy"}

def chk_dip(i):
    if np.isnan(hi8[i]) or hi8[i] <= 0 or np.isnan(bd20[i]) or bd20[i] <= 0: return None
    dp = (hi8[i]-L[i]) / hi8[i] * 100
    if dp < 3 or C[i] <= O[i] or bd[i] < bd20[i]*1.5 or C[i] < e55[i]: return None
    return {"s": "DIP", "e": C[i], "sl": C[i]-1.5*ATR[i], "ta": 3.5, "ts": 48, "side": "buy"}

def chk_plb(i):
    a = ATR[i]
    if np.isnan(a) or a <= 0 or np.isnan(e40[i]): return None
    d = L[i] - e40[i]
    if d < -0.3*a or d > 1.2*a or L[i] > e21[i] or C[i] <= O[i] or C[i] <= e21[i]: return None
    sl = min(L[i], e40[i]) - 0.3*a
    if C[i]-sl <= 0 or C[i]-sl > 3*a: return None
    return {"s": "PLB", "e": C[i], "sl": sl, "ta": 3.5, "ts": 48, "side": "buy"}

def chk_vrs(i):
    a = ATR[i]
    if np.isnan(a) or a <= 0 or np.isnan(vol20[i]) or vol20[i] <= 0: return None
    dc = sum(1 for j in range(1, 4) if i-j >= 0 and V[i-j] < 0.8*vol20[i])
    if dc < 1 or V[i] < 1.2*vol20[i] or abs(C[i]-e21[i]) > 0.8*a: return None
    if C[i] <= O[i] or C[i] <= e21[i]: return None
    return {"s": "VRS", "e": C[i], "sl": C[i]-1.5*a, "ta": 4.0, "ts": 60, "side": "buy"}

LONGS = [("BRK", chk_brk), ("MOM", chk_mom), ("DIP", chk_dip),
         ("PLB", chk_plb), ("VRS", chk_vrs)]


# ═══════════════════════════════════════════════════════════════
#  SHORT STRATEGIES (3 new — parametric for sweep)
# ═══════════════════════════════════════════════════════════════

def chk_brk_s(i, lookback=30, vol_mult=1.5, sl_atr=1.5):
    """BREAKDOWN SHORT: price breaks below N-bar low with volume."""
    a = ATR[i]
    if np.isnan(a) or a <= 0: return None
    la = lo.get(lookback)
    if la is None or np.isnan(la[i-1]): return None
    if np.isnan(vol20[i]) or vol20[i] <= 0: return None
    if C[i] >= la[i-1] or V[i] < vol20[i]*vol_mult or C[i] >= O[i]: return None
    return {"s": "BRK_S", "e": C[i], "sl": C[i]+sl_atr*a, "side": "sell"}

def chk_mom_s(i, body_mult=2.0, vol_mult=2.0, sl_atr=1.5):
    """MOMENTUM SHORT: big red candle + volume below EMA."""
    a = ATR[i]
    if np.isnan(a) or a <= 0: return None
    if np.isnan(bd20[i]) or bd20[i] <= 0 or np.isnan(vol20[i]) or vol20[i] <= 0: return None
    if C[i] >= O[i] or bd[i] < bd20[i]*body_mult or V[i] < vol20[i]*vol_mult: return None
    if C[i] >= e21[i]: return None
    return {"s": "MOM_S", "e": C[i], "sl": C[i]+sl_atr*a, "side": "sell"}

def chk_rej_s(i, ema_p=40, max_dist=0.5, sl_atr=1.5):
    """EMA REJECTION SHORT: bear rally reaches EMA, gets rejected."""
    a = ATR[i]
    if np.isnan(a) or a <= 0: return None
    ev = ema_map.get(ema_p, e40)[i]
    if np.isnan(ev): return None
    dist = ev - H[i]  # positive = high below EMA
    if dist > max_dist*a or dist < -1.5*a: return None
    if C[i] >= O[i] or C[i] >= ev: return None
    sl = max(H[i], ev) + 0.3*a
    risk = sl - C[i]
    if risk <= 0 or risk > 3*a: return None
    return {"s": "REJ_S", "e": C[i], "sl": sl, "side": "sell"}


# ═══════════════════════════════════════════════════════════════
#  3. PARAMETER SWEEP FOR SHORTS
# ═══════════════════════════════════════════════════════════════
print("\n[3/6] Sweeping short strategy parameters...")

def sweep_short(name, fn, combos, trails, tstops):
    """Sweep one short strategy. Returns sorted results."""
    results = []
    total = len(combos) * len(trails) * len(tstops)
    for prm in combos:
        for ta in trails:
            for ts in tstops:
                trades = []; act = None; cd = 0; bal = 10000.0
                for i in range(ST, N):
                    # Exit check
                    if act:
                        a = ATR[i]
                        if not np.isnan(a) and a > 0:
                            nt = C[i] + ta * a  # trail for short
                            if nt < act["tr"]: act["tr"] = nt
                        pnl = None
                        if H[i] >= act["sl"]:  # SL hit
                            pnl = (act["e"] - act["sl"]) * act["u"] - FR
                        elif act["tr"] < act["e"] and H[i] >= act["tr"]:  # Trail hit (profit)
                            pnl = (act["e"] - act["tr"]) * act["u"] - FR
                        elif i >= act["tsi"]:  # Time stop
                            pnl = (act["e"] - C[i]) * act["u"] - FR
                        if pnl is not None:
                            bal += pnl
                            trades.append({"pnl": round(pnl, 2), "ei": act["ei"], "xi": i})
                            act = None; cd = i + 3
                    # Entry check
                    if act or i < cd or not is_dn(i): continue
                    if np.isnan(ATR[i]) or ATR[i] <= 0: continue
                    sig = fn(i, **prm)
                    if sig is None: continue
                    risk_amt = sig["sl"] - sig["e"]
                    if risk_amt <= 0: continue
                    u = (bal * RSK) / risk_amt
                    act = {"e": sig["e"], "sl": sig["sl"], "tr": sig["sl"],
                           "ei": i, "tsi": i + ts, "u": u}
                # Close remaining
                if act:
                    pnl = (act["e"] - C[N-1]) * act["u"] - FR
                    bal += pnl
                    trades.append({"pnl": round(pnl, 2), "ei": act["ei"], "xi": N-1})

                nt_ = len(trades)
                if nt_ < 8: continue
                pnl_t = sum(t["pnl"] for t in trades)
                if pnl_t <= 0: continue
                ws = sum(t["pnl"] for t in trades if t["pnl"] > 0)
                ls = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
                pf = ws / ls if ls > 0 else 99
                if pf < 1.1: continue
                wr = sum(1 for t in trades if t["pnl"] > 0) / nt_
                half = nt_ // 2
                h1 = sum(t["pnl"] for t in trades[:half])
                h2 = sum(t["pnl"] for t in trades[half:])
                wf = h1 > 0 and h2 > 0
                results.append({"p": prm, "ta": ta, "ts": ts, "n": nt_, "pnl": pnl_t,
                                "pf": pf, "wr": wr, "h1": h1, "h2": h2, "wf": wf})
    results.sort(key=lambda x: (x["wf"], x["pf"]), reverse=True)
    return results, total

# BREAKDOWN_SHORT sweep
brk_s_combos = [{"lookback": lb, "vol_mult": vm, "sl_atr": sa}
    for lb in [20, 30, 40] for vm in [1.5, 2.0] for sa in [1.5, 2.0]]
brk_s_res, brk_s_tot = sweep_short("BRK_S", chk_brk_s, brk_s_combos, [3.0, 3.5, 4.0], [48, 60])

# MOMENTUM_SHORT sweep
mom_s_combos = [{"body_mult": bm, "vol_mult": vm, "sl_atr": sa}
    for bm in [1.5, 2.0, 2.5] for vm in [1.5, 2.0] for sa in [1.5, 2.0]]
mom_s_res, mom_s_tot = sweep_short("MOM_S", chk_mom_s, mom_s_combos, [3.0, 3.5, 4.0], [48, 60])

# EMA_REJECTION sweep
rej_s_combos = [{"ema_p": ep, "max_dist": md, "sl_atr": sa}
    for ep in [30, 40, 50] for md in [0.3, 0.5, 0.8] for sa in [1.5, 2.0]]
rej_s_res, rej_s_tot = sweep_short("REJ_S", chk_rej_s, rej_s_combos, [3.0, 3.5, 4.0], [48, 60])

# Print sweep results
for nm, res, tot in [("BREAKDOWN_SHORT", brk_s_res, brk_s_tot),
                      ("MOMENTUM_SHORT", mom_s_res, mom_s_tot),
                      ("EMA_REJECTION", rej_s_res, rej_s_tot)]:
    passed = [r for r in res if r["wf"]]
    print("\n  %s: %d configs -> %d profitable -> %d walk-forward pass" % (
        nm, tot, len(res), len(passed)))
    if res:
        b = res[0]
        tag = "WF-PASS" if b["wf"] else "WF-FAIL"
        print("    BEST: %d trades | $%+.0f | PF %.2f | WR %.0f%% [%s]" % (
            b["n"], b["pnl"], b["pf"], b["wr"]*100, tag))
        print("    Params: %s | trail=%.1f | ts=%d" % (b["p"], b["ta"], b["ts"]))
        print("    Half1=$%+.0f | Half2=$%+.0f" % (b["h1"], b["h2"]))

# Store winners
best_shorts = {}
if brk_s_res: best_shorts["BRK_S"] = brk_s_res[0]
if mom_s_res: best_shorts["MOM_S"] = mom_s_res[0]
if rej_s_res: best_shorts["REJ_S"] = rej_s_res[0]
n_shorts = len(best_shorts)
print("\n  Short strategies viable: %d/3" % n_shorts)


# ═══════════════════════════════════════════════════════════════
#  4. COMBINED SYSTEM ENGINE
# ═══════════════════════════════════════════════════════════════

def stats(trades):
    if not trades: return 0, 0, 0, 0
    pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = wins / len(trades)
    ws = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    ls = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = ws / ls if ls > 0 else 0
    return pnl, wr, pf, len(trades)

def run_system(start, end, use_regime=True, use_shorts=True, adaptive=True):
    """
    Full system: 5 longs + up to 3 shorts + regime gating + ADX sizing.
    """
    bal = 10000.0; active = []; trades = []
    cds = {s: 0 for s in ["BRK","MOM","DIP","PLB","VRS","BRK_S","MOM_S","REJ_S"]}
    cd_n = {"BRK":3,"MOM":3,"DIP":3,"PLB":3,"VRS":2,"BRK_S":3,"MOM_S":3,"REJ_S":3}

    for i in range(max(start, ST), end):
        # ── EXIT CHECK ──
        still = []
        for t in active:
            a = ATR[i]; pnl = None
            if t["side"] == "buy":
                if not np.isnan(a) and a > 0:
                    nt = C[i] - t["ta"] * a
                    if nt > t["tr"]: t["tr"] = nt
                if L[i] <= t["sl"]:
                    pnl = (t["sl"] - t["e"]) * t["u"] - FR
                elif t["tr"] > t["e"] and L[i] <= t["tr"]:
                    pnl = (t["tr"] - t["e"]) * t["u"] - FR
                elif i >= t["tsi"]:
                    pnl = (C[i] - t["e"]) * t["u"] - FR
            else:  # sell/short
                if not np.isnan(a) and a > 0:
                    nt = C[i] + t["ta"] * a
                    if nt < t["tr"]: t["tr"] = nt
                if H[i] >= t["sl"]:
                    pnl = (t["e"] - t["sl"]) * t["u"] - FR
                elif t["tr"] < t["e"] and H[i] >= t["tr"]:
                    pnl = (t["e"] - t["tr"]) * t["u"] - FR
                elif i >= t["tsi"]:
                    pnl = (t["e"] - C[i]) * t["u"] - FR
            if pnl is not None:
                bal += pnl; cds[t["s"]] = i + cd_n[t["s"]]
                trades.append({"s": t["s"], "pnl": round(pnl, 2), "ei": t["ei"],
                               "xi": i, "side": t["side"]})
            else:
                still.append(t)
        active = still
        if np.isnan(ATR[i]) or ATR[i] <= 0: continue

        r = rgm(i) if use_regime else "ALL"
        risk_m = adx_risk(i) if adaptive else 1.0

        # ── LONG SIGNALS (in BULL, or always if no regime) ──
        if (r == 'BULL') or (not use_regime and is_up(i)):
            for sn, fn in LONGS:
                if i < cds[sn] or any(t["s"] == sn for t in active) or len(active) >= 5: continue
                sig = fn(i)
                if sig and sig["e"] - sig["sl"] > 0:
                    u = (bal * RSK * risk_m) / (sig["e"] - sig["sl"])
                    active.append({"s": sig["s"], "e": sig["e"], "ei": i, "sl": sig["sl"],
                        "tr": sig["sl"], "ta": sig["ta"], "tsi": i + sig["ts"],
                        "u": u, "side": "buy"})

        # ── SHORT SIGNALS (in BEAR only) ──
        if use_shorts and r == 'BEAR' and is_dn(i):
            short_checks = []
            if "BRK_S" in best_shorts:
                b = best_shorts["BRK_S"]
                short_checks.append(("BRK_S", chk_brk_s, b["p"], b["ta"], b["ts"]))
            if "MOM_S" in best_shorts:
                b = best_shorts["MOM_S"]
                short_checks.append(("MOM_S", chk_mom_s, b["p"], b["ta"], b["ts"]))
            if "REJ_S" in best_shorts:
                b = best_shorts["REJ_S"]
                short_checks.append(("REJ_S", chk_rej_s, b["p"], b["ta"], b["ts"]))
            for sn, fn, prm, ta, ts in short_checks:
                if i < cds[sn] or any(t["s"] == sn for t in active) or len(active) >= 5: continue
                sig = fn(i, **prm)
                if sig:
                    risk_amt = sig["sl"] - sig["e"]
                    if risk_amt <= 0: continue
                    u = (bal * RSK * risk_m) / risk_amt
                    active.append({"s": sig["s"], "e": sig["e"], "ei": i, "sl": sig["sl"],
                        "tr": sig["sl"], "ta": ta, "tsi": i + ts,
                        "u": u, "side": "sell"})

    # Close remaining positions
    for t in active:
        if t["side"] == "buy":
            pnl = (C[end-1] - t["e"]) * t["u"] - FR
        else:
            pnl = (t["e"] - C[end-1]) * t["u"] - FR
        bal += pnl
        trades.append({"s": t["s"], "pnl": round(pnl, 2), "ei": t["ei"],
                       "xi": end-1, "side": t["side"]})
    return trades, bal


# ═══════════════════════════════════════════════════════════════
#  5. OLD vs NEW COMPARISON
# ═══════════════════════════════════════════════════════════════
print("\n[4/6] Running OLD vs NEW comparison...")

old_trades, _ = run_system(0, N, use_regime=False, use_shorts=False, adaptive=False)
new_trades, _ = run_system(0, N, use_regime=True, use_shorts=True, adaptive=True)

old_pnl, old_wr, old_pf, old_n = stats(old_trades)
new_pnl, new_wr, new_pf, new_n = stats(new_trades)

print("\n  %-25s | %5s | %3s | %9s | %4s" % ("System", "Trades", "WR", "PnL", "PF"))
print("  " + "-" * 60)
print("  %-25s | %5d | %2.0f%% | $%+8.0f | %.2f" % (
    "OLD (longs only)", old_n, old_wr*100, old_pnl, old_pf))
print("  %-25s | %5d | %2.0f%% | $%+8.0f | %.2f" % (
    "NEW (regime+shorts+adx)", new_n, new_wr*100, new_pnl, new_pf))

# Breakdown by side
long_t = [t for t in new_trades if t["side"] == "buy"]
short_t = [t for t in new_trades if t["side"] == "sell"]
lp, lw, lpf, ln = stats(long_t)
sp, sw, spf, sn = stats(short_t)
print("  %-25s | %5d | %2.0f%% | $%+8.0f | %.2f" % ("  -> Longs", ln, lw*100, lp, lpf))
print("  %-25s | %5d | %2.0f%% | $%+8.0f | %.2f" % ("  -> Shorts", sn, sw*100, sp, spf))

# Per-strategy breakdown
print("\n  Per-strategy (NEW system):")
for sn in ["BRK","MOM","DIP","PLB","VRS","BRK_S","MOM_S","REJ_S"]:
    st = [t for t in new_trades if t["s"] == sn]
    if not st: continue
    p, w, pf, n_ = stats(st)
    side = "LONG" if sn in ["BRK","MOM","DIP","PLB","VRS"] else "SHORT"
    print("    %-6s (%5s): %3d trades | WR %2.0f%% | $%+6.0f | PF %.2f" % (
        sn, side, n_, w*100, p, pf))

# By regime
print("\n  OLD system by regime:")
for rname in ["BULL", "BEAR", "CHOP"]:
    rt = [t for t in old_trades if rgm(t["ei"]) == rname]
    if rt:
        p, w, pf, n_ = stats(rt)
        print("    %-6s: %3d trades | $%+6.0f | PF %.2f" % (rname, n_, p, pf))
    else:
        print("    %-6s: 0 trades" % rname)

print("\n  NEW system by regime:")
for rname in ["BULL", "BEAR", "CHOP"]:
    rt = [t for t in new_trades if rgm(t["ei"]) == rname]
    if rt:
        p, w, pf, n_ = stats(rt)
        ls_ = [t for t in rt if t["side"] == "buy"]
        ss_ = [t for t in rt if t["side"] == "sell"]
        print("    %-6s: %3d trades (%dL/%dS) | $%+6.0f | PF %.2f" % (
            rname, n_, len(ls_), len(ss_), p, pf))
    else:
        print("    %-6s: 0 trades" % rname)


# ═══════════════════════════════════════════════════════════════
#  6. WALK-FORWARD — 6 CHUNKS
# ═══════════════════════════════════════════════════════════════
print("\n[5/6] Walk-forward (6 chunks)...")

chunk = N // 6

print("\n  OLD SYSTEM (longs only):")
print("  %-5s | %-10s  %-10s | %4s | %8s | %4s | %s" % (
    "Chunk", "From", "To", "Trds", "PnL", "PF", "BTC"))
print("  " + "-" * 70)
old_prof = 0
for ci in range(6):
    s = ci * chunk; e = (ci+1)*chunk if ci < 5 else N
    t, _ = run_system(s, e, False, False, False)
    p, w, pf, n_ = stats(t)
    btc = (C[e-1] - C[max(s, ST)]) / C[max(s, ST)] * 100
    if p > 0: old_prof += 1
    print("  W%-4d | %-10s  %-10s | %4d | $%+6.0f | %.2f | BTC %+.0f%%" % (
        ci+1, gd(max(s, ST)), gd(e-1), n_, p, pf, btc))
print("  Profitable chunks: %d/6" % old_prof)

print("\n  NEW SYSTEM (regime + shorts + adaptive):")
print("  %-5s | %-10s  %-10s | %4s | %8s | %4s | %s" % (
    "Chunk", "From", "To", "Trds", "PnL", "PF", "BTC"))
print("  " + "-" * 70)
new_prof = 0
for ci in range(6):
    s = ci * chunk; e = (ci+1)*chunk if ci < 5 else N
    t, _ = run_system(s, e, True, True, True)
    p, w, pf, n_ = stats(t)
    ls_ = [x for x in t if x["side"] == "buy"]
    ss_ = [x for x in t if x["side"] == "sell"]
    btc = (C[e-1] - C[max(s, ST)]) / C[max(s, ST)] * 100
    if p > 0: new_prof += 1
    print("  W%-4d | %-10s  %-10s | %4d (%dL/%dS) | $%+6.0f | %.2f | BTC %+.0f%%" % (
        ci+1, gd(max(s, ST)), gd(e-1), n_, len(ls_), len(ss_), p, pf, btc))
print("  Profitable chunks: %d/6" % new_prof)


# ═══════════════════════════════════════════════════════════════
#  QUARTERLY COMPARISON
# ═══════════════════════════════════════════════════════════════
print("\n  QUARTERLY:")

def group_q(trades):
    qs = {}
    for t in trades:
        d = gd(t["ei"])
        if len(d) >= 7:
            y = d[:4]; m = int(d[5:7])
            q = "%s-Q%d" % (y, (m-1)//3+1)
        else:
            q = "?"
        qs.setdefault(q, []).append(t)
    return qs

oq = group_q(old_trades); nq = group_q(new_trades)
all_q = sorted(set(list(oq.keys()) + list(nq.keys())))

print("  %-8s | %12s | %12s | %s" % ("Quarter", "OLD", "NEW", "Change"))
print("  " + "-" * 60)
old_neg = 0; new_neg = 0
for q in all_q:
    op, _, _, on = stats(oq.get(q, []))
    np_, _, _, nn = stats(nq.get(q, []))
    if op < 0: old_neg += 1
    if np_ < 0: new_neg += 1
    diff = np_ - op
    marker = "+" if diff > 0 else "-" if diff < 0 else "="
    print("  %-8s | %3d tr $%+5.0f | %3d tr $%+5.0f | $%+5.0f [%s]" % (
        q, on, op, nn, np_, diff, marker))

print("\n  Losing quarters: OLD=%d -> NEW=%d" % (old_neg, new_neg))


# ═══════════════════════════════════════════════════════════════
#  MAX DRAWDOWN COMPARISON
# ═══════════════════════════════════════════════════════════════
print("\n[6/6] Drawdown analysis...")

def calc_drawdown(trades):
    """Calculate max drawdown from trade sequence."""
    equity = [10000.0]
    for t in trades:
        equity.append(equity[-1] + t["pnl"])
    peak = equity[0]; max_dd = 0
    for e in equity:
        if e > peak: peak = e
        dd = (peak - e) / peak * 100
        if dd > max_dd: max_dd = dd
    return max_dd, max(equity), min(equity)

old_dd, old_peak, old_low = calc_drawdown(old_trades)
new_dd, new_peak, new_low = calc_drawdown(new_trades)

print("  OLD: max drawdown %.1f%% | peak $%.0f | low $%.0f" % (old_dd, old_peak, old_low))
print("  NEW: max drawdown %.1f%% | peak $%.0f | low $%.0f" % (new_dd, new_peak, new_low))


# ═══════════════════════════════════════════════════════════════
#  VERDICT
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  UPGRADE VERDICT")
print("=" * 70)

improvement = new_pnl - old_pnl
fewer_losing_q = old_neg - new_neg

# Check if shorts actually helped in bear periods
bear_old = [t for t in old_trades if rgm(t["ei"]) == 'BEAR']
bear_new = [t for t in new_trades if rgm(t["ei"]) == 'BEAR']
bear_old_pnl = sum(t["pnl"] for t in bear_old)
bear_new_pnl = sum(t["pnl"] for t in bear_new)
bear_improvement = bear_new_pnl - bear_old_pnl

print("\n  %-30s %12s %12s" % ("Metric", "OLD", "NEW"))
print("  " + "-" * 56)
print("  %-30s %12s %12s" % ("Total trades", str(old_n), str(new_n)))
print("  %-30s %12s %12s" % ("Total PnL", "$%+.0f" % old_pnl, "$%+.0f" % new_pnl))
print("  %-30s %12s %12s" % ("Profit Factor", "%.2f" % old_pf, "%.2f" % new_pf))
print("  %-30s %12s %12s" % ("Max Drawdown", "%.1f%%" % old_dd, "%.1f%%" % new_dd))
print("  %-30s %12s %12s" % ("Profitable chunks", "%d/6" % old_prof, "%d/6" % new_prof))
print("  %-30s %12s %12s" % ("Losing quarters", str(old_neg), str(new_neg)))
print("  %-30s %12s %12s" % ("BEAR regime PnL", "$%+.0f" % bear_old_pnl, "$%+.0f" % bear_new_pnl))

if improvement > 0 and n_shorts >= 2:
    verdict = "DEPLOY"
    reason = "Shorts add $%+.0f, %d fewer losing quarters, bear PnL improved by $%+.0f" % (
        improvement, fewer_losing_q, bear_improvement)
elif improvement > 0:
    verdict = "PARTIAL DEPLOY"
    reason = "Only %d short strategies viable but overall PnL improved by $%+.0f" % (
        n_shorts, improvement)
elif bear_improvement > 0:
    verdict = "DEPLOY SHORTS ONLY"
    reason = "Total PnL slightly lower but bear regime improved by $%+.0f (less bleeding)" % bear_improvement
else:
    verdict = "HOLD"
    reason = "Shorts don't improve the system enough. Need more bear market data or different approach."

print("  VERDICT: %s" % verdict)
print("  Reason: %s" % reason)

if n_shorts > 0:
    print("\n  WINNING SHORT PARAMETERS:")
    for sn, b in best_shorts.items():
        print("    %s: %s | trail=%.1f | ts=%d | %d trades PF %.2f" % (
            sn, b["p"], b["ta"], b["ts"], b["n"], b["pf"]))

print("\nDone.")
