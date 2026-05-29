"""
status_page.py — generate a public HTML status page.

Regenerated every 5 minutes by systemd timer.
Served by status_server.py on port 8080.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/home/ubuntu/common")
import track_record


OUT_PATH = "/home/ubuntu/common/status.html"

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="60">
<title>Pro Signal Bot · Live Status</title>
<style>
  body {{
    background: #0F1115; color: #E5E7EB;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    margin: 0; padding: 24px; line-height: 1.5;
  }}
  .container {{ max-width: 980px; margin: 0 auto; }}
  h1 {{
    font-size: 28px; margin: 0 0 8px;
    color: #FFD700; letter-spacing: -0.5px;
  }}
  .sub {{ color: #9CA3AF; font-size: 13px; margin-bottom: 32px; }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px; margin-bottom: 24px;
  }}
  .card {{
    background: #16191F; border: 1px solid #222731; border-radius: 10px;
    padding: 18px;
  }}
  .label {{ color: #9CA3AF; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }}
  .value {{ color: #FFD700; font-size: 22px; font-weight: 600; }}
  .value.green {{ color: #00C851; }}
  .value.red {{ color: #FF4444; }}
  .value.blue {{ color: #5865F2; }}
  table {{
    width: 100%; border-collapse: collapse; background: #16191F;
    border: 1px solid #222731; border-radius: 10px; overflow: hidden;
  }}
  th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #222731; }}
  th {{ background: #0F1115; color: #9CA3AF; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }}
  td {{ font-family: monospace; font-size: 13px; color: #E5E7EB; }}
  .green {{ color: #00C851; }}
  .red {{ color: #FF4444; }}
  .muted {{ color: #9CA3AF; }}
  .badge {{
    display: inline-block; padding: 3px 10px; border-radius: 4px;
    font-size: 11px; font-weight: 600;
  }}
  .badge.active {{ background: #00C851; color: #0F1115; }}
  .badge.stopped {{ background: #6b7280; color: #fff; }}
  footer {{ margin-top: 32px; color: #6b7280; font-size: 12px; text-align: center; }}
</style>
</head>
<body>
<div class="container">
  <h1>📊 Pro Signal Bot · Live Status</h1>
  <div class="sub">Auto-refresh every 60 seconds · Last regenerated: <strong>{now_str}</strong></div>

  <h2 style="font-size:16px;color:#E5E7EB;margin:24px 0 12px;">🤖 Services</h2>
  <table>
    <thead><tr><th>Service</th><th>Status</th><th>Uptime</th></tr></thead>
    <tbody>
      {service_rows}
    </tbody>
  </table>

  <h2 style="font-size:16px;color:#E5E7EB;margin:32px 0 12px;">📋 Forward-Test Track Record</h2>
  <div class="grid">
    <div class="card">
      <div class="label">Trades resolved</div>
      <div class="value">{n_trades}</div>
    </div>
    <div class="card">
      <div class="label">Win rate</div>
      <div class="value {wr_class}">{wr:.1f}%</div>
    </div>
    <div class="card">
      <div class="label">Total R</div>
      <div class="value {r_class}">{total_r:+.2f}R</div>
    </div>
    <div class="card">
      <div class="label">Mode</div>
      <div class="value blue">PAPER</div>
    </div>
  </div>

  <h2 style="font-size:16px;color:#E5E7EB;margin:32px 0 12px;">📈 Per-Strategy Performance</h2>
  <table>
    <thead><tr><th>Strategy</th><th>Trades</th><th>Win Rate</th><th>Total R</th></tr></thead>
    <tbody>
      {strat_rows}
    </tbody>
  </table>

  <h2 style="font-size:16px;color:#E5E7EB;margin:32px 0 12px;">📅 Recent Alerts (last 10)</h2>
  <table>
    <thead><tr><th>Time</th><th>Bot</th><th>System</th><th>Status</th><th>R</th></tr></thead>
    <tbody>
      {recent_rows}
    </tbody>
  </table>

  <footer>
    Pro Signal Bot · Educational only — not financial advice<br>
    BTCUSDT 4H trend / 1H entry · paper / dry-run mode<br>
    <span class="muted">Page auto-regenerates · API: alerts via Discord webhooks · backtest validated walk-forward</span>
  </footer>
</div>
</body>
</html>
"""


def is_active(svc):
    try:
        r = subprocess.run(["systemctl", "is-active", svc],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def uptime(svc):
    try:
        r = subprocess.run(["systemctl", "show", svc, "--property=ActiveEnterTimestamp"],
                           capture_output=True, text=True, timeout=3)
        line = r.stdout.strip()
        if "=" in line:
            ts = line.split("=", 1)[1].strip()
            return ts if ts else "—"
    except Exception:
        pass
    return "—"


def main():
    services = ["cryptobot.service", "daily_signal.service",
                "forward_tracker.timer", "weekly_report.timer",
                "monthly_recap.timer", "convergence_monitor.timer",
                "health_monitor.timer", "daily_summary.timer"]

    rows = []
    for s in services:
        active = is_active(s)
        badge = '<span class="badge active">ACTIVE</span>' if active else '<span class="badge stopped">STOPPED</span>'
        rows.append(f"<tr><td>{s}</td><td>{badge}</td><td>{uptime(s)[:19]}</td></tr>")
    service_rows = "\n      ".join(rows)

    summary = track_record.all_summary()
    n_trades = summary["n"]
    wr = summary["wr"]
    total_r = summary["total_r"]
    wr_class = "green" if wr >= 40 else ("red" if wr < 30 and n_trades > 5 else "")
    r_class = "green" if total_r > 0 else ("red" if total_r < 0 else "muted")

    per_strat = track_record.per_strategy_table()
    if per_strat:
        sorted_strats = sorted(per_strat.items(), key=lambda kv: -kv[1]["total_r"])
        strat_rows = "\n      ".join(
            f"<tr><td>{k}</td><td>{v['n']}</td><td>{v['wr']:.0f}%</td>"
            f"<td class='{('green' if v['total_r'] > 0 else 'red')}'>{v['total_r']:+.2f}R</td></tr>"
            for k, v in sorted_strats)
    else:
        strat_rows = '<tr><td colspan="4" class="muted">No strategy data yet — bots scanning, awaiting setups.</td></tr>'

    # Recent alerts from ledger
    recent_rows_list = []
    ledger_path = "/home/ubuntu/common/forward_results.jsonl"
    if os.path.exists(ledger_path):
        recents = []
        with open(ledger_path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get("event") == "close":
                        recents.append(r)
                except Exception:
                    pass
        for r in recents[-10:][::-1]:
            ts = (r.get("exit_time") or r.get("opened_at") or "—")[:16]
            status = r.get("status", "?")
            cls = "green" if status == "TP" else "red" if status == "SL" else "muted"
            r_val = r.get("r", 0)
            recent_rows_list.append(
                f"<tr><td>{ts}</td><td>{r.get('bot', '?')}</td><td>{r.get('system', '?')}</td>"
                f"<td class='{cls}'>{status}</td><td class='{cls}'>{r_val:+.2f}R</td></tr>")
    if not recent_rows_list:
        recent_rows_list.append('<tr><td colspan="5" class="muted">No alerts have resolved yet.</td></tr>')
    recent_rows = "\n      ".join(recent_rows_list)

    html = HTML_TEMPLATE.format(
        now_str=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        service_rows=service_rows,
        n_trades=n_trades, wr=wr, total_r=total_r,
        wr_class=wr_class, r_class=r_class,
        strat_rows=strat_rows, recent_rows=recent_rows,
    )

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[status_page] wrote {len(html):,} bytes")


if __name__ == "__main__":
    main()
