#!/usr/bin/env python3
"""
backfill_pnl.py — Fix historical live-trade records using Bybit's authoritative
/v5/position/closed-pnl endpoint.

Matching strategy:
  • Walk Bybit close events in chronological order.
  • For each event, find unmatched live trades whose closed_at is within
    a 15-minute window AFTER the event's updatedTime.
  • From those candidates, pick a subset whose qty SUM matches event.qty
    (exact within 0.001 BTC tolerance). Prefer the EARLIEST candidates
    (lowest close-time first).
  • Assign that subset to the event; prorate closedPnl by qty share.
  • Unmatched events / live trades are reported separately.
"""
import sys, json, os
from datetime import datetime, timezone
from itertools import combinations

sys.path.insert(0, '/home/ubuntu')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/bot/.env')
from common.bybit_client import BybitClient

LIVE_PATH = '/home/ubuntu/bot/logs/live_trades.json'
BACKUP_PATH = LIVE_PATH + '.bak.before_backfill'

with open(LIVE_PATH) as f:
    live = json.load(f)
if not os.path.exists(BACKUP_PATH):
    with open(BACKUP_PATH, 'w') as f:
        json.dump(live, f, indent=2, default=str)
    print(f'Backup: {BACKUP_PATH}')

client = BybitClient()
all_pnl = []
cursor = ''
while True:
    pool = client._request('GET', '/v5/position/closed-pnl',
                           params={'category': 'linear',
                                   'symbol': 'BTCUSDT',
                                   'limit': 50,
                                   **({'cursor': cursor} if cursor else {})})
    all_pnl.extend(pool.get('list', []))
    cursor = pool.get('nextPageCursor', '')
    if not cursor:
        break

def pnl_ms(p):
    try:
        return int(p.get('updatedTime') or p.get('createdTime') or 0)
    except Exception:
        return 0

all_pnl.sort(key=pnl_ms)
print(f'Fetched {len(all_pnl)} closed-pnl events from Bybit')


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


def to_ms(dt):
    return int(dt.timestamp() * 1000) if dt else 0


closed = live.get('closed_orders', [])
# Each live trade gets a sortable record: (closed_ms, side, qty, order_ref)
candidates = []
for o in closed:
    closed_at = parse_iso(o.get('closed_at') or '')
    if not closed_at:
        continue
    entry_side = o.get('bybit_side', 'Buy')
    close_side = 'Sell' if entry_side == 'Buy' else 'Buy'
    candidates.append({
        'order': o,
        'closed_ms': to_ms(closed_at),
        'close_side': close_side,
        'qty': float(o.get('qty', 0)),
        'matched': False,
    })


def match_subset(items, target_qty, tol=0.0011):
    """Find subset of items whose qty sum == target (within tol). Prefer
    smaller subsets and earlier closed_ms. Returns subset or None."""
    # Try sizes 1..len(items), prefer earliest items
    items_sorted = sorted(items, key=lambda c: c['closed_ms'])
    for size in range(1, len(items_sorted) + 1):
        for combo in combinations(items_sorted, size):
            total = sum(c['qty'] for c in combo)
            if abs(total - target_qty) <= tol:
                return list(combo)
    return None


report = []
total = 0.0
unmatched_events = []
for ev in all_pnl:
    ev_ms = pnl_ms(ev)
    ev_side = ev.get('side')
    ev_qty = float(ev.get('qty', 0))
    ev_pnl = float(ev.get('closedPnl', 0))
    ev_exit = float(ev.get('avgExitPrice', 0))
    ev_entry = float(ev.get('avgEntryPrice', 0))
    # Candidates: same close_side, closed within 0..15 min AFTER event
    window_ms = 15 * 60 * 1000
    pool = [c for c in candidates
            if not c['matched']
            and c['close_side'] == ev_side
            and ev_ms - 60_000 <= c['closed_ms'] <= ev_ms + window_ms]
    subset = match_subset(pool, ev_qty)
    if subset is None:
        unmatched_events.append((ev_ms, ev_side, ev_qty, ev_pnl, ev_exit))
        continue
    # Apply
    total_subset_qty = sum(c['qty'] for c in subset)
    for c in subset:
        c['matched'] = True
        o = c['order']
        share = c['qty'] / total_subset_qty if total_subset_qty > 0 else 1.0
        realized = round(ev_pnl * share, 4)
        o['exit_price'] = ev_exit
        o['exit_price_approx'] = ev_exit
        o['exit_source'] = 'bybit_closed_pnl_backfill'
        o['entry_price_actual'] = ev_entry
        o['realized_pnl_usdt'] = realized
        risk = float(o.get('risk_usdt', 0))
        if risk > 0:
            o['realized_R'] = round(realized / risk, 3)
        total += realized
        report.append((o['live_id'], o.get('strategy', '?'), c['qty'],
                       o.get('closed_at', '?')[:19], ev_exit, realized))

# Save
with open(LIVE_PATH + '.tmp', 'w') as f:
    json.dump(live, f, indent=2, default=str)
os.replace(LIVE_PATH + '.tmp', LIVE_PATH)

# Report
print()
print('=== BACKFILL REPORT ===')
report.sort(key=lambda r: r[3])
print(f"{'live_id':16s} {'strategy':16s} {'qty':>7s} {'closed_at':21s} "
      f"{'exit':>11s} {'realized_usdt':>14s}")
for live_id, strat, qty, closed_at, ex, real in report:
    print(f'{live_id:16s} {strat[:16]:16s} {qty:>7.3f} {closed_at:21s} '
          f'${ex:>10.2f} ${real:>+13.4f}')

unmatched_live = [c for c in candidates if not c['matched']]
print()
print(f'Total matched: {len(report)} live trades')
print(f'Unmatched live trades: {len(unmatched_live)}')
for c in unmatched_live:
    print(f'  live_id={c["order"]["live_id"]} qty={c["qty"]} '
          f'closed_at={c["order"].get("closed_at","?")[:19]} '
          f'status={c["order"].get("status","?")}')
print(f'Unmatched Bybit events: {len(unmatched_events)}')
for ev_ms, side, qty, pnl, exit_p in unmatched_events:
    dt = datetime.fromtimestamp(ev_ms/1000, tz=timezone.utc).isoformat()[:19]
    print(f'  {dt} {side} qty={qty} closedPnl=${pnl:+.2f} exit=${exit_p}')
print()
print(f'Total live realized P&L (backfilled): ${total:+.4f}')
bal = client.get_balance('USDT')
print(f'Current Bybit balance: ${bal:.2f}  (drop from $499.76: ${bal-499.76:+.2f})')
print(f'P&L attribution vs balance drop: ${total - (bal - 499.76):+.4f} '
      f'(remainder = fees + funding + unmatched events)')
