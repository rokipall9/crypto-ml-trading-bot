"""
==================================================================================
SMC TRADING BOOK — BEST 5 STRATEGIES (consolidated, runnable)
==================================================================================

This is the SINGLE SOURCE OF TRUTH for the best version of each strategy
backtested across V7, V8, V9, V10 iterations on BTC.

Author: Lidor Malka SMC trading book + V9 fib-retrace refinement.
Tested: BTC-USD, daily 5y + 1h 2y, $1000 account, 1% risk per trade.

Run: python BEST_STRATEGIES.py
Output: console table + best_strategies_trades.csv

==================================================================================
RESULTS TABLE — BTC 1h, 2 years (the moneymaker timeframe)
==================================================================================
  Strategy            Trades  Win%   AvgR   Final$    Return    DD     Source
  S1 Spring/UTAD          50  56.0%  +0.96   1,600     +60.0%   3.1%   V9
  S2 MSS+IMB             501  50.7%  +0.78  45,546   +4,454%   9.6%   V9
  S3 OTE 0.7-0.8         464  56.5%  +1.26 309,313  +30,831%   5.9%   V8
  S4 Range Deviation      34  55.9%  +0.96   1,375     +37.5%   3.0%   V9
  S5 FTR (fast move)     710  43.5%  +0.57  49,955   +4,895%  11.5%   V9
==================================================================================
"""
from __future__ import annotations
import warnings; warnings.filterwarnings('ignore')
# yfinance stubbed for shadow mode (data injected by shadow_book.py)
import sys as _sys
if "yfinance" not in _sys.modules:
    _yf = type(_sys)("yfinance")
    _yf.download = lambda *a, **k: None
    _sys.modules["yfinance"] = _yf
import yfinance as yf  # now resolves to the stub
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional


# =================================================================================
# DATA & UTILITIES
# =================================================================================

def fetch(symbol='BTC-USD', period='730d', interval='1h'):
    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
    if df.empty: return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df[['Open','High','Low','Close','Volume']].dropna()
    df.columns = ['open','high','low','close','volume']
    return df


def atr(df, n=14):
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def pivots_zz(df, atr_mult=2.0):
    """ATR-based zigzag pivot detection. Returns highs and lows as [(pos, price), ...]."""
    a = atr(df, 14)
    highs, lows = [], []
    direction = 0; last_pos = 0; last_price = df['close'].iloc[0]
    for i in range(1, len(df)):
        h, l = df['high'].iloc[i], df['low'].iloc[i]
        cur_atr = a.iloc[i] if not pd.isna(a.iloc[i]) else 0
        thr = cur_atr * atr_mult
        if direction == 0:
            if h - last_price > thr: direction = 1; last_price = h; last_pos = i
            elif last_price - l > thr: direction = -1; last_price = l; last_pos = i
        elif direction == 1:
            if h > last_price: last_price = h; last_pos = i
            elif last_price - l > thr:
                highs.append((last_pos, last_price)); direction = -1; last_price = l; last_pos = i
        else:
            if l < last_price: last_price = l; last_pos = i
            elif h - last_price > thr:
                lows.append((last_pos, last_price)); direction = 1; last_price = h; last_pos = i
    return highs, lows


@dataclass
class Trade:
    strategy: str
    side: str
    entry_idx: int
    entry: float
    stop: float
    target: float
    R: Optional[float] = None
    outcome: Optional[str] = None


def simulate(df, entry_idx, side, entry, stop, target, max_bars=80):
    """Walk forward: stop hit first or target hit first?"""
    risk = abs(entry-stop)
    if risk == 0: return 0.0, 'flat'
    end = min(entry_idx+max_bars, len(df)-1)
    for j in range(entry_idx+1, end+1):
        h, l = df['high'].iloc[j], df['low'].iloc[j]
        if side == 'long':
            if l <= stop: return -1.0, 'loss'
            if h >= target: return (target-entry)/risk, 'win'
        else:
            if h >= stop: return -1.0, 'loss'
            if l <= target: return (entry-target)/risk, 'win'
    exit_p = df['close'].iloc[end]
    R = (exit_p-entry)/risk if side == 'long' else (entry-exit_p)/risk
    return R, 'time'


def fib_entry(df, impulse_high_pos, impulse_high_price, impulse_low_pos, impulse_low_price,
              direction, target_R, fib_top, fib_bot, fib_stop, max_wait_bars=50,
              strategy='?'):
    """Generic fib-retrace entry on an impulse. Returns Trade or None."""
    if direction == 'long':
        ph_price = impulse_high_price
        pl_price = impulse_low_price
        zone_top = ph_price - (ph_price - pl_price) * fib_top
        zone_bot = ph_price - (ph_price - pl_price) * fib_bot
        for j in range(impulse_high_pos+1, min(impulse_high_pos+max_wait_bars, len(df))):
            if zone_bot <= df['low'].iloc[j] <= zone_top:
                entry = (zone_top + zone_bot) / 2
                stop = pl_price + (ph_price - pl_price) * fib_stop
                if entry <= stop: return None
                risk = entry - stop
                target = entry + risk * target_R
                R, oc = simulate(df, j, 'long', entry, stop, target, max_bars=60)
                return Trade(strategy, 'long', j, entry, stop, target, R, oc)
    else:
        ph_price = impulse_high_price
        pl_price = impulse_low_price
        zone_bot = pl_price + (ph_price - pl_price) * fib_top
        zone_top = pl_price + (ph_price - pl_price) * fib_bot
        for j in range(impulse_low_pos+1, min(impulse_low_pos+max_wait_bars, len(df))):
            if zone_bot <= df['high'].iloc[j] <= zone_top:
                entry = (zone_top + zone_bot) / 2
                stop = ph_price - (ph_price - pl_price) * fib_stop
                if entry >= stop: return None
                risk = stop - entry
                target = entry - risk * target_R
                R, oc = simulate(df, j, 'short', entry, stop, target, max_bars=60)
                return Trade(strategy, 'short', j, entry, stop, target, R, oc)
    return None


# =================================================================================
# S1 — Wyckoff Spring/UTAD with fib retrace entry  [V9 — BEST]
# =================================================================================
# 1h: 50 trades, 56.0% win, +0.96 avgR, +60.0% return, 3.1% DD
# Logic: detect Spring (wick + recovery in range) → wait for rally peak (4+ ATR up)
#        → enter on fib retrace 0.62-0.85 of post-Spring rally
# =================================================================================

def s1_spring(df,
              # 1h best params:
              lookback=50, min_touches=3, sweep_max_atr=2.5,
              target_R=2.5, fib_top=0.62, fib_bot=0.85, fib_stop=0.10,
              htf_filter=False):
    """S1: Spring/UTAD with fib retrace entry. V9 — best."""
    trades = []
    a = atr(df, 14)
    sma50 = df['close'].rolling(50).mean()
    if len(df) < lookback + 30: return trades

    for i in range(lookback, len(df) - 30):
        atr_now = a.iloc[i]
        if pd.isna(atr_now) or atr_now <= 0: continue
        win = df.iloc[i-lookback:i]
        rng_high = win['high'].max(); rng_low = win['low'].min()
        if not (atr_now*3 < rng_high - rng_low < atr_now*15): continue
        candle = df.iloc[i]

        # SPRING (long)
        if candle['low'] < rng_low and candle['close'] > rng_low:
            wick = rng_low - candle['low']
            if not (0.2*atr_now < wick < sweep_max_atr*atr_now): continue
            if (win['low'] <= rng_low + 0.5*atr_now).sum() < min_touches: continue
            if htf_filter and not pd.isna(sma50.iloc[i]) and candle['close'] < sma50.iloc[i]:
                continue
            spring_low = candle['low']
            rally_peak_price = candle['close']
            rally_peak_pos = i
            for j in range(i+1, min(i+30, len(df))):
                if df['high'].iloc[j] > rally_peak_price:
                    rally_peak_price = df['high'].iloc[j]; rally_peak_pos = j
                if rally_peak_price - candle['low'] >= atr_now * 4: break
            if rally_peak_price - spring_low < atr_now * 4: continue
            t = fib_entry(df, rally_peak_pos, rally_peak_price, i, spring_low,
                          'long', target_R, fib_top, fib_bot, fib_stop, strategy='S1')
            if t: trades.append(t)

        # UTAD (short)
        if candle['high'] > rng_high and candle['close'] < rng_high:
            wick = candle['high'] - rng_high
            if not (0.2*atr_now < wick < sweep_max_atr*atr_now): continue
            if (win['high'] >= rng_high - 0.5*atr_now).sum() < min_touches: continue
            if htf_filter and not pd.isna(sma50.iloc[i]) and candle['close'] > sma50.iloc[i]:
                continue
            utad_high = candle['high']
            drop_low_price = candle['close']
            drop_low_pos = i
            for j in range(i+1, min(i+30, len(df))):
                if df['low'].iloc[j] < drop_low_price:
                    drop_low_price = df['low'].iloc[j]; drop_low_pos = j
                if utad_high - drop_low_price >= atr_now * 4: break
            if utad_high - drop_low_price < atr_now * 4: continue
            t = fib_entry(df, i, utad_high, drop_low_pos, drop_low_price,
                          'short', target_R, fib_top, fib_bot, fib_stop, strategy='S1')
            if t: trades.append(t)
    return trades


# =================================================================================
# S2 — MSS + fib retrace of MSS impulse  [V9 — BEST]
# =================================================================================
# 1h: 501 trades, 50.7% win, +0.78 avgR, +4,454.6% return, 9.6% DD
# Logic: ATR zigzag finds pivots → close beyond opposite pivot (MSS)
#        → enter on fib retrace 0.7-0.85 of (last opposite pivot → MSS peak)
# =================================================================================

def s2_mss(df,
           # 1h best params:
           zz_atr_mult=2.0, msb_strength_atr=0.3, target_R=2.5,
           fib_top=0.7, fib_bot=0.85, fib_stop=0.10,
           htf_filter=True):
    """S2: MSS + fib retrace entry. V9 — best."""
    trades = []
    a = atr(df, 14)
    sma50 = df['close'].rolling(50).mean()
    highs, lows = pivots_zz(df, atr_mult=zz_atr_mult)
    all_pv = sorted([(p,pr,'H') for p,pr in highs] + [(p,pr,'L') for p,pr in lows])

    for pi in range(2, len(all_pv)):
        prev_pos, prev_pr, prev_t = all_pv[pi-1]
        curr_pos, curr_pr, curr_t = all_pv[pi]
        scan_start = curr_pos + 1
        scan_end = min(scan_start + 25, len(df))
        atr_at = a.iloc[scan_start] if scan_start < len(a) else 0
        if pd.isna(atr_at) or atr_at <= 0: continue

        if prev_t == 'H' and curr_t == 'L':
            for k in range(scan_start, scan_end):
                if k < 2 or k >= len(df) - 1: continue
                if htf_filter and (pd.isna(sma50.iloc[k]) or df['close'].iloc[k] < sma50.iloc[k]):
                    continue
                if df['close'].iloc[k] > prev_pr + atr_at*msb_strength_atr:
                    peak_pos = k; peak_price = df['close'].iloc[k]
                    for kk in range(k+1, min(k+10, len(df))):
                        if df['high'].iloc[kk] > peak_price:
                            peak_price = df['high'].iloc[kk]; peak_pos = kk
                    if peak_price - curr_pr < atr_at * 4: break
                    t = fib_entry(df, peak_pos, peak_price, curr_pos, curr_pr,
                                  'long', target_R, fib_top, fib_bot, fib_stop, strategy='S2')
                    if t: trades.append(t)
                    break
        elif prev_t == 'L' and curr_t == 'H':
            for k in range(scan_start, scan_end):
                if k < 2 or k >= len(df) - 1: continue
                if htf_filter and (pd.isna(sma50.iloc[k]) or df['close'].iloc[k] > sma50.iloc[k]):
                    continue
                if df['close'].iloc[k] < prev_pr - atr_at*msb_strength_atr:
                    trough_pos = k; trough_price = df['close'].iloc[k]
                    for kk in range(k+1, min(k+10, len(df))):
                        if df['low'].iloc[kk] < trough_price:
                            trough_price = df['low'].iloc[kk]; trough_pos = kk
                    if curr_pr - trough_price < atr_at * 4: break
                    t = fib_entry(df, curr_pos, curr_pr, trough_pos, trough_price,
                                  'short', target_R, fib_top, fib_bot, fib_stop, strategy='S2')
                    if t: trades.append(t)
                    break
    return trades


# =================================================================================
# S3 — OTE 0.7-0.8 fib + OB + HTF trend filter  [V8 — BEST native]
# =================================================================================
# 1h: 464 trades, 56.5% win, +1.26 avgR, +30,831% return, 5.9% DD  🏆 KING
# Logic: ATR zigzag → impulse 4+ ATR → wait for fib retrace 0.65-0.92 → enter
# =================================================================================

def s3_ote(df,
           zz_atr_mult=2.0, htf_filter=True, target_R=3.0,
           fib_top=0.7, fib_bot=0.92, fib_stop=0.10):
    """S3: OTE 0.7-0.8 + OB. V8 — best (native fib-retrace mechanic)."""
    trades = []
    a = atr(df, 14)
    sma50 = df['close'].rolling(50).mean()
    highs, lows = pivots_zz(df, atr_mult=zz_atr_mult)
    all_pv = sorted([(p,pr,'H') for p,pr in highs] + [(p,pr,'L') for p,pr in lows])

    for pi in range(1, len(all_pv)):
        prev = all_pv[pi-1]; curr = all_pv[pi]
        if prev[2] == 'L' and curr[2] == 'H':
            pl_pos, pl_pr = prev[0], prev[1]
            ph_pos, ph_pr = curr[0], curr[1]
            if ph_pos <= pl_pos + 3: continue
            avg_a = a.iloc[pl_pos:ph_pos+1].mean()
            if pd.isna(avg_a) or avg_a <= 0: continue
            if (ph_pr - pl_pr) < avg_a * 4: continue
            if htf_filter and (pd.isna(sma50.iloc[ph_pos]) or ph_pr < sma50.iloc[ph_pos]): continue
            t = fib_entry(df, ph_pos, ph_pr, pl_pos, pl_pr,
                          'long', target_R, fib_top, fib_bot, fib_stop, strategy='S3')
            if t: trades.append(t)

        elif prev[2] == 'H' and curr[2] == 'L':
            ph_pos, ph_pr = prev[0], prev[1]
            pl_pos, pl_pr = curr[0], curr[1]
            if pl_pos <= ph_pos + 3: continue
            avg_a = a.iloc[ph_pos:pl_pos+1].mean()
            if pd.isna(avg_a) or avg_a <= 0: continue
            if (ph_pr - pl_pr) < avg_a * 4: continue
            if htf_filter and (pd.isna(sma50.iloc[pl_pos]) or pl_pr > sma50.iloc[pl_pos]): continue
            t = fib_entry(df, ph_pos, ph_pr, pl_pos, pl_pr,
                          'short', target_R, fib_top, fib_bot, fib_stop, strategy='S3')
            if t: trades.append(t)
    return trades


# =================================================================================
# S4 — Range Deviation + fib retrace of post-deviation move  [V9 — BEST 1h]
# =================================================================================
# 1h: 34 trades, 55.9% win, +0.96 avgR, +37.5% return, 3.0% DD
# Logic: range with 2+ touches each side → deviation outside → fib retrace of move back inside
# =================================================================================

def s4_range_dev(df,
                 # 1h best params:
                 lookback=30, min_touches=2, range_min_atr=3, range_max_atr=12,
                 target_R=2.5, fib_top=0.7, fib_bot=0.85, fib_stop=0.10,
                 htf_filter=False):
    """S4: Range deviation + fib retrace. V9 — best."""
    trades = []
    a = atr(df, 14)
    sma50 = df['close'].rolling(50).mean()
    fired = set()

    for i in range(lookback+2, len(df)-30):
        atr_now = a.iloc[i]
        if pd.isna(atr_now) or atr_now <= 0: continue
        win = df.iloc[i-lookback:i]
        rh = win['high'].max(); rl = win['low'].min()
        if not (atr_now*range_min_atr < rh-rl < atr_now*range_max_atr): continue
        th = (win['high'] >= rh - 0.5*atr_now).sum()
        tl = (win['low'] <= rl + 0.5*atr_now).sum()
        if th < min_touches or tl < min_touches: continue
        c1 = df.iloc[i]; c2 = df.iloc[i+1]

        # SHORT deviation
        if c1['close'] > rh and c2['close'] < rh:
            if i+1 in fired: continue
            if htf_filter and not pd.isna(sma50.iloc[i+1]) and c2['close'] > sma50.iloc[i+1]:
                continue
            dev_high = c1['high']
            low_pos = i+1; low_price = c2['close']
            for j in range(i+2, min(i+15, len(df))):
                if df['low'].iloc[j] < low_price:
                    low_price = df['low'].iloc[j]; low_pos = j
                if dev_high - low_price >= atr_now * 4: break
            if dev_high - low_price < atr_now * 4: continue
            t = fib_entry(df, i, dev_high, low_pos, low_price,
                          'short', target_R, fib_top, fib_bot, fib_stop, strategy='S4')
            if t: trades.append(t); fired.add(i+1)

        # LONG deviation
        if c1['close'] < rl and c2['close'] > rl:
            if i+1 in fired: continue
            if htf_filter and not pd.isna(sma50.iloc[i+1]) and c2['close'] < sma50.iloc[i+1]:
                continue
            dev_low = c1['low']
            high_pos = i+1; high_price = c2['close']
            for j in range(i+2, min(i+15, len(df))):
                if df['high'].iloc[j] > high_price:
                    high_price = df['high'].iloc[j]; high_pos = j
                if high_price - dev_low >= atr_now * 4: break
            if high_price - dev_low < atr_now * 4: continue
            t = fib_entry(df, high_pos, high_price, i, dev_low,
                          'long', target_R, fib_top, fib_bot, fib_stop, strategy='S4')
            if t: trades.append(t); fired.add(i+1)
    return trades


# =================================================================================
# S5 — Fast move + fib retrace of fast move  [V9 — BEST]
# =================================================================================
# 1h: 710 trades, 43.5% win, +0.57 avgR, +4,895.5% return, 11.5% DD
# Logic: candle range > 2 ATR with strong close → fib retrace 0.4-0.7 of candle
# =================================================================================

def s5_ftr(df,
           # 1h best params:
           fast_atr=2.0, close_pos=0.65, target_R=2.5,
           fib_top=0.4, fib_bot=0.7, fib_stop=0.10,
           htf_filter=False):
    """S5: Fast move + fib retrace entry. V9 — best."""
    trades = []
    a = atr(df, 14)
    sma50 = df['close'].rolling(50).mean()

    for i in range(20, len(df)-30):
        candle = df.iloc[i]
        atr_now = a.iloc[i]
        if pd.isna(atr_now) or atr_now <= 0: continue
        cr = candle['high'] - candle['low']
        if cr < atr_now*fast_atr: continue
        cp = (candle['close'] - candle['low']) / cr if cr > 0 else 0
        bullish = candle['close'] > candle['open']

        if bullish and cp > close_pos:
            if htf_filter and (pd.isna(sma50.iloc[i]) or candle['close'] < sma50.iloc[i]): continue
            peak_pos = i; peak_price = candle['high']
            for j in range(i+1, min(i+8, len(df))):
                if df['high'].iloc[j] > peak_price:
                    peak_price = df['high'].iloc[j]; peak_pos = j
                else: break
            t = fib_entry(df, peak_pos, peak_price, i, candle['low'],
                          'long', target_R, fib_top, fib_bot, fib_stop, max_wait_bars=15, strategy='S5')
            if t: trades.append(t)
        elif (not bullish) and cp < (1 - close_pos):
            if htf_filter and (pd.isna(sma50.iloc[i]) or candle['close'] > sma50.iloc[i]): continue
            trough_pos = i; trough_price = candle['low']
            for j in range(i+1, min(i+8, len(df))):
                if df['low'].iloc[j] < trough_price:
                    trough_price = df['low'].iloc[j]; trough_pos = j
                else: break
            t = fib_entry(df, i, candle['high'], trough_pos, trough_price,
                          'short', target_R, fib_top, fib_bot, fib_stop, max_wait_bars=15, strategy='S5')
            if t: trades.append(t)
    return trades


# =================================================================================
# REPORTING
# =================================================================================

def stats(trades):
    if not trades: return None
    Rs = [t.R for t in trades if t.R is not None]
    if not Rs: return None
    wins = sum(1 for t in trades if t.outcome == 'win')
    return {'n': len(trades), 'win_rate': wins/len(trades)*100,
            'avg_R': float(np.mean(Rs)), 'total_R': float(np.sum(Rs))}


def equity(trades, risk_pct=0.01, start=1000):
    bal = start; peak = start; max_dd = 0
    for t in trades:
        if t.R is None: continue
        bal *= (1 + risk_pct*t.R)
        peak = max(peak, bal)
        dd = (peak-bal)/peak*100
        max_dd = max(max_dd, dd)
    return bal, (bal/start-1)*100, max_dd


def main():
    print("=" * 92)
    print("BEST 5 SMC STRATEGIES — peak version of each — BTC")
    print("$1,000 starting account, 1% risk per trade")
    print("=" * 92)

    for tf_label, period, interval in [
        ('BTC daily 5y', '5y', '1d'),
        ('BTC 1h 2y', '730d', '1h'),
    ]:
        print(f"\n=== {tf_label} ===")
        df = fetch('BTC-USD', period=period, interval=interval)
        if df is None or len(df) < 200:
            print("  insufficient data"); continue
        print(f"  {len(df)} bars from {df.index[0]} to {df.index[-1]}\n")

        all_trades = []
        for name, fn in [
            ('S1 Spring/UTAD',     s1_spring),
            ('S2 MSS+IMB',         s2_mss),
            ('S3 OTE 0.7-0.8',     s3_ote),
            ('S4 Range Deviation', s4_range_dev),
            ('S5 FTR fast move',   s5_ftr),
        ]:
            trades = fn(df)
            all_trades.extend(trades)
            s = stats(trades)
            bal, ret, dd = equity(trades)
            if s is None:
                print(f"  {name:<22}  no trades")
                continue
            print(f"  {name:<22}  n={s['n']:>4}  win={s['win_rate']:>5.1f}%  avgR={s['avg_R']:+.2f}  totR={s['total_R']:+.2f}  ${bal:>10,.0f}  {ret:+9.1f}%  DD={dd:>5.1f}%")

        # Save trade list to CSV
        if interval == '1h':
            rows = [{
                'date': str(df.index[t.entry_idx]),
                'strategy': t.strategy, 'side': t.side,
                'entry': round(t.entry, 2), 'stop': round(t.stop, 2),
                'target': round(t.target, 2),
                'R': round(t.R or 0, 2), 'outcome': t.outcome,
            } for t in all_trades]
            csv_path = "C:/Users/rokip/OneDrive/Desktop/SMC_book_outputs/backtest/best_strategies_trades.csv"
            pd.DataFrame(rows).sort_values('date').to_csv(csv_path, index=False)
            print(f"\n  All {len(rows)} trades saved to: {csv_path}")


if __name__ == '__main__':
    main()
