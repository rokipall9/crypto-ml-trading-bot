# Twenty-strategy screen — full evaluation of all twenty (frozen spec)

Committed before any number below was computed. The strategies, costs,
execution and periods are exactly those of `SPEC_20.md`. That screen gated only
the top five. This one runs every check on all twenty and fixes the verdict
rules in advance.

What has already been seen: the selection and holdout Sharpe, total return
and drawdown of all twenty (results_20.json), and control p for the top
five. Everything else below (controls for the other fifteen, multiple-testing
adjustment, robustness, confidence intervals, early period) is new.

## 1. Gate, every strategy, every period

Same four checks as SPEC_20 (Sharpe > buy-and-hold perp, max DD ≤ 0.7 ×
buy-and-hold perp, circular-shift control, positive total), in:
- selection 2017-01-01 → 2020-12-31
- holdout 2021-01-01 → 2026-09-27
- **early 2014-01-01 → 2016-12-31, daily-decided strategies only** (T1–T6,
  D1–D5). Intraday-triggered ones (D6, D7, H1–H4, S1–S3) are excluded
  because pre-2017 minutes are too sparse for honest intraday fills. Hourly
  bars for this period are built from 2013-01-01 into a separate file.

Control: circular shift of the hourly exposure, 1,000 draws, offset uniform
in [720, N − 720] hours, seed 20260928.

## 2. Multiple testing

Twenty holdout control p-values, adjusted with Benjamini–Hochberg at
q = 0.10. Bonferroni at 0.05 / 20 = 0.0025 is also reported.

## 3. Robustness (holdout), each a separate re-run

- costs × 2 (0.21 % per side)
- costs × 0.5 (0.0525 % per side, maker-like)
- execution delayed one more hour
- funding 0.03 %/8h
- two parameter neighbours per strategy (below), same everything else
- 90 % confidence interval of holdout Sharpe: moving-block bootstrap of
  daily net returns, 30-day blocks, 2,000 resamples, seed 20260928

Parameter neighbours (low / high):

| strategy | default | low | high |
|---|---|---|---|
| T1 tsmom | 28 | 21 | 35 |
| T2 tsmom_multi ladder | 7,14,28,56,112 | ×0.75 → 5,10,21,42,84 | ×1.25 → 9,18,35,70,140 |
| T3 sma | 200 | 150 | 250 |
| T4 donchian | 20/10 | 15/8 | 25/13 |
| T5 golden cross | 50/200 | 40/150 | 60/250 |
| T6 target vol | 0.50 | 0.40 | 0.60 |
| D1 RSI(2) entry | < 10 | < 5 | < 15 |
| D2 drop threshold | −5 % | −4 % | −6 % |
| D3 hold days | 5 | 4 | 6 |
| D4 weekdays | none | — | — |
| D5 window days | 3 | 2 | 4 |
| D6, D7 k | 0.5 | 0.4 | 0.6 |
| H1 lookbacks (h) | 24/168 | 18/126 | 30/210 |
| H2 4h donchian | 20/10 | 15/8 | 25/13 |
| H3 band width | 2.0σ | 1.5σ | 2.5σ |
| H4 RSI entry | < 25 | < 20 | < 30 |
| S1 window start | 20:00 (4 h) | 19:00 | 21:00 |
| S2 decision hour | 22 (hold 1 h) | 21 (hold 2 h) | 20 (hold 3 h) |
| S3 decision hour | 13 (hold to 20:00) | 12 | 14 |

A parameterised re-implementation is used for neighbours; a test must show
its default parameters reproduce `screen20.decisions` exactly for all twenty.

## 4. Verdict per strategy (fixed now)

- **PASS**: holdout gate all four with the control significant after
  Benjamini–Hochberg (q = 0.10); holdout Sharpe > 0 in every robustness
  re-run; bootstrap 90 % CI lower bound > 0; and, where an early period
  exists, early Sharpe > early buy-and-hold perp.
- **PROMISING**: holdout Sharpe > buy-and-hold perp and positive total, and
  holdout Sharpe > 0 in every robustness re-run, but not PASS.
- **FAIL**: everything else.

None of these verdicts is live validation. Only prospective paper trading
is.
