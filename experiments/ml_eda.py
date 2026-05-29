#!/usr/bin/env python3
"""
ml_eda.py — Look at the actual data. For each feature, what's the win
rate / loss rate by bin? Tells us which features carry real signal
and which are noise. Output drives the new weight tuning.
"""
import json
import sys
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, "/home/ubuntu/common")
sys.path.insert(0, "/tmp")
# We need the same loader the backtest uses
exec(open("/tmp/ml_backtest.py").read().split("def main()")[0])


def main():
    rows = _load_all_closes()
    rows.sort(key=lambda r: r["closed_at"] or "")
    print(f"Loaded {len(rows)} closes; {sum(1 for r in rows if r['R']>0)} wins,",
          f"{sum(1 for r in rows if r['R']<0)} losses,",
          f"{sum(1 for r in rows if r['R']==0)} flat")
    print()

    # Build feature dataset
    dataset = []
    for t in rows:
        s = _backtest_score(t, rows)
        if s is None:
            continue
        f = s["features"]
        dataset.append({
            "id": t["id"],
            "strategy": t["strategy"],
            "side": t["side"],
            "R": t["R"],
            "pnl": t["pnl"],
            "win": 1 if t["R"] > 0 else (0 if t["R"] == 0 else -1),
            "target_R": f["target_R"],
            "hour": f["hour_utc"],
            "dow": f["dow"],
            "strat_wr": f["strategy_winrate_10"],
            "strat_avgR": f["strategy_avg_R_10"],
            "strat_n": f["strategy_n_samples"],
        })

    def report_bin(name, bin_fn, sort_key=str):
        print(f"━━━ {name} ━━━")
        groups = defaultdict(list)
        for d in dataset:
            groups[bin_fn(d)].append(d)
        print(f"  {'bin':25s} {'n':>4s} {'wins':>5s} {'losses':>7s} {'flat':>5s} "
              f"{'wr':>7s} {'avgR':>8s} {'sumPnL':>10s}")
        for k in sorted(groups.keys(), key=sort_key):
            g = groups[k]
            n = len(g)
            w = sum(1 for x in g if x["win"] == 1)
            l = sum(1 for x in g if x["win"] == -1)
            f_ = sum(1 for x in g if x["win"] == 0)
            wr = w / (w + l) if (w + l) else 0
            avgR = sum(x["R"] for x in g) / n if n else 0
            sumpnl = sum(x["pnl"] for x in g)
            print(f"  {str(k)[:25]:25s} {n:>4d} {w:>5d} {l:>7d} {f_:>5d} "
                  f"{wr*100:>6.1f}% {avgR:>+8.2f} {sumpnl:>+10.2f}")
        print()

    # ─── Single-feature analyses ─────────────────────────────────
    report_bin("By STRATEGY",
               lambda d: d["strategy"])

    report_bin("By SIDE",
               lambda d: d["side"])

    report_bin("By STRATEGY+SIDE",
               lambda d: f"{d['strategy']}|{d['side']}")

    report_bin("By strat_wr bucket (rolling 10-trade)",
               lambda d: ("(none)" if d["strat_n"] == 0 else
                          "0-30%" if d["strat_wr"] < 0.30 else
                          "30-45%" if d["strat_wr"] < 0.45 else
                          "45-55%" if d["strat_wr"] < 0.55 else
                          "55-70%" if d["strat_wr"] < 0.70 else
                          "70%+"))

    report_bin("By strat_avgR bucket",
               lambda d: ("(none)" if d["strat_n"] == 0 else
                          "<-0.5" if d["strat_avgR"] < -0.5 else
                          "-0.5..0" if d["strat_avgR"] < 0 else
                          "0..+0.5" if d["strat_avgR"] < 0.5 else
                          "+0.5..+1" if d["strat_avgR"] < 1.0 else
                          ">+1"))

    report_bin("By target_R bucket",
               lambda d: ("<1.5R" if d["target_R"] < 1.5 else
                          "1.5-2.0R" if d["target_R"] < 2.0 else
                          "2.0-2.5R" if d["target_R"] < 2.5 else
                          "2.5-3.0R" if d["target_R"] < 3.0 else
                          "3.0R+"))

    report_bin("By hour_utc (binned 4h)",
               lambda d: f"{d['hour']//4*4:02d}-{d['hour']//4*4+4:02d}",
               sort_key=lambda x: int(x[:2]))

    report_bin("By day_of_week",
               lambda d: ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][d["dow"]],
               sort_key=lambda x: ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].index(x))

    # ─── Strategy combined with strat_wr — does a losing strategy
    # combined with a known-bad strategy double-down? ──────────────
    report_bin("By strategy + winrate bucket",
               lambda d: (f"{d['strategy'][:14]}|" +
                          ("none" if d["strat_n"] == 0 else
                           "low" if d["strat_wr"] < 0.40 else
                           "mid" if d["strat_wr"] < 0.60 else
                           "hi")),
               sort_key=str)


if __name__ == "__main__":
    main()
