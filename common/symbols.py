"""
symbols.py — Multi-symbol configuration scaffolding.

The bot currently runs BTCUSDT only (the only walk-forward-validated
strategy set as of Apr 2026 audit). When ETH / SOL / etc. are validated,
adopt by:

  1. Add the symbol to ENABLED_SYMBOLS below
  2. Update strategy code to call ledger_for(symbol) instead of hard-coded path
  3. Update dashboard to expose a symbol filter

This module exists so adoption is a 3-line change, not a refactor.
"""
from __future__ import annotations

import os
from typing import List

# ─── Configuration ───
# Add validated symbols here. Strategy code should iterate via enabled_symbols().
ENABLED_SYMBOLS: List[str] = ["BTCUSDT"]

# Per-symbol strategy whitelist (empty = all 4 default strategies)
# Used by scan loop to decide which strategies to run for which symbol
SYMBOL_STRATEGIES = {
    "BTCUSDT": [],   # all 4 strategies
    # "ETHUSDT": ["BREAKOUT_4H", "EMA_PULLBACK_4H"],   # example: only validated
}

# Per-symbol risk overrides (e.g. ETH gets smaller position size)
SYMBOL_RISK = {
    # "ETHUSDT": {"max_risk_per_trade_pct": 0.5},
}

LEDGER_DIR = "/home/ubuntu/common"


def enabled_symbols() -> List[str]:
    """Return active symbols. Override via SRS_SYMBOLS env (comma-separated)."""
    env_override = os.environ.get("SRS_SYMBOLS", "").strip()
    if env_override:
        return [s.strip().upper() for s in env_override.split(",") if s.strip()]
    return list(ENABLED_SYMBOLS)


def ledger_for(symbol: str) -> str:
    """Return the ledger path for a symbol.

    Single-symbol mode (BTCUSDT only): returns the legacy path
    /home/ubuntu/common/forward_results.jsonl for backward compatibility.
    Multi-symbol mode: returns /home/ubuntu/common/forward_results_<SYMBOL>.jsonl.
    """
    if len(ENABLED_SYMBOLS) <= 1 and symbol == "BTCUSDT":
        return os.path.join(LEDGER_DIR, "forward_results.jsonl")
    return os.path.join(LEDGER_DIR,
                        f"forward_results_{symbol.upper()}.jsonl")


def strategies_for(symbol: str) -> List[str]:
    """Return strategy whitelist for a symbol. Empty list = all strategies."""
    return list(SYMBOL_STRATEGIES.get(symbol.upper(), []))


def risk_overrides_for(symbol: str) -> dict:
    """Return per-symbol risk config overrides."""
    return dict(SYMBOL_RISK.get(symbol.upper(), {}))


def is_enabled(symbol: str) -> bool:
    return symbol.upper() in [s.upper() for s in enabled_symbols()]


if __name__ == "__main__":
    import json
    info = {
        "enabled": enabled_symbols(),
        "ledgers": {s: ledger_for(s) for s in enabled_symbols()},
        "strategies": {s: strategies_for(s) for s in enabled_symbols()},
        "risk_overrides": {s: risk_overrides_for(s)
                           for s in enabled_symbols()},
    }
    print(json.dumps(info, indent=2, default=str))
