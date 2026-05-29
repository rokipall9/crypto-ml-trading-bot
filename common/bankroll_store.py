"""bankroll_store.py — per-user bankroll preferences (private DM-set)."""
import json
import os
from typing import Optional

STORE_PATH = "/home/ubuntu/common/bankroll_store.json"


def _load():
    if not os.path.exists(STORE_PATH):
        return {}
    try:
        with open(STORE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data):
    with open(STORE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def set_bankroll(user_id: int, amount: float, risk_pct: float = 1.0):
    data = _load()
    data[str(user_id)] = {"bankroll": float(amount), "risk_pct": float(risk_pct)}
    _save(data)


def get_bankroll(user_id: int) -> Optional[dict]:
    data = _load()
    return data.get(str(user_id))


def all_bankrolls():
    return _load()


def position_size(entry: float, sl: float, bankroll: float, risk_pct: float = 1.0) -> dict:
    """Compute risk amount + position size + position value."""
    risk_amount = bankroll * (risk_pct / 100.0)
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return {"risk_amount": risk_amount, "units": 0, "position_value": 0}
    units = risk_amount / risk_per_unit
    position_value = units * entry
    return {
        "risk_amount": risk_amount,
        "units": units,
        "position_value": position_value,
    }
