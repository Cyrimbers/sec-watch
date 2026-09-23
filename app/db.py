"""SQLite storage. One connection, used only from the asyncio event-loop thread."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    accession     TEXT PRIMARY KEY,
    cik           TEXT NOT NULL,
    ticker        TEXT,
    company       TEXT,
    form          TEXT NOT NULL,
    filed_at      TEXT NOT NULL,          -- ISO-8601 UTC acceptance time
    url           TEXT,
    items         TEXT,                   -- 8-K item numbers, comma separated
    signal        TEXT,                   -- BUY / SELL / OTHER (Form 4 only)
    buy_value     REAL DEFAULT 0,
    sell_value    REAL DEFAULT 0,
    insider       TEXT,
    insider_role  TEXT,
    codes         TEXT,                   -- transaction codes in the Form 4
    parsed        INTEGER DEFAULT 0,      -- 1 once details fetched (or nothing to fetch)
    alerted       INTEGER DEFAULT 0,      -- 1 once alert rules evaluated
    source        TEXT,                   -- live / sweep / backfill
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_filings_filed ON filings(filed_at DESC);
CREATE INDEX IF NOT EXISTS ix_filings_ticker ON filings(ticker, filed_at DESC);

CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    accession    TEXT NOT NULL,
    owner        TEXT,
    role         TEXT,
    security     TEXT,
    date         TEXT,
    code         TEXT,
    acq_disp     TEXT,
    shares       REAL,
    price        REAL,
    value        REAL,
    owned_after  REAL,
    direct       TEXT,
    plan_10b5_1  INTEGER DEFAULT 0,
    derivative   INTEGER DEFAULT 0,
    footnotes    TEXT                   -- the filing's own note on this line
);
CREATE INDEX IF NOT EXISTS ix_tx_acc ON transactions(accession);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker    TEXT PRIMARY KEY,
    cik       TEXT,
    name      TEXT,
    added_at  TEXT NOT NULL,
    backfilled INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    accession  TEXT,
    sent_at    TEXT NOT NULL,
    title      TEXT,
    body       TEXT,
    ok         INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """One-off data repairs, each guarded by a flag in settings."""
        if not self.get_setting("acceptance_tz_fixed"):
            from .edgar import undo_et_misread
            rows = self.conn.execute(
                "SELECT accession, filed_at FROM filings WHERE source IN ('backfill','sweep')"
            ).fetchall()
            self.conn.executemany(
                "UPDATE filings SET filed_at=? WHERE accession=?",
                [(undo_et_misread(r["filed_at"]), r["accession"]) for r in rows],
            )
            self.conn.commit()
            self.set_setting("acceptance_tz_fixed", True)
            if rows:
                print(f"migration: corrected acceptance times on {len(rows)} filings")

        if not self.get_setting("tx_footnotes"):
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(transactions)")]
            if "footnotes" not in cols:
                self.conn.execute("ALTER TABLE transactions ADD COLUMN footnotes TEXT")
            # re-read stored ownership filings so history gets footnotes and the improved
            # 10b5-1 logic; the worker picks these up a few at a time
            n = self.conn.execute(
                "UPDATE filings SET parsed=0 WHERE form IN ('3','4','5','3/A','4/A','5/A')"
            ).rowcount
            self.conn.commit()
            self.set_setting("tx_footnotes", True)
            if n:
                print(f"migration: queued {n} ownership filings for re-parsing")

    # ---------- settings ----------
    def get_setting(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    # ---------- watchlist ----------
    def watchlist(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM watchlist ORDER BY ticker")]

    def add_ticker(self, ticker: str, cik: str | None, name: str | None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO watchlist(ticker,cik,name,added_at) VALUES(?,?,?,?)",
            (ticker, cik, name, now_iso()),
        )
        self.conn.execute(
            "UPDATE watchlist SET cik=COALESCE(?,cik), name=COALESCE(?,name) WHERE ticker=?",
            (cik, name, ticker),
        )
        self.conn.commit()

    def remove_ticker(self, ticker: str) -> None:
        self.conn.execute("DELETE FROM watchlist WHERE ticker=?", (ticker,))
        self.conn.commit()

    def mark_backfilled(self, ticker: str) -> None:
        self.conn.execute("UPDATE watchlist SET backfilled=1 WHERE ticker=?", (ticker,))
        self.conn.commit()

    # ---------- filings ----------
    def has_filing(self, accession: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM filings WHERE accession=?", (accession,)
        ).fetchone() is not None

    def insert_filing(self, f: dict) -> bool:
        """Insert if new. Returns True when the row was new."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO filings
               (accession,cik,ticker,company,form,filed_at,url,items,source,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (f["accession"], f["cik"], f.get("ticker"), f.get("company"), f["form"],
             f["filed_at"], f.get("url"), f.get("items"), f.get("source"), now_iso()),
        )
        if cur.rowcount == 0 and f.get("items"):
            # the live feed has no 8-K items; the sweep does, so fill them in
            self.conn.execute(
                "UPDATE filings SET items=? WHERE accession=? AND (items IS NULL OR items='')",
                (f["items"], f["accession"]),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def update_filing(self, accession: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE filings SET {cols} WHERE accession=?", (*fields.values(), accession))
        self.conn.commit()

    def replace_transactions(self, accession: str, txs: list[dict]) -> None:
        self.conn.execute("DELETE FROM transactions WHERE accession=?", (accession,))
        self.conn.executemany(
            """INSERT INTO transactions
               (accession,owner,role,security,date,code,acq_disp,shares,price,value,
                owned_after,direct,plan_10b5_1,derivative,footnotes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(accession, t.get("owner"), t.get("role"), t.get("security"), t.get("date"),
              t.get("code"), t.get("acq_disp"), t.get("shares"), t.get("price"), t.get("value"),
              t.get("owned_after"), t.get("direct"), int(bool(t.get("plan_10b5_1"))),
              int(bool(t.get("derivative"))), t.get("footnotes")) for t in txs],
        )
        self.conn.commit()

    def pending_parse(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM filings WHERE parsed=0 ORDER BY filed_at DESC LIMIT ?", (limit,))]

    def pending_alert(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM filings WHERE alerted=0 AND parsed=1 ORDER BY filed_at ASC")]

    def transactions(self, accession: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM transactions WHERE accession=? ORDER BY derivative, id", (accession,))]

    def claim_alert(self, accession: str) -> bool:
        """Mark a filing as alerted, returning True only for the caller that won the claim.

        The UPDATE is atomic, so two concurrent evaluators can't both send for one filing.
        """
        cur = self.conn.execute(
            "UPDATE filings SET alerted=1 WHERE accession=? AND alerted=0", (accession,))
        self.conn.commit()
        return cur.rowcount == 1

    def log_alert(self, accession: str | None, title: str, body: str, ok: bool) -> None:
        self.conn.execute(
            "INSERT INTO alert_log(accession,sent_at,title,body,ok) VALUES(?,?,?,?,?)",
            (accession, now_iso(), title, body, int(ok)),
        )
        self.conn.commit()

    def recent_alerts(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,))]
