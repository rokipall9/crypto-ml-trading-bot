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

---

# Twenty-strategy screen (`SPEC_20.md`, commit 6a4954e)

Twenty textbook strategies across four styles, ranked on **2017–2020**, then
the top five tested on **2021-01-01 → 2026-09-27**. One hourly engine runs
them all with the same costs (0.105 %/side, 0.01 %/8h long funding) and
execution (next :04 minute close). It reproduces the daily engine's holdout
numbers (tsmom28 0.78, tsmom_multi 0.64, tsmom_multi_vt 0.58).

## Top five (chosen on 2017–2020 only)

| # | strategy | Sharpe 17–20 | Sharpe 21+ | total 21+ | max DD 21+ | trades/yr | gate |
|---|---|---|---|---|---|---|---|
| 1 | T6 tsmom_multi_vt | 1.89 | 0.58 | +106 % | −47 % | 15 | fail (p 0.053) |
| 2 | T4 donchian20_10 | 1.75 | 0.44 | +73 % | −61 % | 8 | fail (DD, p 0.22) |
| 3 | T2 tsmom_multi | 1.73 | 0.64 | +153 % | −51 % | 16 | fail (p 0.036) |
| 4 | T3 sma200 | 1.49 | 0.48 | +94 % | −66 % | 5 | fail (DD, p 0.16) |
| 5 | D5 turn_of_month | 1.48 | 0.17 | +8 % | −48 % | 12 | fail (Sharpe, p 0.39) |
| — | buy-and-hold perp | 1.32 | 0.42 | +54 % | −79 % | — | — |

**No top-five strategy passes the holdout gate** (control bar p < 0.01). The
four trend rules still beat buy-and-hold on Sharpe in 2021+; turn-of-month
did not survive.

## The other fifteen, 2021+ Sharpe

tsmom28 0.78 · golden cross 0.39 · rsi2 0.47 · weekdays 0.17 ·
vol breakout (trend) 0.05 · 4h Bollinger 0.00 · 4h Donchian −0.01 ·
panic dip −0.01 · three down −0.03 · vol breakout −0.14 · hourly RSI −0.54 ·
hourly trend −1.45 · US-open momentum −1.98 · evening −2.47 ·
last-hour momentum −4.63

## What it says

- **Trading frequency decides survival.** Every strategy trading more than
  about 50 times a year lost money after costs in 2021+. The four worst
  trade 180–365 times a year and lost 86–97 %. At 0.105 % per side, a daily
  round trip costs about 77 % a year before any edge.
- **Daily trend is the only family positive in both periods.** It is also
  the family already seen on 2021+ in earlier rounds, so this holdout is not
  clean for it.
- **Selection carried over** (Spearman 0.74 between the two periods), mostly
  because trend is good and intraday is bad in both.
- `D1_rsi2` (ranked 13th on selection) had 2021+ Sharpe 0.47 with the
  smallest drawdown of any positive strategy (−31 %). Noticing that in the
  holdout is itself selection, so it is only a candidate for a future
  pre-registered test, not a finding.
