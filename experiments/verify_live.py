#!/usr/bin/env python3
"""Verify backfilled live records display losses correctly."""
import json

ld = json.load(open('/home/ubuntu/bot/logs/live_trades.json'))
print("=== Live trade records (post-backfill) ===")
total_pnl = 0
wins = 0
losses = 0
print(f"{'live_id':16s} {'strat':16s} {'side':4s} {'qty':>6s} "
      f"{'entry_actual':>12s} {'exit':>10s} {'pnl_usdt':>10s} {'R':>6s}")
for o in ld.get('closed_orders', []):
    pnl = o.get('realized_pnl_usdt')
    R = o.get('realized_R')
    if pnl is not None:
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1
    entry_actual = (o.get('entry_price_actual')
                    or o.get('entry_signal') or 0)
    exit_p = (o.get('exit_price')
              or o.get('exit_price_approx') or 0)
    pnl_str = f'${pnl:+.2f}' if pnl is not None else '   ?  '
    R_str = f'{R:+.2f}' if R is not None else '  ?  '
    print(f"{o['live_id']:16s} {o['strategy'][:16]:16s} "
          f"{o['side'][:4]:4s} {o.get('qty',0):>6.3f} "
          f"${entry_actual:>11.2f} ${exit_p:>9.2f} "
          f"{pnl_str:>10s} {R_str:>6s}")
print()
print(f'Wins: {wins}  Losses: {losses}  '
      f'Total realized P&L: ${total_pnl:+.2f}')

# Sanity check: any record still missing pnl?
missing = [o['live_id'] for o in ld.get('closed_orders', [])
           if o.get('realized_pnl_usdt') is None]
print(f'Records still missing realized_pnl_usdt: {len(missing)} '
      f'-> {missing}')
