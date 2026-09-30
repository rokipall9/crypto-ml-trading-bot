"""Fetch Bitstamp BTC/USD 1-minute candles, verify them, build daily bars.

Source: github.com/ff137/bitstamp-btcusd-minute-data (historical bulk file +
daily-updated file). Writes into ./data (gitignored):

  btc_1m.pkl      merged minute candles
  btc_daily.csv   one row per UTC day: open/high/low/close/volume and
                  exec_px = close of the 00:04 minute (spec execution price)
  manifest.json   source URLs, sizes, sha256, integrity results

Refuses to write the daily file if any integrity check fails.

The update file grows daily. To rebuild the exact dataset the committed
results were computed on, cut it at the study's last minute:

  python3 fetch_data.py --force --until 2026-09-28T02:00:00Z
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
BASE = ("https://raw.githubusercontent.com/ff137/bitstamp-btcusd-minute-data/"
        "main/data")
SOURCES = {
    "hist.csv.gz": f"{BASE}/historical/btcusd_bitstamp_1min_2012-2025.csv.gz",
    "latest.csv": f"{BASE}/updates/btcusd_bitstamp_1min_latest.csv",
}
DAILY_FROM = "2015-01-01"
EXEC_MINUTE = 4          # close of the 00:04 minute -> filled at ~00:05 UTC


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(force: bool = False) -> dict:
    DATA.mkdir(exist_ok=True)
    out = {}
    for name, url in SOURCES.items():
        path = DATA / name
        if force or not path.exists():
            print(f"downloading {url}")
            urllib.request.urlretrieve(url, path)
        out[name] = {"url": url, "bytes": path.stat().st_size,
                     "sha256": _sha256(path)}
    return out


def load_minutes(until: str | None = None) -> tuple[pd.DataFrame, dict]:
    hist = pd.read_csv(DATA / "hist.csv.gz")
    upd = pd.read_csv(DATA / "latest.csv")
    overlap = hist.merge(upd, on="timestamp", suffixes=("_h", "_u"))
    conflicts = int((overlap[["open_h", "high_h", "low_h", "close_h"]].values
                     != overlap[["open_u", "high_u", "low_u", "close_u"]].values
                     ).any(axis=1).sum()) if len(overlap) else 0
    df = (pd.concat([hist, upd]).drop_duplicates("timestamp", keep="last")
          .sort_values("timestamp").reset_index(drop=True))
    if until:
        df = df[df["timestamp"] <= pd.Timestamp(until).timestamp()] \
            .reset_index(drop=True)
    steps = np.diff(df["timestamp"].values)
    ohlc_bad = int(((df.high < df[["open", "close"]].max(axis=1))
                    | (df.low > df[["open", "close"]].min(axis=1))
                    | (df.low <= 0)).sum())
    checks = {
        "rows": int(len(df)),
        "first_utc": str(pd.to_datetime(df.timestamp.iloc[0], unit="s")),
        "last_utc": str(pd.to_datetime(df.timestamp.iloc[-1], unit="s")),
        "non_60s_steps": int((steps != 60).sum()),
        "ohlc_violations": ohlc_bad,
        "overlap_rows": int(len(overlap)),
        "overlap_conflicts": conflicts,
    }
    return df, checks


def build_daily(df: pd.DataFrame, start: str = DAILY_FROM) -> pd.DataFrame:
    """Daily bar D = minutes stamped in [D 00:00, D+1 00:00) UTC.

    Bitstamp stamps a minute candle by its open time, so the candle stamped
    00:04 closes at 00:05 — that close is the spec's execution price.
    Only complete days (1,440 minutes) are kept.
    """
    t = pd.to_datetime(df["timestamp"], unit="s")
    m = df.assign(day=t.dt.floor("D"), minute=t.dt.hour * 60 + t.dt.minute)
    m = m[m["day"] >= pd.Timestamp(start)]
    g = m.groupby("day")
    daily = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
        "n_minutes": g.size(),
    })
    exec_px = m[m["minute"] == EXEC_MINUTE].set_index("day")["close"]
    daily["exec_px"] = exec_px
    daily = daily[daily["n_minutes"] == 1440].dropna()
    daily.index.name = "date"
    return daily


def _arg(name: str) -> str | None:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def main() -> int:
    files = download(force="--force" in sys.argv)
    until = _arg("--until")
    df, checks = load_minutes(until)
    ok = (checks["non_60s_steps"] == 0 and checks["ohlc_violations"] == 0
          and checks["overlap_conflicts"] == 0)
    print(json.dumps(checks, indent=2))
    if not ok:
        print("INTEGRITY FAILED - daily file not written")
        return 1
    df.to_pickle(DATA / "btc_1m.pkl")
    for stale in ("btc_hourly.csv", "btc_hourly_2013.csv", "btc_daily_2013.csv"):
        (DATA / stale).unlink(missing_ok=True)      # derived from btc_1m.pkl
    daily = build_daily(df)
    daily.to_csv(DATA / "btc_daily.csv")
    manifest = {
        "built_utc": datetime.now(timezone.utc).isoformat(),
        "sources": files,
        "until_utc": until,
        "integrity": checks,
        "daily_rows": int(len(daily)),
        "daily_first": str(daily.index[0].date()),
        "daily_last": str(daily.index[-1].date()),
        "daily_sha256": _sha256(DATA / "btc_daily.csv"),
    }
    (DATA / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"daily rows {len(daily)}  {manifest['daily_first']} -> "
          f"{manifest['daily_last']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
