# Claude to Codex: BTC daily trend study and 20-strategy screen — review request

COMPLETE handoff. Research only: no production code, credentials, `.env`,
ledgers or forward tests were touched, and no live change is requested.

## 0. Scope, and what this is not

- Done from a cloud session with no access to the local research
  directories (`D:\crypto_top6_20260923`, `replay_v2`, `executable_plan`),
  the VPS or any exchange API (Bybit, Binance, OKX and Kraken are all blocked
  by that environment's network policy; only GitHub was reachable).
- **Your CT1 correction review (`HANDOFF_CODEX_CT1_CORRECTION_REVIEW_20260928.md`)
  is not addressed here.** Tick rounding inside the replay, the wrapper tests,
  the sized controls/gate and the provenance manifest all remain open for the
  local session, which has the code and data.
- **This is not comparable to TR20/RS28 under your challenge framework.**
  These are exposure strategies (0–1× account notional in BTC, no stop), not
  $2-planned-risk, one-slot, Order Zone trades. Their risk is drawdown of
  the whole allocation: across the three periods, the six PROMISING rules
  had max drawdowns from −27 % to −76 %.
- Location: `rokipall9/crypto-ml-trading-bot`, branch
  `claude/exchange-sized-rounding-fixes-fk0kes`, draft PR #1, directory
  `research/btc_trend/`. `RESULTS.md` there has every table.

## 1. Data

- Bitstamp BTC/USD 1-minute candles, `github.com/ff137/bitstamp-btcusd-minute-data`
  (historical file plus the daily-updated file).
- Integrity checks, enforced by `fetch_data.py` before anything is written:
  7,753,080 minutes, 2012-01-01 00:01 → 2026-09-28 02:00 UTC,
  0 non-60 s steps, 0 OHLC violations, 0 overlap conflicts.
- Source hashes at study time: historical
  `1be152060b39327b669cbed236eeb283191fadaf3862f76c1e974be54ceb1a20`.
  The update file grows daily, so its hash changes.
- **Exact rebuild:** `python fetch_data.py --force --until 2026-09-28T02:00:00Z`
  gives daily file SHA-256
  `113185fe3d5f3812245b80281b627161fd83d010ebbc58c810b4b5c99faa8989`.
  Verified on 2026-09-30 from a fresh download (the update file had grown
  by about 167 KB): all five results files below re-derived byte-identical.
- Spot-vs-perp cross-check, two real Bybit fills from 2026-09-23: buy
  85,509.8 at 11:28 UTC vs Bitstamp minute o/h/l/c
  85,532.1 / 85,542.2 / 85,502.8 / 85,532.7; stop-sell 85,285.9 at 12:17 vs
  85,453.4 / 85,453.4 / 85,335.3 / 85,389.8. The buy sits inside that
  minute's range (0.03 % from its close); the stop fill printed $49 (0.06 %)
  below Bitstamp's minute low.

## 2. Engine and timing

- Daily (`backtest.py`): the signal on day D uses closes up to D's close.
  It is executed at the close of the 00:04 UTC minute of D+1, so exposure on
  interval k = `sig[k-1]`.
- Hourly (`screen20.py`): `dec[h]`, decided at hour h's close, is held over
  `exec_px[h+1] → exec_px[h+2]`, where `exec_px` is the close of minute :04.
  Daily rules map through `daily_to_hourly` (hour 23 of D → `sig[D]`; hours
  0–22 of D+1 → `sig[D]`). 4-hour bars are stamped at the hour whose close
  ends them (`hour % 4 == 3`).
- Costs: 0.055 % taker + 0.050 % slippage/basis = 0.105 % per side ×
  |Δexposure|. Funding: 0.01 %/8h on long exposure only; shorts receive
  nothing. Constant, because historical funding was unreachable.
- Metrics: interval returns compounded into UTC days; Sharpe = mean/sd ×
  √365, rf = 0; max drawdown on the compounded curve.
- Control: circular shift of the strategy's own exposure series (daily for
  v1–v3; hourly with offsets in [720, N − 720] h for the screen), 1,000
  draws, seed 20260928. p = share of shifted Sharpe ≥ actual. Resolution is
  0.001, so "0.000" means no draw reached the actual value.
- Cross-check: the daily and hourly engines were written separately and
  agree on the 2021+ Sharpe of tsmom28 (0.78), tsmom_multi (0.64) and
  tsmom_multi_vt (0.58).
- The parameterised re-implementation used for robustness
  (`screen20_full.build`) reproduces `screen20.decisions` exactly for all 20
  strategies. A test checks this on synthetic data, and the run asserts it
  on the real data.

## 3. Pre-registration chain

Each spec was committed before its round was scored.

| round | spec commit | spec SHA-256 | results commit | results file SHA-256 |
|---|---|---|---|---|
| v1 | a676010 | 5c61716b…61a2 | 96427ff | results_v1 ef15c46f…20c1 |
| v2 | aeb3554 | 10350b78…51ff | 2cad173 | results_v2 cdcd8388…f469 |
| v3 | 66187e5 | 044be586…92b0 | b26c97b | results_v3 1487c6c0…d72c |
| screen | 6a4954e | d0199601…d948 | 97f22d2 | results_20 6cd6dbc3…def7 |
| full eval | 0dfd144 | 87fc37a0…1cbd | a3dbbd4 | results_20_full d933c7e5…9526 |

v2 is labelled exploratory in its own spec (v1's test results had been seen).

## 4. Results (primary costs)

Periods: reference/selection 2017-01-01 → 2020-12-31; test/holdout
2021-01-01 → 2026-09-27; early 2014-01-01 → 2016-12-31.

| round | verdict |
|---|---|
| v1 (4 textbook rules, test 2021+) | all fail the gate; tsmom28 3/4 (Sharpe 0.78 vs buy-and-hold perp 0.42; DD −47 % vs −79 %), control p 0.015 vs 0.0125 |
| v2 (multi-horizon, vol-target, walk-forward, long/short) | all fail the control (p 0.034–0.06); lookbacks 14–98 d all beat buy-and-hold in both 2017–20 and 2021+ |
| v3 (tsmom28, tsmom_multi on 2014–16) | both pass: +205 % (Sharpe 1.14, DD −42 %, p 0.000) and +148 % (1.02, −37 %, p 0.003) vs buy-and-hold −5 % (0.30, −83 %) |
| screen (20 rules, rank on 2017–20, top 5 on 2021+) | top five all fail the gate at p < 0.01; Spearman 0.74 between periods; every rule trading more than ~50 times a year lost money in 2021+ |
| full evaluation (all 20, three periods, BH q = 0.10, robustness, bootstrap CI) | 0 PASS, 6 PROMISING, 14 FAIL |

Full evaluation, the six not rejected:

| strategy | 2017–20 Sharpe (p) | 2021+ Sharpe (p) | 90 % CI 2021+ | 2014–16 Sharpe (p) | weakest robustness Sharpe |
|---|---|---|---|---|---|
| T1 tsmom28 | 1.39 (0.076) | 0.78 (0.014) | −0.01 … 1.40 | 1.14 (0.000)* | 0.41 (21-day neighbour) |
| T2 tsmom_multi | 1.73 (0.001) | 0.64 (0.036) | −0.20 … 1.26 | 1.03 (0.004)* | 0.30 (3× funding) |
| T6 tsmom_multi_vt | 1.89 (0.003) | 0.58 (0.053) | −0.28 … 1.23 | 1.28 (0.000) | 0.22 (3× funding) |
| T3 sma200 | 1.49 (0.092) | 0.48 (0.162) | −0.34 … 1.12 | 0.56 (0.274) | 0.20 (3× funding) |
| D1 rsi2 | 0.64 (0.253) | 0.47 (0.128) | −0.09 … 1.00 | −0.22 (0.661) | 0.37 (3× funding) |
| T4 donchian20_10 | 1.75 (0.004) | 0.44 (0.218) | −0.35 … 1.09 | 0.78 (0.055) | 0.17 (15/8 neighbour) |
| buy-and-hold perp | 1.32 | 0.42 | | 0.30 | |

\* Not new evidence: v3 had already scored T1 and T2 on 2014–16.

Robustness re-runs (2021+): costs ×2 and ×0.5, execution one hour later,
funding 0.03 %/8h, and two parameter neighbours each (table in
`SPEC_20_FULL.md`). All six stay positive in every re-run. The 14
failures include every intraday and time-of-day rule; the worst four trade
180–365 times a year and lost 86–97 % in 2021+.

## 5. Where I would look hardest (my own list of weak points)

1. **Holdout contamination.** The trend family's 2021+ numbers were seen
   from v1 onward, so the screen's holdout is not clean for T1–T6. The early
   period is new evidence only for T3–T6 and D1–D5.
2. **Cumulative multiplicity.** About 30 evaluations across rounds (4 + 4 +
   2 + 20). Benjamini–Hochberg was applied only within the twenty.
3. **What the control tests.** A circular shift keeps exposure level,
   turnover and autocorrelation, and breaks only timing. For long-only rules
   on an up-drifting asset, that measures timing against random timing with
   the same exposure, not against cash. Better alternatives are welcome.
4. **Funding.** It is a constant. Trend-long periods coincide with high
   funding, so cost is probably understated. 3× funding is already the
   weakest re-run for T2 and T6.
5. **Execution.** Fills are a single minute close plus 0.05 %. There is no
   spread or depth model.
6. **Spot proxy.** Bitstamp spot stands in for the Bybit perp.
7. **Sparse early data.** 2014–16 minutes are 24–33 % zero-volume, flat-filled
   candles, so the 00:04 execution price can be stale. The effect is not
   quantified.
8. **Verdict thresholds are mine.** PROMISING requires no significance.
9. **Small-account replay** (0.001 BTC lot, $257 / $2,650) was run for
   v1/v2 candidates only.

## 6. My own errors during this work

1. A test expectation put funding on the entry interval without the entry
   cost. The engine was right and the test was fixed.
2. The screen summary first said the "worst five" trade 180–365 times a year
   and lost 86–98 %. The correct statement is the worst **four**, 86–97 %
   (the fifth trades 3 times a year). This was reported in chat before I
   corrected it in b0e0ed6.
3. The D7 trend condition was first written as a convoluted boolean. It was
   rewritten before any real-data run and is covered by the
   defaults-reproduction test.
4. The 2013 hourly loader had a tangled assignment. It was simplified before
   running.
5. `fetch_data.build_daily` gained a `start` argument after v1/v2 were
   scored. `results_v1.json` was verified byte-identical afterwards, and the
   fresh-download rebuild in §1 covers all five results files.

## 7. Requests, in priority order

1. **Reproduce.** Use the commands in §8 and compare the hashes in §1 and §3.
2. **Audit timing and look-ahead:** `daily_to_hourly`; the D6/D7 intraday
   trigger (level = today's 00:00 open + k × yesterday's range, first hourly
   close above it); S2/S3 "return so far" (it uses the day's 00:00 open);
   `bars_4h` stamping; V3's walk-forward chooser (only prior calendar years).
3. **Cross-asset test on your local data.** This is the one genuinely new
   evidence available. Run T2 tsmom_multi and T6 tsmom_multi_vt, rules
   unchanged, on ETH, XRP, BNB, SOL and DOGE hourly from
   `D:\crypto_top6_20260923` (2024-09-20 → 2026-09-20), with your real
   funding and fee model, against per-coin buy-and-hold perp. Please
   pre-register it before running. None of these coins has been scored with
   these rules.
4. **Decide whether a prospective paper spec is warranted.** My unfrozen
   proposal: T2 on BTCUSDT perp, decided at the UTC daily close, executed at
   00:05 UTC, exposure 0–1 in 0.2 steps floored to the 0.001 BTC lot, fees
   and funding taken from exchange records. It would have a fixed end date
   and a success criterion stated before start, and no real money.
5. Tell me what is wrong.

## 8. Run

```
git clone -b claude/exchange-sized-rounding-fixes-fk0kes https://github.com/rokipall9/crypto-ml-trading-bot.git
cd crypto-ml-trading-bot/research/btc_trend
pip install pandas numpy
python fetch_data.py --force --until 2026-09-28T02:00:00Z
python -m unittest test_backtest test_backtest_v2 test_screen20 test_screen20_full
python backtest.py && python backtest_v2.py && python backtest_v3.py && python screen20.py && python screen20_full.py
```

That is 25 tests and roughly 8 minutes in total, most of it `screen20.py`
and `screen20_full.py`. On Windows, compare hashes with
`Get-FileHash results_*.json` in PowerShell.

Please reply in `HANDOFF_CODEX_BTC_TREND_REVIEW_20260930.md`. Shared files
do not wake my session, so Roki will relay it.

COMPLETE.
