# SRS Trading Bot — Local Clone

Personal algorithmic crypto trading bot — multi-strategy scanner with an
ML scoring layer, Bybit V5 integration, paper/live-shadow trading, and a
systemd-based deployment. Server hostnames, IPs and tokens are redacted;
fill in your own via `.env` (see `.env.example`).

## What's here

```
srs_bot/
├── bot/                      Main bot project
│   ├── bot/                  Discord bot + paper trader package
│   │   ├── discord_bot.py    SMC 15m signal poster + scan loop
│   │   ├── paper_trader.py   SMC paper trader (ML hook injected)
│   │   └── discord_webhook.py
│   ├── run_bot.py            Entry point — what cryptobot.service runs
│   ├── srs_main.py           Multi-strategy scanner
│   ├── config.py             Strategy params + risk caps
│   ├── strategy_engine.py    Strategy interface
│   ├── market_data.py        Bybit kline fetcher
│   ├── requirements.txt
│   ├── .env.example          Copy to .env and fill in
│   └── logs/                 (mostly empty — runtime data lives on VPS)
│       ├── ml_decisions.jsonl   Snapshot of ML scoring log
│       ├── ml_outcomes.jsonl    Snapshot of close outcomes
│       └── ml_state.json        Thresholds + decision count
│
├── common/                   Shared infrastructure modules
│   ├── live_trader.py        Bybit shadow orchestrator
│   │                         (watchdog · auto-resize · reconcile)
│   ├── bybit_client.py       HMAC-signed Bybit V5 REST wrapper
│   ├── book_paper_runner.py  s2/s3/s5/s6/s7/s8 runner (30s timer)
│   ├── smc_confluence.py     SMC entry strategy
│   ├── smc_book_strategies.py  s2-s8 book strategies
│   ├── regime.py             BULL/BEAR/CHOP detection
│   │
│   ├── ml_filter.py          Phase 0 v2 scorer (450 lines)
│   ├── ml_features.py        Feature extractor
│   ├── ml_alerter.py         Posts review/outcome embeds to ML channel
│   ├── ml_digest.py          Daily 00:05 UTC intelligence digest
│   │
│   ├── audit_chain.py        Tamper-evident audit
│   ├── circuit_breaker.py    Strategy circuit breaker
│   ├── risk_budget.py        Per-trade dollar risk cap
│   ├── webhook_queue.py      Durable Discord delivery
│   └── (~60 more)
│
└── systemd/                  Unit files (deployed to /etc/systemd/system/)
    ├── cryptobot.service       Long-running SMC bot
    ├── book_paper_runner.timer (every 30s)
    ├── book_paper_runner.service
    ├── ml_digest.timer         (00:05 UTC daily)
    ├── ml_digest.service
    ├── bot_watchdog.timer
    └── bot_watchdog.service
```

## Critical files (in order of importance)

| File | Why it matters |
|---|---|
| `common/live_trader.py` | Bybit interaction. Risk caps + watchdog + reconcile. |
| `common/ml_filter.py` | Phase 0 v2 scorer — gates trades by tier |
| `common/bybit_client.py` | All Bybit API calls — HMAC signing |
| `bot/bot/paper_trader.py` | SMC paper bot, ML hook injected |
| `common/book_paper_runner.py` | Book strategy runner, ML hook injected |
| `common/ml_digest.py` | Daily Discord intelligence report |

## ML stack summary

- **Model**: `phase0_heuristic_v2` (transparent weighted heuristic)
- **Weights tuned on**: 89 historical closed trades (EDA-driven)
- **OOS validation**: 78% wr / 82% loss-catch on 21 unseen trades
- **Mode**: `shadow` (no blocking) — set `ML_FILTER_MODE=gate` to enable
- **Gate min tier**: `MED` — set `ML_GATE_MIN_TIER=HIGH` for ultra-strict
- **Thresholds**: HIGH ≥ 0.72, MED ≥ 0.62, LOW ≥ 0.50

## Backtest history

Run any of these to re-validate (need VPS access):

```bash
python3 /tmp/ml_backtest.py    # baseline scoring of all closed trades
python3 /tmp/ml_eda.py         # bin analysis (which features predict losses)
python3 /tmp/ml_tune.py        # v2 weight tuning
python3 /tmp/ml_v3.py          # full layered (failed)
python3 /tmp/ml_v3_1.py        # surgical cooldown (failed)
python3 /tmp/ml_v3_2.py        # regime-adaptive (failed)
python3 /tmp/ml_real.py        # actual sklearn ML (LogReg/RF/GBM)
python3 /tmp/ml_radical.py     # KNN, Beta-bandit, ensembles
python3 /tmp/ml_final.py       # v2+RF ensemble + OOS test
```

## How to push changes back to VPS

```bash
# Example: update ml_filter.py
scp -i ~/.ssh/<KEY> common/ml_filter.py \
    <USER>@<VPS_HOST>:/tmp/ml_filter_new.py
ssh -i ~/.ssh/<KEY> <USER>@<VPS_HOST> "
  python3 -c 'import ast; ast.parse(open(\"/tmp/ml_filter_new.py\").read())'
  sudo cp /home/ubuntu/common/ml_filter.py /home/ubuntu/common/ml_filter.py.bak.\$(date +%s)
  sudo cp /tmp/ml_filter_new.py /home/ubuntu/common/ml_filter.py
  sudo systemctl restart cryptobot
"
```

## Operational toggles (on VPS in `/home/ubuntu/bot/.env`)

```
LIVE_TRADING_ENABLED=false|true       # Real money on/off
LIVE_RISK_PCT=0.01                    # 1% per trade
LIVE_STRATEGIES=...,...,...           # Whitelist
ML_FILTER_MODE=shadow|gate            # Filter active?
ML_GATE_MIN_TIER=MED|HIGH             # Cutoff when gated
DISCORD_WEBHOOK_URL_ML=...            # ML channel webhook
NOTIFY_USER_ID=<YOUR_DISCORD_USER_ID> # @ tag on paper opens
```

## Backups on VPS

Every deployment leaves a `.bak.before_<change>` next to the modified file.
Easy revert: `sudo cp <file>.bak.<name> <file> && sudo systemctl restart cryptobot`

## What's NOT in this clone

- `.env` (secrets — see `.env.example`)
- `tokens.json` (API tokens)
- Runtime logs (live on VPS only)
- Backup files (`.bak.*`)
- Webhook queue / dead letters
- Daily snapshots
- `__pycache__` / `*.pyc`

## VPS quick ref

```
ssh -i ~/.ssh/<KEY> <USER>@<VPS_HOST>

# Status
sudo systemctl status cryptobot book_paper_runner.timer ml_digest.timer

# Logs
sudo journalctl -u cryptobot -f
sudo journalctl -u book_paper_runner --since "10 min ago"

# Filter mode toggle
sudo sed -i 's/^ML_FILTER_MODE=.*/ML_FILTER_MODE=gate/' /home/ubuntu/bot/.env
sudo systemctl restart cryptobot
```
