#!/usr/bin/env python3
"""Audit paper trades against the corresponding live trades."""
import json

ld = json.load(open('/home/ubuntu/bot/logs/live_trades.json'))
pb = json.load(open('/home/ubuntu/bot/logs/paper_trades_book.json'))
ps = json.load(open('/home/ubuntu/bot/logs/paper_trades.json'))

paper_lookup = {}
for src, path in [('book', pb), ('smc', ps)]:
    for t in path.get('closed_trades', []) + path.get('open_trades', []):
        paper_lookup[t.get('id', '')] = (src, t)

print(f"{'live_id':14s} {'strat':14s} {'live_pnl':>10s} {'paper_id':14s} "
      f"{'paper_status':24s} {'paper_pnl':>10s} {'consistent?':12s}")
for o in ld.get('closed_orders', []):
    live_pnl = o.get('realized_pnl_usdt')
    pid = o.get('paper_trade_id', '')
    src, t = paper_lookup.get(pid, (None, None))
    if t is None:
        paper_status = 'NOT_FOUND'
        paper_pnl = None
    else:
        paper_status = t.get('status', '?')
        paper_pnl = t.get('realized_pnl', 0)
    # Sign agreement
    if live_pnl is None or paper_pnl is None:
        cons = '?'
    elif (live_pnl > 0) == (paper_pnl > 0):
        cons = 'OK'
    else:
        cons = 'MISMATCH'
    live_pnl_s = f'${live_pnl:+.2f}' if live_pnl is not None else '?'
    paper_pnl_s = f'${paper_pnl:+.2f}' if paper_pnl is not None else '?'
    print(f"{o['live_id']:14s} {o['strategy'][:14]:14s} "
          f"{live_pnl_s:>10s} {pid[:14]:14s} {paper_status:24s} "
          f"{paper_pnl_s:>10s} {cons:12s}")
