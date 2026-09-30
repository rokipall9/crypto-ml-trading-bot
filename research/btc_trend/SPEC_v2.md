# BTC daily trend study — v2 spec (exploratory)

Written and committed before any v2 result was computed. Same data,
execution, costs, funding, periods, control and pass criteria as `SPEC.md`
(v1) unless stated here.

## Status: exploratory, and why

v1 has already been scored on the test period (2021-01-01 → 2026-09-27),
and I have seen that `tsmom28` did best there. Every v2 candidate below is
designed from published practice rather than from the v1 numbers, but
because the author has seen v1's test results, **no v2 result counts as
independent validation**. Only prospective paper trading can do that.

## Candidates (all reported)

| id | rule |
|---|---|
| V1 `tsmom_multi` | long-only; exposure = share of lookbacks L ∈ {7, 14, 28, 56, 112} days with close/close[L] − 1 > 0 (0, 0.2 … 1) |
| V2 `tsmom_multi_vt` | V1 × min(1, 0.50 / σ30), σ30 = annualised stdev of the last 30 daily close-to-close returns; exposure only changes when the new target differs from the held one by more than 0.10 (no-trade band) |
| V3 `tsmom_wf` | walk-forward: on each 1 January choose L from {7, 14, 21, 28, 42, 56, 84, 112, 168} with the highest net Sharpe over the previous 3 calendar years (primary costs), trade single-lookback tsmom(L) for that year |
| V4 `tsmom_multi_ls` | long/short; exposure = (share positive − share negative) over the V1 lookbacks, in [−1, 1]; shorts pay the same per-side cost and **receive no funding** (conservative) |

Sources of the designs: multi-horizon averaging and vol scaling follow
Moskowitz, Ooi & Pedersen (2012) and Hurst, Ooi & Pedersen (2017); the
lookback ladder is a doubling grid, not fitted; 0.50 target vol is a round
number below BTC's typical 60–80 %.

## Evaluation

- Test period and gate exactly as v1: Sharpe > buy-and-hold perp, max DD ≤
  0.7 × buy-and-hold perp, circular-shift p < 0.05 / 4, positive total.
- V3 is only defined from 2018 (needs three prior years from 2015 data);
  scored on the same test period.
- V4's control shifts its signed exposure series the same way.

## Diagnostic (not a candidate, not a gate)

Single-lookback tsmom(L) for L = 7 … 180 (step 7) on reference and test
periods, to show whether v1's L = 28 sits on a plateau or a spike.
