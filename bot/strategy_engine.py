"""
strategy_engine.py — STUB (May 4 2026)

The original 4H strategies (BREAKOUT_4H, VOL_MOMENTUM_4H, PANIC_DIP_4H,
EMA_PULLBACK_4H) were archived to operator PC under
srs/strategy_archive/strategy_engine.py and disabled here.

This stub keeps `srs_main.py`'s `from strategy_engine import StrategyEngine`
import working (so the bot still starts) but returns an empty signal list,
so no 4H strategies fire.

Original file preserved on VPS as: strategy_engine.py.archived_20260504
"""
from __future__ import annotations


class StrategyEngine:
    """No-op stub. Preserves the API but returns no signals."""

    def __init__(self):
        self.last_reasons = ["4H strategies disabled — see strategy_archive/"]
        self._regime = "DISABLED"
        self._atr = 0.0

    def update_data(self, df_4h, df_1d):
        return None

    def check_signals(self):
        return []

    def get_regime(self):
        return self._regime

    def get_current_atr(self):
        return self._atr

    def get_trend_status(self):
        return "4H strategy engine disabled"

    def record_trade_result(self, strategy, won):
        return None

    def status(self):
        return {"engine": "DISABLED",
                "note": "4H strategies archived May 4 2026"}
