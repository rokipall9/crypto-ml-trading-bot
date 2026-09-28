# BTC daily trend study — frozen spec (v1)

Written and committed **before any result was computed**. The commit that adds
this file is the freeze point; `backtest.py` prints this file's SHA-256 on
every run so a changed spec cannot pass silently. Changing any rule below
after seeing results makes the run exploratory, not a test of this spec.

## Why this family

Every intraday candidate in this project (SMC, s2_mss, s3_ote, s3r, funding,
H1, CT1, TR20, RS28) died on the same two things once measured honestly:
execution costs that are large relative to a tight stop, and fills that live
can't get. A daily, low-turnover trend filter attacks both: a handful of
round trips per year makes costs near zero in R terms, and a market order a
few minutes after the daily close needs no resting-order fiction.

Source of edge being tested: time-series momentum. Documented across asset
classes (Moskowitz, Ooi & Pedersen 2012; Hurst, Ooi & Pedersen 2017) and in
crypto specifically at 1–4 week horizons (Liu & Tsyvinski 2021). The expected
benefit is **drawdown reduction** versus holding BTC: staying out of long
bear phases, not predicting tops. It is expected to lose in sideways chop.

## Data

- Bitstamp BTC/USD 1-minute candles, `ff137/bitstamp-btcusd-minute-data`
  (historical 2012–2025-01-07 + daily updates). Fetched by `fetch_data.py`.
- Integrity required before use: continuous 60 s grid, no OHLC violations,
  no conflicting overlap between the historical and update files.
- Cross-check done at spec time: Bybit perp fills on 2026-09-23 were within
  0.03–0.06 % of Bitstamp minute prices. Spot is a proxy for the perp.
- Daily bar D = minutes in [D 00:00, D+1 00:00) UTC. Loaded from 2015-01-01.

## Candidates (all four reported, none selected after the fact)

All long-only, flat otherwise. `close` = daily close. Signal on day D uses
closes up to and including D.

| id | rule |
|---|---|
| C1 `sma200` | long while close > SMA(200) of closes |
| C2 `donchian20_10` | enter when close > max(close of prior 20 days); exit when close < min(close of prior 10 days) |
| C3 `tsmom28` | long while close / close 28 days earlier − 1 > 0 |
| C4 `ensemble` | exposure = mean of C1, C2, C3 signals (0, ⅓, ⅔ or 1) |

No parameter was chosen from this dataset: 200/20/10/28 are the textbook
values these rules are published with.

## Execution and costs

- Signal from day D's close is executed at the **close of the 00:04 UTC minute
  of day D+1** (≈5 minutes after the daily close).
- Per-side cost on every exposure change: taker fee 0.055 % + slippage/basis
  0.050 % = **0.105 %** × |Δexposure|.
- Perp funding while long: **0.01 % per 8 h (0.03 %/day) × exposure**,
  primary. Sensitivity reported at 0 % and 0.03 %/8 h. Constant funding is an
  approximation (historical funding data unreachable from the study box);
  trend-long periods coincide with high funding, so 0.01 % may understate cost
  in strong bulls.
- Benchmarks: buy-and-hold spot (no funding) and buy-and-hold perp (same
  funding as the strategies, one entry cost).

## Periods

- Warmup: 2015-01-01 → 2016-12-31 (not scored).
- **Reference**: 2017-01-01 → 2020-12-31.
- **Test**: 2021-01-01 → last complete day in the data.
- Per-calendar-year results for every candidate.

BTC's history is widely known and trend-following on BTC is widely discussed,
so even the test period is **reused history, exploratory**. Only a
prospective paper run can validate anything.

## Metrics

CAGR, annualised vol, Sharpe (daily, rf = 0, √365), max drawdown, time in
market, round trips, round-trip win rate, per-year return.

## Control

Circular shift of each candidate's exposure series within the test period
(1,000 draws, offset uniform in [30, N−30] days, seed 20260928). This keeps
exposure, turnover and holding lengths identical and breaks only the timing.
p = share of shifted Sharpe ≥ actual Sharpe. With 4 candidates the bar is
p < 0.0125 (Bonferroni).

## Pass criteria (test period, primary costs), all required

1. Sharpe > buy-and-hold **perp** Sharpe.
2. Max drawdown ≤ 0.7 × buy-and-hold perp max drawdown.
3. Control p < 0.0125.
4. Positive total return in the test period.

A pass means "worth a prospective paper run", nothing more. No live change.

## Small-account check (reported, not a pass criterion)

Replay the test period in dollars with Bybit's 0.001 BTC lot (floor to lot,
skip if below one lot) for $257 and $2,650 starting equity, to show whether
the exposure steps survive rounding.
