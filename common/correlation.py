"""
correlation.py — cross-bot correlation check.

Reads audit.jsonl from daily_signal + pro_signal, and the cryptobot log.
For each bot: extract the DAYS it qualified/fired on.
Check: do the 3 bots fire on overlapping days? If yes → redundant.
If no → truly independent edges.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict, Counter


PATHS = {
    "daily_signal": "/home/ubuntu/daily_signal/state/audit.jsonl",
    "pro_signal":   "/home/ubuntu/pro_signal/state/audit.jsonl",
    "cryptobot":    "/home/ubuntu/bot/logs/bot_live.log",
}


def qualify_days_jsonl(path):
    days = set()
    if not os.path.exists(path):
        return days
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") != "scan":
                continue
            if not r.get("qualifies"):
                continue
            bt = r.get("bar_time", "")
            if len(bt) >= 10:
                days.add(bt[:10])
    return days


def qualify_days_cryptobot(path):
    """cryptobot log — find SIGNAL lines with implicit dates."""
    # Log has no explicit per-line timestamp, but 'No signal' lines include
    # price so we can at least count unique SIGNAL events. For cross-day
    # correlation we'd need timestamps — cryptobot writes at 4H UTC closes
    # so SIGNAL lines appear between "[4H] Scan at HH:MM UTC" markers.
    # Rough heuristic: count every SIGNAL line as one day hit (cryptobot
    # can't fire twice the same day due to strategy cooldowns).
    days = set()
    if not os.path.exists(path):
        return days
    current_date = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            # Capture date from log timestamps in format "YYYY-MM-DDTHH:MM"
            m = re.search(r"(\d{4}-\d{2}-\d{2})T", line)
            if m:
                current_date = m.group(1)
            elif "[4H] SIGNAL" in line and current_date:
                days.add(current_date)
    return days


def main():
    all_days = {}
    for bot, path in PATHS.items():
        if "log" in path:
            all_days[bot] = qualify_days_cryptobot(path)
        else:
            all_days[bot] = qualify_days_jsonl(path)

    print("=" * 72)
    print("  CROSS-BOT CORRELATION — qualifying-day overlap")
    print("=" * 72)
    for bot, days in all_days.items():
        print("  %-16s  %3d qualifying days" % (bot, len(days)))
    print()

    bots = list(all_days.keys())
    print("  OVERLAPS (days where >=2 bots qualified)")
    print("  " + "-" * 60)
    for i, b1 in enumerate(bots):
        for b2 in bots[i+1:]:
            overlap = all_days[b1] & all_days[b2]
            total1, total2 = len(all_days[b1]), len(all_days[b2])
            pct = 100 * len(overlap) / max(1, min(total1, total2))
            print("  %s ↔ %s: %d overlapping days (%.1f%% of smaller set)" % (
                b1, b2, len(overlap), pct))

    # 3-way overlap
    if len(bots) == 3:
        triple = all_days[bots[0]] & all_days[bots[1]] & all_days[bots[2]]
        print("  All three overlap: %d days" % len(triple))

    # Combined unique "alert days"
    union = set()
    for days in all_days.values():
        union |= days
    print()
    print("  Total unique days with at least one qualifying setup: %d" % len(union))
    print()
    print("  Interpretation:")
    if len(union) == 0:
        print("    No data yet — none of the bots have qualifying days recorded.")
    else:
        avg_overlap_pct = 0
        pair_count = 0
        for i, b1 in enumerate(bots):
            for b2 in bots[i+1:]:
                overlap = all_days[b1] & all_days[b2]
                smaller = min(len(all_days[b1]), len(all_days[b2]))
                if smaller:
                    avg_overlap_pct += 100 * len(overlap) / smaller
                    pair_count += 1
        if pair_count:
            avg = avg_overlap_pct / pair_count
            if avg > 50:
                print("    ❌ HIGH overlap (%.0f%% avg). Bots are redundant — one edge in 3 wrappers." % avg)
            elif avg > 20:
                print("    ⚠  MODERATE overlap (%.0f%% avg). Some diversification but not fully independent." % avg)
            else:
                print("    ✓ LOW overlap (%.0f%% avg). Bots fire on different days — real diversification." % avg)


if __name__ == "__main__":
    main()
