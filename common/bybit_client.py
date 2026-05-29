"""
bybit_client.py — Minimal HMAC-signed wrapper around Bybit V5 REST API.

Handles only what the live trader needs:
  • place market order with stop-loss + take-profit (one-way mode)
  • get current position for symbol
  • get wallet balance
  • close position (market exit)

NOT a general-purpose Bybit SDK — purpose-built for the SMC bot.

Credentials read from env:
  BYBIT_API_KEY      — generated on Bybit's API page (no withdrawal scope!)
  BYBIT_API_SECRET   — paired secret
  BYBIT_TESTNET      — "true" → api-testnet.bybit.com, else mainnet
"""
from __future__ import annotations

import os
import time
import hmac
import hashlib
import json
import urllib.request
import urllib.error
from typing import Optional, Dict, Any


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip()


class BybitError(Exception):
    """Raised when Bybit API returns a non-success response."""
    pass


class BybitClient:
    def __init__(self, api_key: Optional[str] = None,
                 api_secret: Optional[str] = None,
                 testnet: Optional[bool] = None,
                 recv_window: int = 5000):
        self.api_key = api_key or _env("BYBIT_API_KEY")
        self.api_secret = api_secret or _env("BYBIT_API_SECRET")
        if testnet is None:
            testnet = _env("BYBIT_TESTNET", "false").lower() in ("1", "true", "yes")
        self.testnet = testnet
        self.recv_window = recv_window
        self.base_url = ("https://api-testnet.bybit.com" if testnet
                         else "https://api.bybit.com")
        if not self.api_key or not self.api_secret:
            raise BybitError("BYBIT_API_KEY / BYBIT_API_SECRET not set")

    # ─── HMAC signing ────────────────────────────────────────────────

    def _sign(self, ts: str, payload: str) -> str:
        msg = f"{ts}{self.api_key}{self.recv_window}{payload}"
        return hmac.new(self.api_secret.encode(),
                        msg.encode(),
                        hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str,
                 params: Optional[Dict] = None,
                 body: Optional[Dict] = None) -> Dict:
        ts = str(int(time.time() * 1000))
        if method == "GET":
            qs = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
            payload = qs
            url = f"{self.base_url}{path}?{qs}" if qs else f"{self.base_url}{path}"
            data = None
        else:
            payload = json.dumps(body or {}, separators=(",", ":"))
            url = f"{self.base_url}{path}"
            data = payload.encode()

        sign = self._sign(ts, payload)
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": str(self.recv_window),
            "X-BAPI-SIGN": sign,
            "Content-Type": "application/json",
            "User-Agent": "srs-live/1.0",
        }
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        # Retry loop: 2 attempts with 2s backoff. Catches URLError (network)
        # so it surfaces as BybitError (caller's expected exception type)
        # rather than escaping uncaught. Fixes orphan-position class of bug.
        last_err = None
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                try:
                    err_body = json.loads(e.read())
                except Exception:
                    err_body = {"message": str(e)}
                raise BybitError(
                    f"HTTP {e.code}: {err_body.get('retMsg', err_body)}")
            except urllib.error.URLError as e:
                # Network-level failure (timeout, DNS, refused). Retry once.
                last_err = e
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise BybitError(f"network error (after retry): {e}")
            except Exception as e:
                # Catch-all so JSON parse / other unexpected errors don't escape
                # as the wrong exception type — caller catches BybitError only.
                raise BybitError(f"unexpected error: {type(e).__name__}: {e}")

        if resp.get("retCode") != 0:
            raise BybitError(f"retCode={resp.get('retCode')} "
                             f"msg={resp.get('retMsg')}")
        return resp.get("result", {})

    # ─── Public API used by live_trader ──────────────────────────────

    def get_balance(self, coin: str = "USDT") -> float:
        """Available balance in USDT (or whichever coin)."""
        r = self._request("GET", "/v5/account/wallet-balance",
                          params={"accountType": "UNIFIED"})
        for acct in r.get("list", []):
            for c in acct.get("coin", []):
                if c.get("coin") == coin:
                    return float(c.get("availableToWithdraw") or
                                 c.get("walletBalance") or 0)
        return 0.0

    def get_position(self, symbol: str = "BTCUSDT") -> Optional[Dict]:
        """Current position for symbol. Returns None if no open position."""
        r = self._request("GET", "/v5/position/list",
                          params={"category": "linear", "symbol": symbol})
        for p in r.get("list", []):
            if float(p.get("size", 0)) > 0:
                return {
                    "side": p["side"],          # "Buy" or "Sell"
                    "size": float(p["size"]),
                    "entry": float(p["avgPrice"]),
                    "stop_loss": float(p.get("stopLoss") or 0),
                    "take_profit": float(p.get("takeProfit") or 0),
                    "unrealized_pnl": float(p.get("unrealisedPnl") or 0),
                    "position_idx": int(p.get("positionIdx", 0)),
                    "raw": p,
                }
        return None

    def place_market_order(self, symbol: str, side: str, qty: float,
                           stop_loss: Optional[float] = None,
                           take_profit: Optional[float] = None,
                           reduce_only: bool = False) -> Dict:
        """Place a market order. side='Buy'/'Sell'. qty in BTC.
        If stop_loss / take_profit given, set them on the resulting
        position via a separate trading-stop call after the order fills.
        Bybit testnet rejects SL/TP fields in the order body for some
        price ranges (retCode 30208), so we use the 2-step pattern.
        Returns Bybit's order result (orderId, etc).
        """
        import time as _time
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "timeInForce": "IOC",
            "positionIdx": 0,    # one-way mode
        }
        if reduce_only:
            body["reduceOnly"] = True
        order_result = self._request("POST", "/v5/order/create", body=body)

        # Now set SL/TP on the position (only on opening trades, not closes)
        if (stop_loss or take_profit) and not reduce_only:
            _time.sleep(1.5)  # let the order fill before adjusting position
            # Retry SL/TP up to 3 times — network blips shouldn't leave naked
            sltp_err = None
            for attempt in range(3):
                try:
                    self.set_position_sl_tp(symbol,
                                             stop_loss=stop_loss,
                                             take_profit=take_profit)
                    sltp_err = None
                    break
                except BybitError as e:
                    sltp_err = e
                    _time.sleep(1.5)
            if sltp_err is not None:
                # SL/TP setting permanently failed — position is naked.
                # AUTO FORCE-CLOSE: immediately market-close to limit damage.
                try:
                    self.close_position(symbol)
                    raise BybitError(
                        f"order placed but SL/TP failed ({sltp_err}); "
                        f"position auto-flattened for safety")
                except BybitError as close_err:
                    # Both SL/TP and close failed — really bad, alert hard
                    raise BybitError(
                        f"order placed, SL/TP failed ({sltp_err}), AND "
                        f"auto-close ALSO failed ({close_err}) — "
                        f"NAKED POSITION ON BYBIT, MANUAL ACTION REQUIRED")
        return order_result

    def close_position(self, symbol: str = "BTCUSDT") -> Optional[Dict]:
        """Market-close an open position."""
        pos = self.get_position(symbol)
        if pos is None:
            return None
        # Opposite side, reduceOnly to close, exact size
        opp = "Sell" if pos["side"] == "Buy" else "Buy"
        return self.place_market_order(symbol, opp, pos["size"],
                                       reduce_only=True)

    def set_position_sl_tp(self, symbol: str,
                           stop_loss: Optional[float] = None,
                           take_profit: Optional[float] = None) -> Dict:
        """Adjust SL/TP on an existing position (e.g. trailing)."""
        body = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": 0,
        }
        if stop_loss:
            body["stopLoss"] = str(round(stop_loss, 2))
        if take_profit:
            body["takeProfit"] = str(round(take_profit, 2))
        return self._request("POST", "/v5/position/trading-stop", body=body)

    def get_ticker(self, symbol: str = "BTCUSDT") -> float:
        """Current last price (no auth needed; uses signed path for consistency)."""
        # Public endpoint actually — no auth needed, but use path either way
        url = f"{self.base_url}/v5/market/tickers?category=linear&symbol={symbol}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                resp = json.loads(r.read())
            return float(resp["result"]["list"][0]["lastPrice"])
        except Exception as e:
            raise BybitError(f"get_ticker failed: {e}")

    def get_recent_executions(self, symbol: str = "BTCUSDT",
                              limit: int = 50) -> list:
        """Recent executions (fills) for symbol.
        Each entry: {execTime (ms str), side, execQty, execPrice, orderId,
                     closedSize, execFee, feeCurrency, ...}
        Used by reconcile to get the ACTUAL fill price when Bybit closes
        a position via SL/TP — not the ticker at reconcile-time, which can
        drift seconds-to-minutes after the real fill.
        """
        r = self._request("GET", "/v5/execution/list",
                          params={"category": "linear",
                                  "symbol": symbol,
                                  "limit": limit})
        return r.get("list", [])

    def get_closed_pnl(self, symbol: str = "BTCUSDT",
                       limit: int = 50) -> list:
        """Closed-PnL records from Bybit. Each entry has closedPnl (USDT)
        for a position-close event, plus avgEntryPrice, avgExitPrice,
        execType, closedSize. This is the AUTHORITATIVE realized P&L —
        Bybit calculates it server-side from actual fills.
        """
        r = self._request("GET", "/v5/position/closed-pnl",
                          params={"category": "linear",
                                  "symbol": symbol,
                                  "limit": limit})
        return r.get("list", [])
