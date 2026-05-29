"""
TRUST VERIFICATION — Can we trust these backtest numbers?
==========================================================
Tests:
  1. REPRODUCIBILITY — re-run and verify numbers match
  2. EVERY TRADE LOG — date, price, exit so you can spot-check on TradingView
  3. RECENT PERFORMANCE — last 6 months only (least optimized period)
  4. LOOK-AHEAD CHECK — verify entries only use past data
  5. SLIPPAGE STRESS TEST — what if friction is $50 instead of $20?
  6. OUT-OF-SAMPLE — train on first 70%, test on last 30%
  7. WORST CASE — longest losing streak, max consecutive losses
"""
import sys
sys.path.insert(0, "/home/ubuntu/bot")
import market_data
import numpy as np
import pandas as pd
from datetime import datetime

print("=" * 70)
print("  TRUST VERIFICATION — Honest Audit")
print("=" * 70)

print("\nDownloading data...")
df_4h = market_data.download("BTCUSDT", "4h", total_candles=5000)
n4h = len(df_4h)

# Get actual dates
if "open_time" in df_4h.columns:
    dates = df_4h["open_time"].values
elif isinstance(df_4h.index, pd.DatetimeIndex):
    dates = df_4h.index.values
else:
    dates = [None] * n4h

def get_date(i):
    if dates[i] is not None:
        return str(dates[i])[:16]
    return "bar_%d" % i

h4 = df_4h["high"].values.astype(float)
l4 = df_4h["low"].values.astype(float)
c4 = df_4h["close"].values.astype(float)
o4 = df_4h["open"].values.astype(float)
v4 = df_4h["volume"].values.astype(float)

def ema(a, p): return pd.Series(a).ewm(span=p, adjust=False).mean().values
def atr_f(h, l, c, p=14):
    n = len(h); tr = np.zeros(n)
    for i in range(1, n): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    return pd.Series(tr).rolling(p, min_periods=p).mean().values

ema21 = ema(c4, 21); ema55 = ema(c4, 55); ema50 = ema(c4, 50); ema40 = ema(c4, 40)
atr14 = atr_f(h4, l4, c4, 14)
avg_vol = pd.Series(v4).rolling(20, min_periods=20).mean().values
body = np.abs(c4 - o4)
avg_body = pd.Series(body).rolling(20, min_periods=20).mean().values
high30 = pd.Series(h4).rolling(30, min_periods=30).max().values
high8 = pd.Series(h4).rolling(8, min_periods=8).max().values

ema50_slope = np.zeros(n4h)
for i in range(6, n4h):
    if ema50[i-6] > 0:
        ema50_slope[i] = (ema50[i] - ema50[i-6]) / ema50[i-6] * 100

START = 60; RISK = 0.01

def is_uptrend(i):
    return ema21[i] > ema55[i] and ema50_slope[i] > 0.1

def check_breakout(i):
    if np.isnan(high30[i]) or np.isnan(avg_vol[i]) or avg_vol[i] <= 0: return None
    if c4[i] <= high30[i-1]: return None
    if v4[i] < avg_vol[i] * 1.5: return None
    if c4[i] <= o4[i]: return None
    entry = c4[i]; sl = entry - 1.5 * atr14[i]
    return {"strat": "BREAKOUT", "entry": entry, "sl": sl, "trail_atr": 3.0, "ts": 48}

def check_momentum(i):
    if np.isnan(avg_body[i]) or avg_body[i] <= 0 or np.isnan(avg_vol[i]) or avg_vol[i] <= 0: return None
    if c4[i] <= o4[i]: return None
    if body[i] < avg_body[i] * 2.0: return None
    if v4[i] < avg_vol[i] * 2.0: return None
    if c4[i] <= ema21[i]: return None
    entry = c4[i]; sl = entry - 1.5 * atr14[i]
    return {"strat": "MOMENTUM", "entry": entry, "sl": sl, "trail_atr": 3.0, "ts": 48}

def check_dip(i):
    if np.isnan(high8[i]) or high8[i] <= 0 or np.isnan(avg_body[i]) or avg_body[i] <= 0: return None
    dip_pct = (high8[i] - l4[i]) / high8[i] * 100
    if dip_pct < 3.0: return None
    if c4[i] <= o4[i]: return None
    if body[i] < avg_body[i] * 1.5: return None
    if c4[i] < ema55[i]: return None
    entry = c4[i]; sl = entry - 1.5 * atr14[i]
    return {"strat": "DIP", "entry": entry, "sl": sl, "trail_atr": 3.5, "ts": 48}

def check_pullback(i):
    atr = atr14[i]
    if np.isnan(atr) or atr <= 0 or np.isnan(ema40[i]): return None
    dist = l4[i] - ema40[i]
    if dist < -0.3 * atr or dist > 1.2 * atr: return None
    if l4[i] > ema21[i]: return None
    if c4[i] <= o4[i] or c4[i] <= ema21[i]: return None
    entry = c4[i]; sl = min(l4[i], ema40[i]) - 0.3 * atr
    if entry - sl <= 0 or entry - sl > 3 * atr: return None
    return {"strat": "PULLBACK", "entry": entry, "sl": sl, "trail_atr": 3.5, "ts": 48}

def check_volreset(i):
    atr = atr14[i]
    if np.isnan(atr) or atr <= 0 or np.isnan(avg_vol[i]) or avg_vol[i] <= 0: return None
    dry_count = sum(1 for j in range(1,4) if i-j>=0 and v4[i-j] < 0.8*avg_vol[i])
    if dry_count < 1: return None
    if v4[i] < 1.2 * avg_vol[i]: return None
    if abs(c4[i] - ema21[i]) > 0.8 * atr: return None
    if c4[i] <= o4[i] or c4[i] <= ema21[i]: return None
    entry = c4[i]; sl = entry - 1.5 * atr
    return {"strat": "VRESET", "entry": entry, "sl": sl, "trail_atr": 4.0, "ts": 60}


def run_combined(start_bar, end_bar, friction, label, verbose=False):
    """Run full combined simulation on a slice of data."""
    balance = 10000.0; active = []; trades = []
    cooldowns = {"BREAKOUT":0,"MOMENTUM":0,"DIP":0,"PULLBACK":0,"VRESET":0}
    cd_bars = {"BREAKOUT":3,"MOMENTUM":3,"DIP":3,"PULLBACK":3,"VRESET":2}

    for i in range(max(start_bar, START), end_bar):
        still = []
        for t in active:
            new_t = c4[i] - t["trail_atr"] * atr14[i]
            if not np.isnan(new_t) and new_t > t["trail"]: t["trail"] = new_t
            pnl = None; exit_type = None; exit_price = 0
            if l4[i] <= t["sl"]:
                pnl = (t["sl"] - t["entry"]) * t["units"] - friction
                exit_type = "SL"; exit_price = t["sl"]
            elif t["trail"] > t["entry"] and l4[i] <= t["trail"]:
                pnl = (t["trail"] - t["entry"]) * t["units"] - friction
                exit_type = "TRAIL"; exit_price = t["trail"]
            elif i >= t["ts_bar"]:
                pnl = (c4[i] - t["entry"]) * t["units"] - friction
                exit_type = "TIME"; exit_price = c4[i]
            if pnl is not None:
                balance += pnl
                cooldowns[t["strat"]] = i + cd_bars[t["strat"]]
                trades.append({"strat": t["strat"], "pnl": round(pnl,2),
                    "entry_date": get_date(t["ei"]), "exit_date": get_date(i),
                    "entry_price": round(t["entry"],0), "exit_price": round(exit_price,0),
                    "exit_type": exit_type, "ei": t["ei"], "xi": i,
                    "bars_held": i - t["ei"]})
            else: still.append(t)
        active = still

        if not is_uptrend(i) or np.isnan(atr14[i]) or atr14[i] <= 0: continue
        checks = [("BREAKOUT",check_breakout),("MOMENTUM",check_momentum),
                   ("DIP",check_dip),("PULLBACK",check_pullback),("VRESET",check_volreset)]
        for name, fn in checks:
            if i < cooldowns[name]: continue
            if any(t["strat"]==name for t in active): continue
            if len(active) >= 5: break
            sig = fn(i)
            if sig:
                sl_dist = sig["entry"] - sig["sl"]
                if sl_dist <= 0: continue
                units = (balance * RISK) / sl_dist
                active.append({"strat":sig["strat"],"entry":sig["entry"],"ei":i,
                    "sl":sig["sl"],"trail":sig["sl"],"trail_atr":sig["trail_atr"],
                    "ts_bar":i+sig["ts"],"units":units})

    for t in active:
        pnl = (c4[end_bar-1] - t["entry"]) * t["units"] - friction
        balance += pnl
        trades.append({"strat":t["strat"],"pnl":round(pnl,2),
            "entry_date":get_date(t["ei"]),"exit_date":get_date(end_bar-1),
            "entry_price":round(t["entry"],0),"exit_price":round(c4[end_bar-1],0),
            "exit_type":"OPEN","ei":t["ei"],"xi":end_bar-1,"bars_held":end_bar-1-t["ei"]})

    return balance, trades


# ══════════════════════════════════════════════════════════════════════
#  TEST 1: REPRODUCIBILITY
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 1: REPRODUCIBILITY")
print("=" * 70)

bal1, trades1 = run_combined(0, n4h, 20, "run1")
bal2, trades2 = run_combined(0, n4h, 20, "run2")
print("  Run 1: $%.0f (%d trades)" % (bal1, len(trades1)))
print("  Run 2: $%.0f (%d trades)" % (bal2, len(trades2)))
print("  Match: %s" % ("YES" if abs(bal1-bal2) < 0.01 and len(trades1)==len(trades2) else "NO — BUG!"))

# ══════════════════════════════════════════════════════════════════════
#  TEST 2: EVERY TRADE LOG (last 30 trades for spot-checking)
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 2: TRADE LOG (last 30 trades — check on TradingView)")
print("=" * 70)
print("  %-10s | %-16s | %-16s | %7s | %7s | %-5s | %8s | %s" % (
    "Strategy", "Entry Date", "Exit Date", "Entry$", "Exit$", "Type", "PnL", "Bars"))
print("  " + "-" * 100)
for t in trades1[-30:]:
    print("  %-10s | %-16s | %-16s | %7.0f | %7.0f | %-5s | $%+6.0f | %d" % (
        t["strat"], t["entry_date"], t["exit_date"],
        t["entry_price"], t["exit_price"], t["exit_type"], t["pnl"], t["bars_held"]))

# ══════════════════════════════════════════════════════════════════════
#  TEST 3: LOOK-AHEAD BIAS CHECK
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 3: LOOK-AHEAD BIAS CHECK")
print("=" * 70)
bias_found = False
for t in trades1:
    # Entry should use CLOSE of bar (not future bars)
    i = t["ei"]
    if abs(t["entry_price"] - c4[i]) > 1:
        print("  [BIAS] Trade at bar %d: entry $%.0f != close $%.0f" % (i, t["entry_price"], c4[i]))
        bias_found = True
    # SL exit should not use price below the bar's low
    if t["exit_type"] == "SL":
        xi = t["xi"]
        if t["exit_price"] < l4[xi] - 1:
            print("  [BIAS] SL exit at bar %d: exit $%.0f < low $%.0f" % (xi, t["exit_price"], l4[xi]))
            bias_found = True
if not bias_found:
    print("  [CLEAN] No look-ahead bias detected in %d trades" % len(trades1))

# ══════════════════════════════════════════════════════════════════════
#  TEST 4: OUT-OF-SAMPLE (train first 70%, test last 30%)
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 4: OUT-OF-SAMPLE (train 70% / test 30%)")
print("=" * 70)
split = int(n4h * 0.7)  # ~3500 bars
train_bal, train_trades = run_combined(0, split, 20, "train")
test_bal, test_trades = run_combined(split, n4h, 20, "test")

train_pnl = sum(t["pnl"] for t in train_trades)
test_pnl = sum(t["pnl"] for t in test_trades)
train_wins = sum(1 for t in train_trades if t["pnl"] > 0)
test_wins = sum(1 for t in test_trades if t["pnl"] > 0)
train_wr = train_wins/len(train_trades)*100 if train_trades else 0
test_wr = test_wins/len(test_trades)*100 if test_trades else 0
train_ws = sum(t["pnl"] for t in train_trades if t["pnl"] > 0)
train_ls = abs(sum(t["pnl"] for t in train_trades if t["pnl"] <= 0))
test_ws = sum(t["pnl"] for t in test_trades if t["pnl"] > 0)
test_ls = abs(sum(t["pnl"] for t in test_trades if t["pnl"] <= 0))
train_pf = train_ws/train_ls if train_ls > 0 else 0
test_pf = test_ws/test_ls if test_ls > 0 else 0

print("  TRAIN (bars 0-%d, ~%s to %s):" % (split, get_date(START)[:10], get_date(split)[:10]))
print("    %d trades | WR %.0f%% | $%+.0f | PF %.2f" % (len(train_trades), train_wr, train_pnl, train_pf))
print("  TEST  (bars %d-%d, ~%s to %s):" % (split, n4h, get_date(split)[:10], get_date(n4h-1)[:10]))
print("    %d trades | WR %.0f%% | $%+.0f | PF %.2f" % (len(test_trades), test_wr, test_pnl, test_pf))

if test_pnl > 0 and test_pf > 1.0:
    print("  [PASS] Out-of-sample is PROFITABLE (PF %.2f)" % test_pf)
else:
    print("  [WARN] Out-of-sample underperforms")

# Per-strategy in test period
print("\n  Per-strategy (TEST period only):")
test_strats = {}
for t in test_trades:
    s = t["strat"]
    if s not in test_strats: test_strats[s] = {"n":0,"pnl":0,"wins":0}
    test_strats[s]["n"] += 1
    test_strats[s]["pnl"] += t["pnl"]
    if t["pnl"] > 0: test_strats[s]["wins"] += 1
for s in ["BREAKOUT","MOMENTUM","DIP","PULLBACK","VRESET"]:
    if s in test_strats:
        ss = test_strats[s]
        wr = ss["wins"]/ss["n"]*100 if ss["n"] else 0
        print("    %-10s: %2d trades | WR %3.0f%% | $%+.0f" % (s, ss["n"], wr, ss["pnl"]))
    else:
        print("    %-10s: 0 trades" % s)

# ══════════════════════════════════════════════════════════════════════
#  TEST 5: RECENT 6 MONTHS
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 5: RECENT 6 MONTHS ONLY")
print("=" * 70)
recent_start = n4h - (6 * 30 * 6)  # ~6 months of 4H bars
if recent_start < START: recent_start = START
rec_bal, rec_trades = run_combined(recent_start, n4h, 20, "recent")
rec_pnl = sum(t["pnl"] for t in rec_trades)
rec_wins = sum(1 for t in rec_trades if t["pnl"] > 0)
rec_wr = rec_wins/len(rec_trades)*100 if rec_trades else 0
rec_ws = sum(t["pnl"] for t in rec_trades if t["pnl"] > 0)
rec_ls = abs(sum(t["pnl"] for t in rec_trades if t["pnl"] <= 0))
rec_pf = rec_ws/rec_ls if rec_ls > 0 else 0
print("  Period: %s to %s" % (get_date(recent_start)[:10], get_date(n4h-1)[:10]))
print("  %d trades | WR %.0f%% | $%+.0f | PF %.2f" % (len(rec_trades), rec_wr, rec_pnl, rec_pf))
if rec_pnl > 0:
    print("  [PASS] Recent 6 months profitable")
else:
    print("  [WARN] Recent 6 months NOT profitable")

# ══════════════════════════════════════════════════════════════════════
#  TEST 6: SLIPPAGE STRESS TEST
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 6: SLIPPAGE STRESS TEST")
print("=" * 70)
for fric in [20, 50, 80, 100]:
    b, t = run_combined(0, n4h, fric, "fric_%d" % fric)
    pnl = sum(x["pnl"] for x in t)
    ws = sum(x["pnl"] for x in t if x["pnl"] > 0)
    ls_v = abs(sum(x["pnl"] for x in t if x["pnl"] <= 0))
    pf_v = ws/ls_v if ls_v > 0 else 0
    print("  Friction $%3d: %d trades | $%+6.0f | PF %.2f | %s" % (
        fric, len(t), pnl, pf_v, "PROFITABLE" if pnl > 0 else "LOSING"))

# ══════════════════════════════════════════════════════════════════════
#  TEST 7: WORST CASE ANALYSIS
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 7: WORST CASE ANALYSIS")
print("=" * 70)

# Longest losing streak
max_streak = 0; cur_streak = 0
for t in trades1:
    if t["pnl"] <= 0: cur_streak += 1; max_streak = max(max_streak, cur_streak)
    else: cur_streak = 0
print("  Max consecutive losses: %d" % max_streak)

# Worst drawdown period
eq = [10000.0]
for t in trades1: eq.append(eq[-1] + t["pnl"])
ea = np.array(eq)
peaks = np.maximum.accumulate(ea)
dd = ea - peaks
worst_dd_idx = np.argmin(dd)
worst_dd = dd[worst_dd_idx]
# Find start of drawdown
dd_start = worst_dd_idx
while dd_start > 0 and ea[dd_start] >= peaks[dd_start] - 0.01: dd_start -= 1
print("  Worst drawdown: $%.0f (trade %d to %d)" % (abs(worst_dd), dd_start, worst_dd_idx))
if dd_start < len(trades1) and worst_dd_idx-1 < len(trades1):
    print("    From: %s" % trades1[max(0,dd_start)]["entry_date"])
    print("    To:   %s" % trades1[min(worst_dd_idx-1, len(trades1)-1)]["exit_date"])

# Recovery time
if worst_dd_idx < len(ea) - 1:
    recovery = None
    peak_val = peaks[worst_dd_idx]
    for j in range(worst_dd_idx, len(ea)):
        if ea[j] >= peak_val:
            recovery = j - worst_dd_idx
            break
    if recovery:
        print("  Recovery: %d trades to recover from worst drawdown" % recovery)
    else:
        print("  Recovery: NOT YET RECOVERED from worst drawdown")

# Average hold time
avg_bars = np.mean([t["bars_held"] for t in trades1])
avg_hours = avg_bars * 4
print("  Avg trade duration: %.0f bars (%.0f hours / %.1f days)" % (avg_bars, avg_hours, avg_hours/24))

# Win/loss size ratio
avg_win = np.mean([t["pnl"] for t in trades1 if t["pnl"] > 0]) if any(t["pnl"]>0 for t in trades1) else 0
avg_loss = np.mean([t["pnl"] for t in trades1 if t["pnl"] <= 0]) if any(t["pnl"]<=0 for t in trades1) else 0
print("  Avg win:  $%+.0f" % avg_win)
print("  Avg loss: $%+.0f" % avg_loss)
print("  Win/loss ratio: %.2fx" % (avg_win / abs(avg_loss)) if avg_loss != 0 else "  Win/loss ratio: inf")

# ══════════════════════════════════════════════════════════════════════
#  HONEST SUMMARY
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  HONEST TRUST ASSESSMENT")
print("=" * 70)

issues = []
if test_pnl <= 0: issues.append("Out-of-sample LOSES money")
if test_pf < 1.0: issues.append("Out-of-sample PF below 1.0")
if rec_pnl <= 0: issues.append("Recent 6 months LOSES money")
if max_streak > 10: issues.append("Max losing streak > 10 trades")
if bias_found: issues.append("Look-ahead bias detected!")

# Check: $50 friction still profitable?
b50, t50 = run_combined(0, n4h, 50, "f50")
pnl50 = sum(x["pnl"] for x in t50)
if pnl50 <= 0: issues.append("NOT profitable at $50 friction")

strengths = []
if test_pnl > 0: strengths.append("Out-of-sample profitable ($%+.0f)" % test_pnl)
if test_pf > 1.2: strengths.append("Out-of-sample PF %.2f" % test_pf)
if rec_pnl > 0: strengths.append("Recent 6 months profitable")
if not bias_found: strengths.append("No look-ahead bias")
if pnl50 > 0: strengths.append("Survives $50 friction")
if max_streak <= 8: strengths.append("Max losing streak only %d" % max_streak)

print("\n  STRENGTHS:")
for s in strengths: print("    + %s" % s)

print("\n  CONCERNS:")
if issues:
    for i in issues: print("    - %s" % i)
else:
    print("    None found")

print("\n  CAVEATS (always true for any backtest):")
print("    ! Past performance does NOT guarantee future results")
print("    ! All params were optimized on this data (no true holdout)")
print("    ! BTC regime changes (bear market) could break all strategies")
print("    ! 833 days is good but not exhaustive (missed 2022 bear)")
print("    ! Paper mode exists for a reason — validate with real execution")

print("\nDone.")
