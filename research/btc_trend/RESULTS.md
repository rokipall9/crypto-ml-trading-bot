# BTC daily trend study — results

Rules were written and committed before each round was scored:
`SPEC.md` (a676010), `SPEC_v2.md` (aeb3554), `SPEC_v3.md` (66187e5).
Data: Bitstamp BTC/USD 1-minute candles (ff137/bitstamp-btcusd-minute-data),
2012 → 2026-09-27, zero gaps, zero OHLC violations; cross-checked within
0.03–0.06 % of two real Bybit fills from 2026-09-23.

All numbers are after 0.105 % per side (taker fee + slippage) and 0.01 %/8h
perp funding on long exposure, executed at 00:05 UTC the day after the signal.

## Verdicts

| round | period | status | result |
|---|---|---|---|
| v1 | test 2021-01-01 → 2026-09-27 | pre-registered | **all 4 fail**; `C3_tsmom28` passes 3/4, misses control (p 0.015 vs 0.0125) |
| v2 | same test period | exploratory (v1 already seen) | **all 4 fail** the control (p 0.034–0.06); long-only variants beat buy-and-hold on Sharpe and drawdown |
| v3 | **2014-01-01 → 2016-12-31, never scored before** | pre-registered | **both pass all 4 checks** |

## The numbers

Test period 2021-01-01 → 2026-09-27 (5.7 years):

| | total | CAGR | Sharpe | max DD | time in market |
|---|---|---|---|---|---|
| buy-and-hold perp | +55 % | 8 % | 0.42 | −79 % | 100 % |
| C3 `tsmom28` | +279 % | 26 % | 0.78 | −47 % | 54 % |
| V1 `tsmom_multi` | +153 % | 18 % | 0.64 | −51 % | avg exposure 53 % |

Unseen early period 2014-01-01 → 2016-12-31:

| | total | Sharpe | max DD | control p |
|---|---|---|---|---|
| buy-and-hold perp | −5 % | 0.30 | −83 % | — |
| C3 `tsmom28` | +205 % | 1.14 | −42 % | 0.000 |
| V1 `tsmom_multi` | +148 % | 1.02 | −37 % | 0.003 |

Robustness (v2 diagnostic): every single lookback from 14 to 98 days beats
buy-and-hold on Sharpe in both 2017–2020 and 2021+. Only 7 days fails. The
effect is a plateau, not one lucky parameter.

## What this does and does not show

- **Shows:** a daily "hold BTC only while it is above where it was N weeks
  ago" rule beat holding BTC, after realistic costs, in three separate eras,
  and roughly halved the worst drawdown. It passed a pre-registered gate on
  history nobody had scored.
- **Weakening:** the edge is smaller in 2021+ than before, and 2025 was a
  losing year (−19 % vs buy-and-hold −16 %). Recent evidence alone is
  marginal (p 0.015–0.06).
- **Does not show:** that it will make money from here. The only validation
  left is prospective paper trading.
- **Different risk from the current bot:** this holds up to 1× the account
  in BTC with no stop. A −40 % to −50 % account drawdown has happened in
  every era. It is not a $2-per-trade system.
- **Not tested:** other coins (exchange data is blocked from the study
  environment), real historical funding, and intraday execution.

## Small-account replay (test period, 0.001 BTC lot, floor to lot)

| start | C3 `tsmom28` end | max DD | V1 `tsmom_multi` end | max DD |
|---|---|---|---|---|
| $257 | $945 | −46 % | $656 | −45 % |
| $2,650 | $10,052 | −47 % | $6,644 | −50 % |

No entry was skipped for being below one lot.

## Reproduce

```
cd research/btc_trend
python3 fetch_data.py            # downloads ~145 MB, verifies, builds daily bars
python3 -m unittest test_backtest test_backtest_v2
python3 backtest.py && python3 backtest_v2.py && python3 backtest_v3.py
```

## Suggested next step

Run `V1_tsmom_multi` (the pre-registered multi-horizon version, less
dependent on one lookback than `tsmom28`) as a **paper-only** daily signal
for a fixed period before any real money, and have Codex review this study
first. Nothing here changes live trading.
