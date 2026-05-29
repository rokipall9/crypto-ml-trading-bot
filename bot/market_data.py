from __future__ import annotations
"""
market_data.py — Download and cache OHLCV candle data from Binance.

Uses the public Binance REST API (no API key required for klines).
Downloaded data is stored as a CSV so subsequent runs skip the network
round-trip.  Pass force=True to re-download even when a cache exists.
"""

import os
import time
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

# ── Resilient HTTP session: retry 3x with backoff ────────────────────
_session = requests.Session()
_retry   = Retry(
    total=3,
    backoff_factor=0.5,          # 0.5s, 1s, 2s between retries
    status_forcelist=[429, 500, 502, 503, 504],
    method_whitelist=frozenset({"GET"}),
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))

# ── Fallback price + OHLCV sources (no API key needed) ──────────────
# Priority: Binance → CryptoCompare → Kraken → CoinGecko → Coinpaprika
_COINGECKO_URL    = "https://api.coingecko.com/api/v3/simple/price"
_CRYPTOCOMPARE_URL = "https://min-api.cryptocompare.com/data"
_KRAKEN_URL        = "https://api.kraken.com/0/public"
_COINPAPRIKA_URL   = "https://api.coinpaprika.com/v1/tickers"
# Symbol → CoinGecko id mapping for the symbols we trade
_CG_IDS = {
    "BTCUSDT":  "bitcoin",
    "ETHUSDT":  "ethereum",
    "SOLUSDT":  "solana",
    "BNBUSDT":  "binancecoin",
    "XRPUSDT":  "ripple",
    "ADAUSDT":  "cardano",
    "DOGEUSDT": "dogecoin",
    "MATICUSDT":"matic-network",
    "DOTUSDT":  "polkadot",
    "LTCUSDT":  "litecoin",
    "AVAXUSDT": "avalanche-2",
    "LINKUSDT": "chainlink",
    "ATOMUSDT": "cosmos",
    "UNIUSDT":  "uniswap",
    "XLMUSDT":  "stellar",
    "ALGOUSDT": "algorand",
    "ARBUSDT":  "arbitrum",
    "INJUSDT":  "injective-protocol",
    "APTUSDT":  "aptos",
    "SUIUSDT":  "sui",
    "GALAUSDT": "gala",
    "ICPUSDT":  "internet-computer",
}

# CryptoCompare uses base/quote — just strip USDT → fsym=BASE, tsym=USDT
def _cc_sym(symbol: str):
    """BTCUSDT → (BTC, USDT)"""
    for quote in ("USDT", "BTC", "ETH", "BNB", "BUSD"):
        if symbol.endswith(quote):
            return symbol[:-len(quote)], quote
    return symbol[:-4], symbol[-4:]

# Kraken uses different ticker names for some assets
_KRAKEN_SYM = {
    "BTCUSDT": "XBTUSD",  "ETHUSDT": "ETHUSD",  "LTCUSDT": "LTCUSD",
    "XRPUSDT": "XRPUSD",  "XMRUSDT": "XMRUSD",  "ADAUSDT": "ADAUSD",
    "DOTUSDT": "DOTUSD",  "SOLUSDT": "SOLUSD",   "ATOMUSDT": "ATOMUSD",
    "LINKUSDT":"LINKUSD", "UNIUSDT": "UNIUSD",   "ALGOUSDT": "ALGOUSD",
    "INJUSDT": "INJUSD",  "APTUSDT": "APTUSD",   "ARBUSDT": "ARBUSD",
    "GALAUSDT":"GALAUSD", "SUIUSDT": "SOLUSD",   "ICPUSDT": "ICPUSD",
}
# Kraken OHLC interval map (minutes → Kraken interval int)
_KRAKEN_INTERVAL = {1:1, 3:1, 5:5, 15:15, 30:30, 60:60, 240:240, 1440:1440}
# CryptoCompare OHLCV endpoint map
_CC_ENDPOINT = {1:"histominute",5:"histominute",15:"histominute",
                30:"histominute",60:"histohour",240:"histohour",1440:"histoday"}
_CC_AGGREGATE = {1:1,5:5,15:15,30:30,60:1,240:4,1440:1}


# ── Binance public endpoints ────────────────────────────────────────
KLINES_URL  = "https://api.binance.com/api/v3/klines"
TICKER_URL  = "https://api.binance.com/api/v3/ticker/price"


def _price_from_cryptocompare(symbol: str) -> float:
    """CryptoCompare free tier — ~160ms, no key needed."""
    base, quote = _cc_sym(symbol)
    resp = requests.get(
        f"{_CRYPTOCOMPARE_URL}/price",
        params={"fsym": base, "tsyms": quote},
        timeout=8,
    )
    resp.raise_for_status()
    return float(resp.json()[quote])


def _price_from_kraken(symbol: str) -> float:
    """Kraken public ticker — ~180ms, no key needed."""
    pair = _KRAKEN_SYM.get(symbol.upper())
    if not pair:
        base, _ = _cc_sym(symbol)
        pair = f"{base}USD"
    resp = requests.get(
        f"{_KRAKEN_URL}/Ticker",
        params={"pair": pair},
        timeout=8,
    )
    resp.raise_for_status()
    result = resp.json()["result"]
    return float(list(result.values())[0]["c"][0])


def _price_from_coinpaprika(symbol: str) -> float:
    """Coinpaprika — ~550ms fallback, no key needed."""
    cg_id = _CG_IDS.get(symbol.upper())
    if not cg_id:
        raise ValueError(f"No Coinpaprika id for {symbol}")
    # Use same id mapping as CoinGecko (ids overlap for major coins)
    base, _ = _cc_sym(symbol)
    coin_id = f"{base.lower()}-{base.lower()}usdt"
    resp = requests.get(
        f"{_COINPAPRIKA_URL}/{base.lower()}-{base.lower()}",
        timeout=10,
    )
    if resp.status_code != 200:
        raise ValueError(f"Coinpaprika returned {resp.status_code}")
    return float(resp.json()["quotes"]["USD"]["price"])


def _price_from_coingecko(symbol: str) -> float:
    """CoinGecko fallback — free, no key, ~300ms latency."""
    cg_id = _CG_IDS.get(symbol.upper())
    if not cg_id:
        raise ValueError(f"No CoinGecko id for {symbol}")
    resp = requests.get(
        _COINGECKO_URL,
        params={"ids": cg_id, "vs_currencies": "usd"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return float(data[cg_id]["usd"])


def get_price(symbol: str) -> float:
    """
    Fetch current price for a symbol.
    Primary: Binance /ticker/price (10s timeout, 3 retries via session).
    Fallback: CoinGecko free API if Binance times out or returns error.
    """
    # ── 1. Primary: Binance ──────────────────────────────────────────
    try:
        resp = _session.get(TICKER_URL, params={"symbol": symbol}, timeout=10)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as _e1:
        print(f"[data] Binance price timeout for {symbol} ({_e1.__class__.__name__}) — trying CryptoCompare")
    # ── 2. CryptoCompare (163ms, free, no key) ───────────────────────
    try:
        price = _price_from_cryptocompare(symbol)
        print(f"[data] CryptoCompare price OK for {symbol}: {price}")
        return price
    except Exception as _e2:
        print(f"[data] CryptoCompare failed ({_e2.__class__.__name__}) — trying Kraken")
    # ── 3. Kraken (177ms, free, no key) ─────────────────────────────
    try:
        price = _price_from_kraken(symbol)
        print(f"[data] Kraken price OK for {symbol}: {price}")
        return price
    except Exception as _e3:
        print(f"[data] Kraken failed ({_e3.__class__.__name__}) — trying CoinGecko")
    # ── 4. CoinGecko (300ms, free, no key) ──────────────────────────
    try:
        price = _price_from_coingecko(symbol)
        print(f"[data] CoinGecko price OK for {symbol}: {price}")
        return price
    except Exception as _e4:
        print(f"[data] CoinGecko failed ({_e4.__class__.__name__}) — trying Coinpaprika")
    # ── 5. Coinpaprika (550ms, free, no key) ────────────────────────
    try:
        price = _price_from_coinpaprika(symbol)
        print(f"[data] Coinpaprika price OK for {symbol}: {price}")
        return price
    except Exception as _e5:
        raise RuntimeError(
            f"ALL 5 price sources failed for {symbol}: "
            f"Binance={_e1}  CC={_e2}  Kraken={_e3}  CoinGecko={_e4}  Coinpaprika={_e5}"
        )


def get_live_price(symbol: str) -> float:
    """Alias for get_price — fetch live market price for a symbol."""
    return get_price(symbol)

# Milliseconds per candle for each supported interval
_INTERVAL_MS = {
    "1m":  60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1d":  86_400_000,
}


def _fetch_klines(symbol: str, interval: str, start_ms: int,
                  limit: int = 1000) -> list[list]:
    """Fetch a single batch of klines from Binance.  Raises on HTTP error."""
    params = {
        "symbol":    symbol,
        "interval":  interval,
        "startTime": start_ms,
        "limit":     limit,
    }
    resp = _session.get(KLINES_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _klines_from_cryptocompare(symbol: str, interval: str, total_candles: int) -> list:
    """
    CryptoCompare OHLCV fallback.  Returns list of Binance-format kline rows
    [open_time_ms, open, high, low, close, volume, close_time_ms, ...].
    """
    base, quote = _cc_sym(symbol)
    ivl_min   = _INTERVAL_MS.get(interval, 900_000) // 60_000
    endpoint  = _CC_ENDPOINT.get(ivl_min, "histominute")
    aggregate = _CC_AGGREGATE.get(ivl_min, 1)
    limit     = min(total_candles, 2000)  # CC max per request
    resp = requests.get(
        f"{_CRYPTOCOMPARE_URL}/v2/{endpoint}",
        params={"fsym": base, "tsym": quote, "limit": limit, "aggregate": aggregate},
        timeout=15,
    )
    resp.raise_for_status()
    candles = resp.json()["Data"]["Data"]
    # Convert to Binance kline format
    rows = []
    step_ms = ivl_min * aggregate * 60_000
    for c in candles:
        ts = int(c["time"]) * 1000
        rows.append([
            ts,                          # open_time ms
            str(c["open"]),              # open
            str(c["high"]),              # high
            str(c["low"]),               # low
            str(c["close"]),             # close
            str(c["volumefrom"]),        # volume
            ts + step_ms - 1,            # close_time ms
            str(c["volumeto"]),          # quote_volume
            0, "0", "0", "0",           # trades, taker_base/quote, ignore
        ])
    return rows


def _klines_from_kraken(symbol: str, interval: str, total_candles: int) -> list:
    """
    Kraken OHLCV fallback.  Returns Binance-format kline rows.
    Kraken returns max 720 candles per request.
    """
    pair    = _KRAKEN_SYM.get(symbol.upper())
    if not pair:
        base, _ = _cc_sym(symbol)
        pair = f"{base}USD"
    ivl_min = _INTERVAL_MS.get(interval, 900_000) // 60_000
    k_ivl   = _KRAKEN_INTERVAL.get(ivl_min, 15)
    since   = int(time.time()) - total_candles * ivl_min * 60
    resp = requests.get(
        f"{_KRAKEN_URL}/OHLC",
        params={"pair": pair, "interval": k_ivl, "since": since},
        timeout=15,
    )
    resp.raise_for_status()
    result  = resp.json()["result"]
    candles = [v for k, v in result.items() if k != "last"][0]
    step_ms = k_ivl * 60_000
    rows = []
    for c in candles:
        ts = int(c[0]) * 1000
        rows.append([
            ts,
            str(c[1]),   # open
            str(c[2]),   # high
            str(c[3]),   # low
            str(c[4]),   # close
            str(c[6]),   # volume
            ts + step_ms - 1,
            str(float(c[6]) * float(c[4])),  # quote volume estimate
            int(c[7]), "0", "0", "0",
        ])
    return rows


def download(symbol: str = config.SYMBOL,
             interval: str = config.INTERVAL,
             total_candles: int | None = None) -> pd.DataFrame:
    """
    Paginated download of historical klines.

    Design
    ------
    We compute the start timestamp as now − (total_candles × candle_ms × 1.10).
    Each batch requests up to 1000 candles from Binance starting at start_ms,
    then advances start_ms to close_time_of_last_candle + 1 ms.
    3 retries per batch handle transient network errors.

    Parameters
    ----------
    symbol        : e.g. 'BTCUSDT' or 'ETHUSDT'
    interval      : e.g. '15m'
    total_candles : target number of candles (default 30 000 ≈ 312 days of 15m)

    Returns
    -------
    pd.DataFrame sorted by open_time, duplicates removed.
    """
    if total_candles is None:
        total_candles = config.TOTAL_CANDLES
    candle_ms   = _INTERVAL_MS.get(interval, 900_000)
    lookback_ms = int(total_candles * candle_ms * 1.10)   # 10 % buffer
    start_ms    = int(time.time() * 1_000) - lookback_ms
    now_ms      = int(time.time() * 1_000)

    all_rows: list[list] = []
    remaining = int(total_candles * 1.15)  # 15% extra ensures we reach current time
    batch_num = 0

    start_dt = pd.Timestamp(start_ms, unit="ms", tz="UTC")
    print(f"[data] Downloading {total_candles} candles for {symbol} {interval} …")
    print(f"[data] Starting from: {start_dt.strftime('%Y-%m-%d %H:%M UTC')}")

    while remaining > 0:
        batch_size = min(remaining, 1000)
        batch_num += 1

        # Retry up to 3 times on transient errors
        rows: list = []
        for attempt in range(3):
            try:
                rows = _fetch_klines(symbol, interval, start_ms, batch_size)
                break
            except Exception as exc:
                if attempt == 2:
                    print(f"[data] Batch {batch_num} failed after 3 attempts: {exc}")
                else:
                    time.sleep(1.0)

        if not rows:
            # Binance klines failed — try CryptoCompare then Kraken for this batch
            print(f"[data] Binance klines failed for {symbol} {interval} — trying CryptoCompare")
            try:
                rows_cc = _klines_from_cryptocompare(symbol, interval, remaining)
                if rows_cc:
                    print(f"[data] CryptoCompare OHLCV OK for {symbol} {interval}: {len(rows_cc)} candles")
                    all_rows.extend(rows_cc)
                    break  # got what we need from fallback
            except Exception as _cc_e:
                print(f"[data] CryptoCompare OHLCV failed ({_cc_e.__class__.__name__}) — trying Kraken")
            try:
                rows_kr = _klines_from_kraken(symbol, interval, remaining)
                if rows_kr:
                    print(f"[data] Kraken OHLCV OK for {symbol} {interval}: {len(rows_kr)} candles")
                    all_rows.extend(rows_kr)
                    break
            except Exception as _kr_e:
                print(f"[data] Kraken OHLCV failed ({_kr_e.__class__.__name__}) — no more fallbacks")
            print(f"[data] ALL OHLCV sources failed for {symbol} {interval}.")
            break

        all_rows.extend(rows)

        # Advance start_ms past the close_time of the last returned candle
        last_close_ms = int(rows[-1][6])
        next_start_ms = last_close_ms + 1

        # Safety: stop if we've caught up to the current time
        if next_start_ms >= now_ms:
            print(f"[data] Reached current time after batch {batch_num}.")
            break

        # Safety: stop if start_ms is not advancing (malformed response)
        if next_start_ms <= start_ms:
            print(f"[data] start_ms not advancing at batch {batch_num} — stopping.")
            break

        start_ms   = next_start_ms
        remaining -= len(rows)

        # Progress report every 10 batches
        if batch_num % 10 == 0:
            print(f"  … {len(all_rows):,} candles fetched (batch {batch_num})")

        time.sleep(0.25)

    df = _rows_to_dataframe(all_rows)
    # Trim to exactly total_candles most-recent rows (extra rows from the 15% buffer)
    if len(df) > total_candles:
        df = df.iloc[-total_candles:].reset_index(drop=True)
    print(f"[data] Done — {len(df):,} unique candles downloaded.")
    return df


def _rows_to_dataframe(rows: list[list]) -> pd.DataFrame:
    """Convert raw Binance kline rows into a clean DataFrame."""
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(rows, columns=cols)

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)

    df = df[["open_time", "open", "high", "low", "close", "volume"]]
    df = (df.drop_duplicates(subset="open_time")
            .sort_values("open_time")
            .reset_index(drop=True))
    return df


# ── Caching ──────────────────────────────────────────────────────────

def _raw_path(symbol: str = config.SYMBOL) -> str:
    return f"{config.DATA_DIR}/{symbol}_{config.INTERVAL}_raw.csv"


def save(df: pd.DataFrame, path: str | None = None) -> None:
    if path is None:
        path = _raw_path(config.SYMBOL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)
    print(f"[data] Saved {len(df):,} rows → {path}")


def load(path: str | None = None) -> pd.DataFrame:
    if path is None:
        path = _raw_path(config.SYMBOL)
    df = pd.read_csv(path, parse_dates=["open_time"])
    print(f"[data] Loaded {len(df):,} rows from {path}")
    return df


def load_or_download(symbol: str = config.SYMBOL,
                     force: bool = False) -> pd.DataFrame:
    """
    Return cached data if available and large enough, otherwise download.

    The cached file is considered stale if it has fewer than 85 % of the
    expected TOTAL_CANDLES rows — e.g. when TOTAL_CANDLES increases from
    30K to 70K, the old 30K file is automatically re-downloaded.

    If the cached file has MORE rows than TOTAL_CANDLES (e.g. it was fetched
    with a larger setting), only the most recent TOTAL_CANDLES rows are
    returned.  This allows TOTAL_CANDLES to act as a "use last N" setting
    without needing a re-download every time the value is changed.
    """
    path      = _raw_path(symbol)
    min_rows  = int(config.TOTAL_CANDLES * 0.85)
    if not force and os.path.exists(path):
        df = load(path)
        if len(df) >= min_rows:
            if len(df) > config.TOTAL_CANDLES:
                # Trim to the most recent TOTAL_CANDLES rows (modern era only)
                df = df.iloc[-config.TOTAL_CANDLES:].reset_index(drop=True)
                print(f"[data] Trimmed cached data to last "
                      f"{config.TOTAL_CANDLES:,} candles "
                      f"(modern institutional era).")
            return df
        print(f"[data] Cached file has {len(df)} rows "
              f"(need ≥ {min_rows}) — re-downloading …")
    df = download(symbol, config.INTERVAL, config.TOTAL_CANDLES)
    save(df, path)
    return df
