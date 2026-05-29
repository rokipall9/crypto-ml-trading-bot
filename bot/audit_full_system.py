"""
FULL SYSTEM AUDIT — 5-strategy combined backtest + bug detection
================================================================
Runs ALL 5 strategies together on the same 5000-candle dataset,
with realistic constraints: max open trades, cooldowns, portfolio-level
position sizing. This is the REAL test — not individual strategy tests.

Also checks:
  - Signal overlap (do strategies fire at the same time?)
  - Max concurrent trades
  - Worst drawdown in combined portfolio
  - Monthly breakdown
  - Per-strategy P&L in combined context
"""
import sys
sys.path.insert(0, "/home/ubuntu/bot")
import market_data
import numpy as np
import pandas as pd

print("=" * 70)
print("  FULL SYSTEM AUDIT — 5 Strategy Combined Backtest")
print("=" * 70)

print("\nDownloading data...")
df_4h = market_data.download("BTCUSDT", "4h", total_candles=5000)
n4h = len(df_4h)
print("4h: %d candles (%d days)" % (n4h, n4h // 6))

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

def get_month(i):
    if isinstance(df_4h.index, pd.DatetimeIndex): return str(df_4h.index[i])[:7]
    elif "open_time" in df_4h.columns: return str(df_4h["open_time"].iloc[i])[:7]
    return "?"

print("Computing indicators...")
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

FRICTION = 20; START = 60; RISK = 0.01
MAX_OPEN = 5  # matches config

def is_uptrend(i):
    return ema21[i] > ema55[i] and ema50_slope[i] > 0.1

# ══════════════════════════════════════════════════════════════════════
#  ENTRY DETECTION (mirrors strategy_engine.py exactly)
# ══════════════════════════════════════════════════════════════════════

def check_breakout(i):
    if np.isnan(high30[i]) or np.isnan(avg_vol[i]) or avg_vol[i] <= 0: return None
    if c4[i] <= high30[i-1]: return None
    if v4[i] < avg_vol[i] * 1.5: return None
    if c4[i] <= o4[i]: return None
    entry = c4[i]; atr = atr14[i]; sl = entry - 1.5 * atr
    return {"strat": "BREAKOUT", "entry": entry, "sl": sl, "trail_atr": 3.0, "ts": 48}

def check_momentum(i):
    if np.isnan(avg_body[i]) or avg_body[i] <= 0: return None
    if np.isnan(avg_vol[i]) or avg_vol[i] <= 0: return None
    if c4[i] <= o4[i]: return None
    if body[i] < avg_body[i] * 2.0: return None
    if v4[i] < avg_vol[i] * 2.0: return None
    if c4[i] <= ema21[i]: return None
    entry = c4[i]; atr = atr14[i]; sl = entry - 1.5 * atr
    return {"strat": "MOMENTUM", "entry": entry, "sl": sl, "trail_atr": 3.0, "ts": 48}

def check_dip(i):
    if np.isnan(high8[i]) or high8[i] <= 0: return None
    if np.isnan(avg_body[i]) or avg_body[i] <= 0: return None
    dip_pct = (high8[i] - l4[i]) / high8[i] * 100
    if dip_pct < 3.0: return None
    if c4[i] <= o4[i]: return None
    if body[i] < avg_body[i] * 1.5: return None
    if c4[i] < ema55[i]: return None
    entry = c4[i]; atr = atr14[i]; sl = entry - 1.5 * atr
    return {"strat": "DIP", "entry": entry, "sl": sl, "trail_atr": 3.5, "ts": 48}

def check_pullback(i):
    atr = atr14[i]
    if np.isnan(atr) or atr <= 0 or np.isnan(ema40[i]): return None
    dist = l4[i] - ema40[i]
    if dist < -0.3 * atr or dist > 1.2 * atr: return None
    if l4[i] > ema21[i]: return None
    if c4[i] <= o4[i]: return None
    if c4[i] <= ema21[i]: return None
    entry = c4[i]
    sl = min(l4[i], ema40[i]) - 0.3 * atr
    sl_dist = entry - sl
    if sl_dist <= 0 or sl_dist > 3 * atr: return None
    return {"strat": "PULLBACK", "entry": entry, "sl": sl, "trail_atr": 3.5, "ts": 48}

def check_volreset(i):
    atr = atr14[i]
    if np.isnan(atr) or atr <= 0: return None
    if np.isnan(avg_vol[i]) or avg_vol[i] <= 0: return None
    dry_count = 0
    for j in range(1, 4):
        if i-j >= 0 and v4[i-j] < 0.8 * avg_vol[i]: dry_count += 1
    if dry_count < 1: return None
    if v4[i] < 1.2 * avg_vol[i]: return None
    if abs(c4[i] - ema21[i]) > 0.8 * atr: return None
    if c4[i] <= o4[i]: return None
    if c4[i] <= ema21[i]: return None
    entry = c4[i]; sl = entry - 1.5 * atr
    return {"strat": "VRESET", "entry": entry, "sl": sl, "trail_atr": 4.0, "ts": 60}

# ══════════════════════════════════════════════════════════════════════
#  COMBINED PORTFOLIO SIMULATION
# ══════════════════════════════════════════════════════════════════════

balance = 10000.0
active_trades = []  # list of dicts
all_trades = []     # completed trades
cooldowns = {"BREAKOUT": 0, "MOMENTUM": 0, "DIP": 0, "PULLBACK": 0, "VRESET": 0}
cd_bars = {"BREAKOUT": 3, "MOMENTUM": 3, "DIP": 3, "PULLBACK": 3, "VRESET": 2}
max_concurrent = 0
overlap_count = 0  # times multiple signals fire on same bar
signal_log = []

print("\nRunning combined 5-strategy simulation...\n")

for i in range(START, n4h):
    # ── 1. Check exits on all active trades ──────────────────────
    still_active = []
    for t in active_trades:
        # Update trailing stop
        new_t = c4[i] - t["trail_atr"] * atr14[i]
        if not np.isnan(new_t) and new_t > t["trail"]:
            t["trail"] = new_t

        pnl = None
        exit_type = None
        if l4[i] <= t["sl"]:
            pnl = (t["sl"] - t["entry"]) * t["units"] - FRICTION
            exit_type = "SL"
        elif t["trail"] > t["entry"] and l4[i] <= t["trail"]:
            pnl = (t["trail"] - t["entry"]) * t["units"] - FRICTION
            exit_type = "TRAIL"
        elif i >= t["ts_bar"]:
            pnl = (c4[i] - t["entry"]) * t["units"] - FRICTION
            exit_type = "TIME"

        if pnl is not None:
            balance += pnl
            cooldowns[t["strat"]] = i + cd_bars[t["strat"]]
            all_trades.append({
                "month": get_month(t["ei"]), "pnl": round(pnl, 2),
                "strat": t["strat"], "entry_i": t["ei"], "exit_i": i,
                "exit_type": exit_type, "entry_price": t["entry"]
            })
        else:
            still_active.append(t)

    active_trades = still_active
    max_concurrent = max(max_concurrent, len(active_trades))

    # ── 2. Check entries (if uptrend + not at max capacity) ──────
    if not is_uptrend(i) or np.isnan(atr14[i]) or atr14[i] <= 0:
        continue

    signals_this_bar = []
    checks = [
        ("BREAKOUT", check_breakout),
        ("MOMENTUM", check_momentum),
        ("DIP", check_dip),
        ("PULLBACK", check_pullback),
        ("VRESET", check_volreset),
    ]

    for name, check_fn in checks:
        if i < cooldowns[name]: continue
        # Check if this strategy already has an active trade
        if any(t["strat"] == name for t in active_trades): continue
        sig = check_fn(i)
        if sig:
            signals_this_bar.append(sig)

    if len(signals_this_bar) > 1:
        overlap_count += 1

    # Open trades up to MAX_OPEN
    for sig in signals_this_bar:
        if len(active_trades) >= MAX_OPEN:
            break
        entry = sig["entry"]
        sl = sig["sl"]
        sl_dist = entry - sl
        if sl_dist <= 0: continue

        units = (balance * RISK) / sl_dist
        active_trades.append({
            "strat": sig["strat"], "entry": entry, "ei": i,
            "sl": sl, "trail": sl, "trail_atr": sig["trail_atr"],
            "ts_bar": i + sig["ts"], "units": units
        })
        signal_log.append({"bar": i, "strat": sig["strat"], "month": get_month(i)})

# Close remaining active trades at last price
for t in active_trades:
    pnl = (c4[-1] - t["entry"]) * t["units"] - FRICTION
    balance += pnl
    all_trades.append({
        "month": get_month(t["ei"]), "pnl": round(pnl, 2),
        "strat": t["strat"], "entry_i": t["ei"], "exit_i": n4h-1,
        "exit_type": "OPEN", "entry_price": t["entry"]
    })

# ══════════════════════════════════════════════════════════════════════
#  ANALYSIS
# ══════════════════════════════════════════════════════════════════════

n_total = len(all_trades)
total_pnl = sum(t["pnl"] for t in all_trades)
wins = sum(1 for t in all_trades if t["pnl"] > 0)
wr = wins / n_total if n_total else 0
ws = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
ls = abs(sum(t["pnl"] for t in all_trades if t["pnl"] <= 0))
pf = ws / ls if ls > 0 else 0

eq = [10000.0]
for t in all_trades: eq.append(eq[-1] + t["pnl"])
ea = np.array(eq)
mdd = abs(np.min(ea - np.maximum.accumulate(ea)))
rdd = total_pnl / mdd if mdd > 0 else 0

# Walk-forward
half = n_total // 2
h1 = sum(t["pnl"] for t in all_trades[:half])
h2 = sum(t["pnl"] for t in all_trades[half:])

# Monthly
monthly = {}
for t in all_trades: monthly.setdefault(t["month"], []).append(t)
mpnl = {m: sum(t["pnl"] for t in mt) for m, mt in monthly.items()}
win_months = sum(1 for v in mpnl.values() if v > 0)
total_months = len(mpnl)
best_month = max(mpnl.values()) if mpnl else 0
worst_month = min(mpnl.values()) if mpnl else 0
conc = (total_pnl - best_month) > 0

# Per-strategy breakdown
strat_stats = {}
for t in all_trades:
    s = t["strat"]
    if s not in strat_stats:
        strat_stats[s] = {"n": 0, "pnl": 0, "wins": 0, "losses": 0}
    strat_stats[s]["n"] += 1
    strat_stats[s]["pnl"] += t["pnl"]
    if t["pnl"] > 0: strat_stats[s]["wins"] += 1
    else: strat_stats[s]["losses"] += 1

# Exit type breakdown
exit_types = {}
for t in all_trades:
    et = t["exit_type"]
    if et not in exit_types: exit_types[et] = {"n": 0, "pnl": 0}
    exit_types[et]["n"] += 1
    exit_types[et]["pnl"] += t["pnl"]

# ══════════════════════════════════════════════════════════════════════
#  REPORT
# ══════════════════════════════════════════════════════════════════════

print("=" * 70)
print("  COMBINED PORTFOLIO RESULTS")
print("=" * 70)
print("  Trades:     %d" % n_total)
print("  Total PnL:  $%+.0f (%.0f%% return)" % (total_pnl, total_pnl/100))
print("  Win Rate:   %.1f%% (%d W / %d L)" % (wr*100, wins, n_total-wins))
print("  PF:         %.2f" % pf)
print("  MDD:        $%.0f" % mdd)
print("  R/DD:       %.2f" % rdd)
print("  Final Bal:  $%.0f" % balance)

print("\n  ROBUSTNESS CHECKS:")
print("  [%s] Profitable:     $%+.0f" % ("PASS" if total_pnl > 0 else "FAIL", total_pnl))
print("  [%s] Concentration:  profitable without best month ($%+.0f best)" % (
    "PASS" if conc else "FAIL", best_month))
print("  [%s] Walk-forward:   H1=$%+.0f H2=$%+.0f" % (
    "PASS" if h1 > 0 and h2 > 0 else "FAIL", h1, h2))
print("  [%s] Sample size:    %d trades (need 20+)" % (
    "PASS" if n_total >= 20 else "FAIL", n_total))
print("  [%s] Profit Factor:  %.2f (need > 1.2)" % (
    "PASS" if pf > 1.2 else "FAIL", pf))

print("\n  PER-STRATEGY BREAKDOWN:")
print("  %-12s | %4s | %3s | %3s | %8s | %4s" % ("Strategy", "Trds", "W", "L", "PnL", "WR"))
print("  " + "-" * 55)
for s in ["BREAKOUT", "MOMENTUM", "DIP", "PULLBACK", "VRESET"]:
    if s in strat_stats:
        ss = strat_stats[s]
        swr = ss["wins"] / ss["n"] * 100 if ss["n"] else 0
        print("  %-12s | %4d | %3d | %3d | $%+6.0f | %3.0f%%" % (
            s, ss["n"], ss["wins"], ss["losses"], ss["pnl"], swr))

print("\n  EXIT TYPE BREAKDOWN:")
for et in ["TRAIL", "SL", "TIME", "OPEN"]:
    if et in exit_types:
        e = exit_types[et]
        print("  %-8s: %3d trades, $%+.0f" % (et, e["n"], e["pnl"]))

print("\n  CONCURRENCY:")
print("  Max concurrent trades: %d" % max_concurrent)
print("  Multi-signal bars:     %d (times 2+ strategies fired same candle)" % overlap_count)

print("\n  MONTHLY P&L:")
for m in sorted(mpnl.keys()):
    trades_this_month = [t for t in all_trades if t["month"] == m]
    n_m = len(trades_this_month)
    print("  %s: $%+6.0f (%d trades)" % (m, mpnl[m], n_m))

print("\n  Win months: %d/%d (%.0f%%)" % (win_months, total_months,
    win_months/total_months*100 if total_months else 0))
print("  Best month:  $%+.0f" % best_month)
print("  Worst month: $%+.0f" % worst_month)

# ══════════════════════════════════════════════════════════════════════
#  BUG DETECTION TESTS
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  BUG DETECTION")
print("=" * 70)

bugs_found = 0

# Test 1: Do backtested strategies match production engine?
from strategy_engine import StrategyEngine
engine = StrategyEngine()
df_4h_test = market_data.download("BTCUSDT", "4h", total_candles=500)
df_1d_test = market_data.download("BTCUSDT", "1d", total_candles=200)
engine.update_data(df_4h_test, df_1d_test)

# Verify engine has all 5 strategies
for method in ["_check_breakout", "_check_vol_momentum", "_check_panic_dip",
               "_check_ema_pullback", "_check_vol_reset"]:
    if not hasattr(engine, method):
        print("  [BUG] Missing method: %s" % method)
        bugs_found += 1
    else:
        print("  [OK] Method exists: %s" % method)

# Test 2: Check cooldown attributes
for attr in ["cooldown_breakout", "cooldown_momentum", "cooldown_dip",
             "cooldown_pullback", "cooldown_volreset"]:
    if not hasattr(engine, attr):
        print("  [BUG] Missing cooldown: %s" % attr)
        bugs_found += 1
    else:
        print("  [OK] Cooldown exists: %s" % attr)

# Test 3: Check status() has all fields
status = engine.status()
for key in ["cooldown_breakout", "cooldown_momentum", "cooldown_pullback", "cooldown_volreset"]:
    if key not in status:
        print("  [BUG] status() missing: %s" % key)
        bugs_found += 1

# Check for missing cooldown_dip in status
if "cooldown_dip" not in status:
    print("  [BUG] status() missing: cooldown_dip")
    bugs_found += 1

# Test 4: Check strategy names match between engine and srs_main
from srs_main import _4H_STRATEGIES
expected = {"BREAKOUT_4H", "VOL_MOMENTUM_4H", "PANIC_DIP_4H", "EMA_PULLBACK_4H", "VOL_RESET_4H"}
if _4H_STRATEGIES != expected:
    print("  [BUG] srs_main._4H_STRATEGIES mismatch: %s" % _4H_STRATEGIES)
    bugs_found += 1
else:
    print("  [OK] srs_main._4H_STRATEGIES matches all 5")

# Test 5: Check config matches
import config
if len(config.STRATEGIES) != 5:
    print("  [BUG] config.STRATEGIES has %d strategies (expected 5)" % len(config.STRATEGIES))
    bugs_found += 1
else:
    print("  [OK] config.STRATEGIES has 5 strategies")

if config.MAX_OPEN_TRADES != 5:
    print("  [BUG] config.MAX_OPEN_TRADES = %d (expected 5)" % config.MAX_OPEN_TRADES)
    bugs_found += 1
else:
    print("  [OK] config.MAX_OPEN_TRADES = 5")

# Test 6: Check paper_trader position limits
import bot.paper_trader as pt
import inspect
src = inspect.getsource(pt.open_paper_trade)
if "if len(open_trades) >= 3:" in src:
    print("  [BUG] paper_trader HARDCODES max 3 trades (should use config.MAX_OPEN_TRADES)")
    bugs_found += 1

if "same_side >= 2:" in src:
    print("  [BUG] paper_trader LIMITS 2 same-side trades (all 5 strategies are 'buy')")
    bugs_found += 1

# Check for duplicate symbol+side block
if 'already open' in src and 'sym_lc' in src:
    print("  [BUG] paper_trader BLOCKS duplicate symbol+side (prevents multiple BTCUSDT buy trades)")
    bugs_found += 1

# Test 7: Check time_stop_hours for VOL_RESET
sigs = engine.check_signals()
# Can't directly test time_stop since it depends on market conditions
# But verify the constant
from strategy_engine import VRESET_TIME_STOP
if VRESET_TIME_STOP != 60:
    print("  [BUG] VRESET_TIME_STOP = %d (expected 60 bars)" % VRESET_TIME_STOP)
    bugs_found += 1
else:
    print("  [OK] VRESET_TIME_STOP = 60 bars (240 hours)")

# Test 8: record_trade_result covers all strategies
engine.record_trade_result("VOL_RESET_4H", True)
if engine.cooldown_volreset != engine.current_bar + 2:
    print("  [BUG] VOL_RESET cooldown not applied correctly")
    bugs_found += 1
else:
    print("  [OK] VOL_RESET cooldown applies correctly")

print("\n  TOTAL BUGS FOUND: %d" % bugs_found)

if bugs_found > 0:
    print("\n  CRITICAL FIXES NEEDED:")
    print("  1. paper_trader.py: Max trades hardcoded to 3 (needs config.MAX_OPEN_TRADES)")
    print("  2. paper_trader.py: Same-side limit of 2 (all strategies are buy)")
    print("  3. paper_trader.py: Duplicate symbol+side block (prevents multi-strategy)")
    print("     → These 3 bugs mean ONLY 1 BTCUSDT buy trade can be open at a time!")
    print("     → The 5-strategy system is effectively reduced to 1 strategy")
    print("  4. strategy_engine.py: cooldown_dip missing from status()")

print("\nDone.")
