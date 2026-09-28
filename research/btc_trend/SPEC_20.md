# Twenty-strategy screen — frozen spec

Written and committed before any of these twenty was scored. Goal: rank 20
candidates on one period, then test whether the top five hold up on a
later period they were not selected on.

## Data and engine

- Bitstamp BTC/USD minutes (same source and integrity checks as SPEC.md),
  aggregated to **hourly** bars from 2016-01-01 (warmup) onward. Daily bars
  are built from the hourly bars.
- Every strategy is expressed as an hourly exposure series in [−1, 1]. A
  decision made at the close of hour h (using data up to that close) is
  held over the interval exec_px[h+1] → exec_px[h+2], where exec_px[h] is
  the close of minute :04 of hour h. For daily rules, that is the same 00:05
  UTC execution as SPEC.md.
- Costs identical to SPEC.md: 0.105 % per side × |Δexposure|; funding
  0.01 %/8h on long exposure only (shorts receive nothing). Returns are
  summed into UTC days for all metrics.

## The twenty (textbook parameters, none fitted here)

Daily trend
1. `T1_tsmom28` long while close > close 28 days ago
2. `T2_tsmom_multi` share of {7,14,28,56,112}-day lookbacks that are positive
3. `T3_sma200` long while close > SMA200
4. `T4_donchian20_10` enter above prior-20 max close, exit below prior-10 min
5. `T5_golden_cross` long while SMA50 > SMA200
6. `T6_tsmom_multi_vt` T2 × min(1, 0.5/σ30), 0.10 no-trade band (SPEC_v2 V2)

Daily mean reversion / calendar
7. `D1_rsi2` enter when RSI(2) < 10 and close > SMA200; exit when close > SMA5
8. `D2_panic_dip` enter after a daily close-to-close return ≤ −5 %; hold 3 days
9. `D3_three_down` enter after three lower closes in a row while close > SMA200; hold 5 days
10. `D4_weekdays` long Monday–Friday UTC, flat Saturday and Sunday
11. `D5_turn_of_month` long from the close three days before month end to the close of the third day of the next month
12. `D6_vol_breakout` each day, level = open + 0.5 × previous day's range; after the first hourly close above level, long until the next 00:05 exit
13. `D7_vol_breakout_trend` D6, only on days whose previous close > SMA20

Intraday (hourly and 4-hour bars)
14. `H1_hourly_trend` long while both 24 h and 168 h returns > 0
15. `H2_4h_donchian` 4-hour bars: enter above prior-20 max close, exit below prior-10 min
16. `H3_4h_bollinger` 4-hour bars: enter when close < SMA20 − 2σ and close > SMA200; exit when close ≥ SMA20
17. `H4_hourly_rsi` enter when RSI(14) < 25 and close > SMA200 (hourly); exit when RSI > 50

Time of day
18. `S1_evening` long every day 20:00 → 24:00 UTC
19. `S2_last_hour_momentum` at the 23:00 close, if the day's return so far > 0, long the final hour
20. `S3_us_open_momentum` at the 14:00 close, if the day's return so far > 0, long 14:00 → 20:00

Any hourly exposure change is executed at the next :04 exec price, which
makes intraday rules pay a full round trip per trade. That is deliberate:
it is what the live bot would pay.

## Periods

- Warmup 2016-01-01 → 2016-12-31. Pre-2017 minutes are too sparse for
  honest intraday fills, so nothing earlier is used.
- **Selection: 2017-01-01 → 2020-12-31.** All twenty ranked by net Sharpe.
- **Holdout: 2021-01-01 → 2026-09-27.** The top five by selection Sharpe
  are evaluated here. All twenty holdout numbers are also reported, with
  the Spearman rank correlation between selection and holdout Sharpe.

Honesty note: the trend family (1–6) has already been scored on 2021+ in
SPEC.md/SPEC_v2.md, so its holdout is not clean. Families 7–20 have not.

## Holdout gate for the top five (all required)

1. Sharpe > buy-and-hold perp Sharpe (holdout)
2. max DD ≤ 0.7 × buy-and-hold perp max DD
3. control p < 0.05 / 5: circular shift of the hourly exposure series by
   an offset uniform in [720, N − 720] hours (any hour, so time-of-day
   alignment is broken too), 1,000 draws, seed 20260928
4. positive total return
