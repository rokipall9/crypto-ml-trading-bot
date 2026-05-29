"""
config.py — 4H Multi-Strategy System configuration.

4 validated long strategies (BTC, BULL regime only):
  S1: 4H Breakout        — PF 2.27, 45 trades solo
  S2: 4H Volume Momentum — PF 2.03, 41 trades solo
  S3: 4H Panic Dip       — PF 2.45, 36 trades solo
  S4: 4H EMA Pullback    — PF 1.99, 52 trades solo

Combined 4-strategy BTC backtest: 174 trades, +$27,464, PF 2.01, MaxDD 19%

Dropped (Apr 17 audit):
  S5 VOL_RESET          — only 20 trades in 833d, under-sampled
  S6 REJECTION_SHORT    — PF 1.19 on BTC / 0.86 on ETH, fragile drag

Multi-symbol test (Apr 17): BTC + ETH pass, 7 altcoins (SOL/BNB/AVAX/LINK/
ATOM/ARB/SUI) FAIL. Deployment = BTC-only.

Paper trading mode until validated live.
"""
# ── Symbol ────────────────────────────────────────────────────────────
SYMBOL = "BTCUSDT"

# ── Paper trading ─────────────────────────────────────────────────────
STARTING_BALANCE = 10000.0
PAPER_MODE = True

# ── Strategies ────────────────────────────────────────────────────────
STRATEGIES = [
    # Active scanner is common/scan_mtf.py which fires:
    #   • SMC_CONFLUENCE        (long, multi-TF confluence)
    #   • SMC_CONFLUENCE_SHORT  (short, mirror)
    # The legacy 4H strategies (BREAKOUT_4H, VOL_MOMENTUM_4H, PANIC_DIP_4H,
    # EMA_PULLBACK_4H) were archived May 4 2026. Names below drive the
    # "Bot Online" status embed — must reflect what's actually scanning.
    "SMC_CONFLUENCE",
    "SMC_CONFLUENCE_SHORT",
]
TIMEFRAME = "4h"

# ── Long strategy params (BULL regime) ────────────────────────────────
# Breakout (720-config sweep, 93% neighbors profitable)
BREAKOUT_HIGH_LOOKBACK = 20  # was 30, lowered to surface alerts faster (paper mode)
BREAKOUT_VOL_MULT = 1.3  # was 1.5
BREAKOUT_TRAIL_ATR = 3.0
BREAKOUT_SL_ATR = 1.5
BREAKOUT_TIME_STOP = 192     # 48 bars * 4h

# Volume Momentum
MOMENTUM_BODY_MULT = 2.0
MOMENTUM_VOL_MULT = 2.0
MOMENTUM_TRAIL_ATR = 3.0
MOMENTUM_SL_ATR = 1.5
MOMENTUM_TIME_STOP = 192

# Panic Dip Recovery (3240-config sweep, 100% neighbors profitable)
DIP_HIGH_LOOKBACK = 8
DIP_PCT = 3.0
DIP_BODY_MULT = 1.5
DIP_TRAIL_ATR = 3.5
DIP_SL_ATR = 1.5
DIP_TIME_STOP = 192

# EMA Pullback (1152-config sweep, 100% neighbors profitable)
PULLBACK_EMA_PERIOD = 40
PULLBACK_MAX_BELOW = 0.3
PULLBACK_MAX_ABOVE = 1.2
PULLBACK_TRAIL_ATR = 3.5
PULLBACK_SL_ATR = 1.5
PULLBACK_TIME_STOP = 192

# Volume Reset (15552-config sweep, 94% neighbors profitable)
VRESET_DRY_RATIO = 0.8
VRESET_EXPAND_MULT = 1.2
VRESET_EMA_DIST = 0.8
VRESET_DRY_LOOKBACK = 3
VRESET_DRY_MIN = 1
VRESET_TRAIL_ATR = 4.0
VRESET_SL_ATR = 1.5
VRESET_TIME_STOP = 240       # 60 bars * 4h

# ── Short strategy params (BEAR regime) ──────────────────────────────
# EMA Rejection Short (best from 108-config sweep)
REJECTION_EMA_PERIOD = 30     # EMA30 as resistance
REJECTION_MAX_DIST = 0.5      # high within 0.5 ATR of EMA30
REJECTION_TRAIL_ATR = 4.0     # trailing stop at 4.0x ATR
REJECTION_SL_ATR = 1.5        # SL = max(high, EMA) + 0.3*ATR
REJECTION_TIME_STOP = 192     # 48 bars * 4h

# ── Regime-based risk ────────────────────────────────────────────────
RISK_PCT = 0.01               # 1% per trade (BULL longs)
BEAR_RISK_MULT = 0.7          # 0.7% per trade (BEAR shorts)

# ── Risk management ──────────────────────────────────────────────────
MAX_OPEN_TRADES = 5
CIRCUIT_BREAKER_LOSSES = 3
CIRCUIT_BREAKER_HOURS = 48

# ── Fear & Greed gate ────────────────────────────────────────────────
FG_MIN = 15
FG_MAX = 85

# ── Data requirements ─────────────────────────────────────────────────
CANDLES_4H = 500
CANDLES_1D = 200

# ── market_data.py compatibility ─────────────────────────────────────
INTERVAL = "4h"
TOTAL_CANDLES = 500
DATA_DIR = "data"
TRADE_SYMBOLS = [SYMBOL, "ETHUSDT"]  # ETH validated Apr 17 audit

# ── Validation thresholds ─────────────────────────────────────────────
MIN_TRADES_FOR_PROMOTION = 100
MIN_WINRATE_FOR_PROMOTION = 0.33
MIN_EV_FOR_PROMOTION = 5.0
