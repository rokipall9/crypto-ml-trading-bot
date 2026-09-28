# BTC daily trend study — v3 spec: the unseen early period

Written and committed before any 2014–2016 result was computed. Nothing in
this project (v1, v2, or the Bybit-era studies) has scored BTC before 2017,
so this is the only untouched history available for these rules.

## Question

Do the two candidates that looked best in v1/v2 also beat holding BTC in a
period neither was looked at in?

## Candidates (fixed, from earlier specs, no changes)

- `C3_tsmom28` (SPEC.md)
- `V1_tsmom_multi` (SPEC_v2.md)

## Data and period

- Same Bitstamp minute source and integrity checks. Daily bars from
  2013-01-01 written to a separate file (`btc_daily_2013.csv`) so the v1/v2
  inputs are untouched.
- **Early period: 2014-01-01 → 2016-12-31.** 2013 is warmup only.
- Pre-2017 Bitstamp minutes include many flat, zero-volume fills; daily
  close and the 00:04 execution price are the last traded prices, which is
  adequate at daily resolution but noisier than later years.

## Execution, costs, control, gate

Identical to SPEC.md: 00:04 UTC execution, 0.105 % per side, 0.01 %/8h
funding on long exposure (perp funding did not exist for most of this
period; kept as a conservative cost), circular-shift control with 1,000
draws and seed 20260928. Pass requires, in the early period:

1. Sharpe > buy-and-hold perp,
2. max DD ≤ 0.7 × buy-and-hold perp,
3. control p < 0.05 / 2,
4. positive total return.
