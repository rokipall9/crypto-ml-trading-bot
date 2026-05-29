"""
MULTI-SYMBOL BACKTEST — "Can altcoins be added?"
=================================================
Tests S1 (BREAKOUT) + S2 (VOL_MOMENTUM) only — the 5/5 validated pair.
Runs the same Mode E BULL-gate engine on 8 symbols (BTC baseline + 7 alts).

Decision rules (per user/teacher spec):
  PF > 1.3  AND  trades >= 30  AND  MaxDD < 25%  = PASS (expand to symbol)
  otherwise = FAIL (keep off)

If ≥3 alts pass: green-light symbol expansion
If 0–2 pass:     stay BTC-only
"""
import sys, warnings, time
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, "/home/ubuntu/bot")
import market_data

# ── Symbols to test ──────────────────────────────────────────────────
SYMBOLS = [
    "BTCUSDT",   # baseline (should match prior $45k Mode E pnl pro-rated for 2 strategies only)
    "ETHUSDT",   # liquid second-largest
    "SOLUSDT",
    "BNBUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "ATOMUSDT",
    "ARBUSDT",
    "SUIUSDT",
]

# Pass thresholds
MIN_TRADES = 30
MIN_PF = 1.3
MAX_DD = 25.0

# Shared params
ST = 60           # warm-up bars (indicator lag)
FR = 20           # flat fee per trade in dollars (round-trip ~$20)
RSK = 0.01        # 1% risk per trade


def build_indicators(df):
    """Compute all indicators for a symbol's OHLCV dataframe."""
    n = len(df)
    H = df["high"].values.astype(float)
    L = df["low"].values.astype(float)
    C = df["close"].values.astype(float)
    O = df["open"].values.astype(float)
    V = df["volume"].values.astype(float)
    dates = df["open_time"].values if "open_time" in df.columns else [None]*n

    def ema(a, p): return pd.Series(a).ewm(span=p, adjust=False).mean().values
    def atr_func(h, l, c, p=14):
        tr = np.zeros(len(h))
        for i in range(1, len(h)):
            tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        return pd.Series(tr).rolling(p, min_periods=p).mean().values

    e21 = ema(C, 21); e50 = ema(C, 50); e55 = ema(C, 55)
    ATR = atr_func(H, L, C, 14)
    vol20 = pd.Series(V).rolling(20, min_periods=20).mean().values
    bd = np.abs(C - O)
    bd20 = pd.Series(bd).rolling(20, min_periods=20).mean().values
    hi30 = pd.Series(H).rolling(30, min_periods=30).max().values

    slp = np.zeros(n)
    for i in range(6, n):
        if e50[i-6] > 0:
            slp[i] = (e50[i] - e50[i-6]) / e50[i-6] * 100

    return {
        "n": n, "H": H, "L": L, "C": C, "O": O, "V": V, "dates": dates,
        "e21": e21, "e55": e55, "ATR": ATR, "vol20": vol20, "bd": bd,
        "bd20": bd20, "hi30": hi30, "slp": slp,
    }


def run_s1_s2_backtest(ind):
    """Run Mode E BULL-only + S1 (BRK) + S2 (MOM) strategies."""
    n, H, L, C, O, V = ind["n"], ind["H"], ind["L"], ind["C"], ind["O"], ind["V"]
    e21, e55, ATR = ind["e21"], ind["e55"], ind["ATR"]
    vol20, bd, bd20 = ind["vol20"], ind["bd"], ind["bd20"]
    hi30, slp = ind["hi30"], ind["slp"]

    def is_up(i): return e21[i] > e55[i] and slp[i] > 0.1

    def chk_brk(i):
        if np.isnan(hi30[i]) or np.isnan(vol20[i]) or vol20[i] <= 0: return None
        if C[i] <= hi30[i-1] or V[i] < vol20[i]*1.5 or C[i] <= O[i]: return None
        return {"s": "BRK", "e": C[i], "sl": C[i]-1.5*ATR[i], "ta": 3.0, "ts": 48}

    def chk_mom(i):
        if np.isnan(bd20[i]) or bd20[i] <= 0 or np.isnan(vol20[i]) or vol20[i] <= 0: return None
        if C[i] <= O[i] or bd[i] < bd20[i]*2 or V[i] < vol20[i]*2 or C[i] <= e21[i]: return None
        return {"s": "MOM", "e": C[i], "sl": C[i]-1.5*ATR[i], "ta": 3.0, "ts": 48}

    STRATS = [("BRK", chk_brk), ("MOM", chk_mom)]

    bal = 10000.0
    active = []
    trades = []
    cds = {"BRK": 0, "MOM": 0}
    cd_n = {"BRK": 3, "MOM": 3}

    for i in range(ST, n):
        # Exit logic (LONG only, Mode E without shorts for this S1/S2 test)
        still = []
        for t in active:
            a = ATR[i]
            pnl = None
            if not np.isnan(a) and a > 0:
                nt = C[i] - t["ta"] * a
                if nt > t["tr"]: t["tr"] = nt
            if L[i] <= t["sl"]:
                pnl = (t["sl"] - t["e"]) * t["u"] - FR
            elif t["tr"] > t["e"] and L[i] <= t["tr"]:
                pnl = (t["tr"] - t["e"]) * t["u"] - FR
            elif i >= t["tsi"]:
                pnl = (C[i] - t["e"]) * t["u"] - FR
            if pnl is not None:
                bal += pnl
                cds[t["s"]] = i + cd_n[t["s"]]
                trades.append({"s": t["s"], "pnl": round(pnl, 2),
                               "ei": t["ei"], "xi": i, "side": "buy"})
            else:
                still.append(t)
        active = still

        if np.isnan(ATR[i]) or ATR[i] <= 0:
            continue

        # Entry gate: BULL only
        if not is_up(i):
            continue

        # Try each strategy
        for sn, fn in STRATS:
            if i < cds[sn] or any(t["s"] == sn for t in active) or len(active) >= 5:
                continue
            sig = fn(i)
            if sig and sig["e"] - sig["sl"] > 0:
                u = (bal * RSK) / (sig["e"] - sig["sl"])
                active.append({"s": sig["s"], "e": sig["e"], "ei": i,
                               "sl": sig["sl"], "tr": sig["sl"], "ta": sig["ta"],
                               "tsi": i + sig["ts"], "u": u})

    # Close any remaining positions at final bar
    for t in active:
        pnl = (C[n-1] - t["e"]) * t["u"] - FR
        bal += pnl
        trades.append({"s": t["s"], "pnl": round(pnl, 2),
                       "ei": t["ei"], "xi": n-1, "side": "buy"})

    return trades, bal


def calc_stats(trades):
    if not trades:
        return {"pnl": 0, "wr": 0, "pf": 0, "n": 0, "dd": 0}
    pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = wins / len(trades)
    ws = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    ls = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = ws / ls if ls > 0 else 0

    # Drawdown
    eq = [10000.0]
    for t in trades:
        eq.append(eq[-1] + t["pnl"])
    peak = eq[0]
    mx_dd = 0
    for e in eq:
        if e > peak: peak = e
        dd = (peak - e) / peak * 100
        if dd > mx_dd: mx_dd = dd

    return {"pnl": pnl, "wr": wr, "pf": pf, "n": len(trades), "dd": mx_dd}


def verdict(st):
    """Pass if: >=30 trades, PF >= 1.3, DD <= 25%"""
    if st["n"] < MIN_TRADES:
        return "FAIL (too few trades)"
    if st["pf"] < MIN_PF:
        return "FAIL (PF < %.1f)" % MIN_PF
    if st["dd"] > MAX_DD:
        return "FAIL (DD > %.0f%%)" % MAX_DD
    return "PASS"


# =====================================================================
# MAIN
# =====================================================================
print("=" * 75)
print("  MULTI-SYMBOL BACKTEST — S1+S2 ONLY (5/5 validated pair)")
print("=" * 75)
print("  Strategies: BREAKOUT_4H, VOL_MOMENTUM_4H")
print("  Gate: BULL only (EMA21>55, slope>0.1%%)")
print("  Pass rule: trades>=%d  PF>=%.1f  MaxDD<=%.0f%%" % (MIN_TRADES, MIN_PF, MAX_DD))
print()

rows = []
for sym in SYMBOLS:
    t0 = time.time()
    try:
        df = market_data.download(sym, "4h", total_candles=5000)
    except Exception as e:
        print("  %-12s  DOWNLOAD FAILED: %s" % (sym, e))
        rows.append((sym, None, None, None, None, None, None, "FAIL (no data)"))
        continue

    if len(df) < 500:
        print("  %-12s  INSUFFICIENT DATA (%d bars)" % (sym, len(df)))
        rows.append((sym, len(df), 0, 0, 0, 0, 0, "FAIL (insufficient data)"))
        continue

    ind = build_indicators(df)
    trades, final_bal = run_s1_s2_backtest(ind)
    st = calc_stats(trades)
    v = verdict(st)

    dt = time.time() - t0
    print("  %-12s  bars=%4d  trades=%3d  pnl=$%+8.0f  PF=%5.2f  WR=%5.1f%%  DD=%5.1f%%  [%s]  (%.1fs)" % (
        sym, ind["n"], st["n"], st["pnl"], st["pf"], st["wr"]*100, st["dd"], v, dt))
    rows.append((sym, ind["n"], st["n"], st["pnl"], st["pf"], st["wr"], st["dd"], v))


print()
print("=" * 75)
print("  SUMMARY TABLE")
print("=" * 75)
print("  %-12s %7s %7s %10s %6s %7s %7s   %s" % (
    "SYMBOL", "BARS", "TRADES", "PNL", "PF", "WR", "MaxDD", "VERDICT"))
print("  " + "-" * 72)
for r in rows:
    sym, bars, n, pnl, pf, wr, dd, v = r
    if n is None:
        print("  %-12s  no data" % sym)
    elif n == 0:
        print("  %-12s %7d     0         -      -       -       -   %s" % (sym, bars, v))
    else:
        print("  %-12s %7d %7d $%+9.0f %6.2f %6.1f%% %6.1f%%   %s" % (
            sym, bars, n, pnl, pf, wr*100, dd, v))

# Decision
passed_alts = [r for r in rows if r[0] != "BTCUSDT" and r[-1] == "PASS"]
btc_row = next((r for r in rows if r[0] == "BTCUSDT"), None)

print()
print("=" * 75)
print("  DECISION")
print("=" * 75)
if btc_row and btc_row[-1] == "PASS":
    print("  BTC baseline: PASS (sanity check — engine works)")
else:
    print("  BTC baseline: FAIL or missing — REVIEW ENGINE before trusting alt results")

print()
print("  Altcoins passing: %d / %d" % (len(passed_alts), len(SYMBOLS)-1))
if passed_alts:
    print("  -> Expand to: " + ", ".join(r[0] for r in passed_alts))
    if len(passed_alts) >= 3:
        print("  -> GREEN LIGHT on symbol expansion")
    else:
        print("  -> LIMITED expansion (add only these)")
else:
    print("  -> NO altcoins pass. Stay BTC-only. Strategy contamination elsewhere?")
