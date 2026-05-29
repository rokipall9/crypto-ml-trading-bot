"""
db.py — Optional SQLite backend for forward_results.

JSONL is fine for tens of thousands of records. SQLite becomes
preferable past ~100k for indexed queries (by date / strategy /
score). This module is ADDITIVE — JSONL stays the source of truth;
SQLite is an indexed mirror that can be rebuilt at any time.

Use:
    db.migrate_from_jsonl()             # one-shot or recurring rebuild
    db.query(strategy="BREAKOUT_4H",
             min_score=8, since="2026-04-01")  # fast indexed lookup
    db.stats()                          # pre-aggregated counters
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger
from ledger import LEDGER

log = get_logger("db")

DB_PATH = "/home/ubuntu/common/forward_results.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    TEXT,
    event       TEXT NOT NULL,
    bot         TEXT,
    system      TEXT,
    symbol      TEXT,
    side        TEXT,
    score       INTEGER,
    entry       REAL,
    sl          REAL,
    tp          REAL,
    exit_price  REAL,
    status      TEXT,
    r           REAL,
    opened_at   TEXT,
    exit_time   TEXT,
    raw         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_system    ON events(event, system);
CREATE INDEX IF NOT EXISTS idx_status          ON events(status);
CREATE INDEX IF NOT EXISTS idx_score           ON events(score);
CREATE INDEX IF NOT EXISTS idx_opened_at       ON events(opened_at);
CREATE INDEX IF NOT EXISTS idx_exit_time       ON events(exit_time);
CREATE INDEX IF NOT EXISTS idx_trade_id        ON events(trade_id);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _ensure_schema() -> None:
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _trade_id(rec: dict) -> str:
    return (rec.get("id") or rec.get("trade_id")
            or f"{rec.get('bot','?')}|{rec.get('system','?')}|"
               f"{rec.get('opened_at','?')}")


def migrate_from_jsonl(rebuild: bool = False) -> Dict[str, int]:
    """Replay forward_results.jsonl into SQLite.
    rebuild=True: truncate + full reload. False: incremental (skip seen)."""
    _ensure_schema()
    conn = _connect()
    try:
        if rebuild:
            conn.execute("DELETE FROM events;")
            log.info("db_truncated")

        # If incremental, find last imported trade_id+event combo
        seen_pairs = set()
        if not rebuild:
            for row in conn.execute("SELECT trade_id, event FROM events"):
                seen_pairs.add((row["trade_id"], row["event"]))

        if not os.path.exists(LEDGER):
            return {"imported": 0, "skipped": 0, "total": 0}

        imported = 0; skipped = 0; total = 0
        with open(LEDGER, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                except Exception:
                    skipped += 1
                    continue
                tid = _trade_id(rec); evt = rec.get("event", "")
                if not rebuild and (tid, evt) in seen_pairs:
                    skipped += 1
                    continue
                conn.execute("""
                    INSERT INTO events (
                        trade_id, event, bot, system, symbol, side, score,
                        entry, sl, tp, exit_price, status, r,
                        opened_at, exit_time, raw)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    tid, evt, rec.get("bot"), rec.get("system"),
                    rec.get("symbol"), rec.get("side"),
                    int(rec["score"]) if rec.get("score") is not None else None,
                    rec.get("entry"), rec.get("sl"), rec.get("tp"),
                    rec.get("exit_price"), rec.get("status"), rec.get("r"),
                    rec.get("opened_at"), rec.get("exit_time"),
                    json.dumps(rec, default=str),
                ))
                imported += 1

        conn.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('last_migration', ?)",
            (datetime.now(timezone.utc).isoformat(),))
        log.info("migration_complete", imported=imported, skipped=skipped,
                 total=total)
        return {"imported": imported, "skipped": skipped, "total": total}
    finally:
        conn.close()


def query(strategy: Optional[str] = None,
          symbol: Optional[str] = None,
          min_score: Optional[int] = None,
          status: Optional[str] = None,
          event: Optional[str] = None,
          since: Optional[str] = None,
          until: Optional[str] = None,
          limit: int = 500) -> List[Dict[str, Any]]:
    if not os.path.exists(DB_PATH):
        _ensure_schema()
    conn = _connect()
    try:
        clauses = []
        params: list = []
        if strategy:  clauses.append("system = ?");    params.append(strategy)
        if symbol:    clauses.append("symbol = ?");    params.append(symbol)
        if min_score is not None:
            clauses.append("score >= ?"); params.append(int(min_score))
        if status:    clauses.append("status = ?");    params.append(status)
        if event:     clauses.append("event = ?");     params.append(event)
        if since:     clauses.append("opened_at >= ?"); params.append(since)
        if until:     clauses.append("opened_at <= ?"); params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (f"SELECT * FROM events {where} "
               f"ORDER BY opened_at DESC LIMIT ?")
        params.append(int(limit))
        rows = []
        for row in conn.execute(sql, params):
            d = dict(row)
            # Hydrate any extra fields from raw
            try:
                raw = json.loads(d.pop("raw"))
                for k, v in raw.items():
                    d.setdefault(k, v)
            except Exception:
                pass
            rows.append(d)
        return rows
    finally:
        conn.close()


def stats() -> Dict[str, Any]:
    if not os.path.exists(DB_PATH):
        _ensure_schema()
    conn = _connect()
    try:
        out = {
            "total_events": conn.execute(
                "SELECT COUNT(*) FROM events").fetchone()[0],
            "alerts": conn.execute(
                "SELECT COUNT(*) FROM events WHERE event='alert'"
            ).fetchone()[0],
            "closes": conn.execute(
                "SELECT COUNT(*) FROM events WHERE event='close'"
            ).fetchone()[0],
            "by_status": dict(conn.execute(
                "SELECT status, COUNT(*) FROM events "
                "WHERE event='close' GROUP BY status").fetchall()),
            "by_system": dict(conn.execute(
                "SELECT system, COUNT(*) FROM events "
                "WHERE event='close' GROUP BY system").fetchall()),
            "last_migration": conn.execute(
                "SELECT value FROM metadata WHERE key='last_migration'"
            ).fetchone(),
        }
        if out["last_migration"]:
            out["last_migration"] = out["last_migration"][0]
        return out
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        rebuild = "--rebuild" in sys.argv
        print(json.dumps(migrate_from_jsonl(rebuild=rebuild), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(stats(), indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "query":
        # argv 2..: key=val pairs
        kw = {}
        for arg in sys.argv[2:]:
            if "=" in arg:
                k, v = arg.split("=", 1)
                if k == "min_score":  v = int(v)
                if k == "limit":      v = int(v)
                kw[k] = v
        print(json.dumps({"items": query(**kw)}, indent=2, default=str))
    else:
        print("Usage:")
        print("  db.py migrate [--rebuild]")
        print("  db.py stats")
        print("  db.py query strategy=BREAKOUT_4H min_score=8")
