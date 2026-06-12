"""SQLite persistence: orders, fills, equity snapshots, paper book, journal.

Everything the bot needs to survive a restart lives here. The schema is
small and append-mostly; a single connection in WAL mode is plenty for a
single-threaded bot.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    client_order_id TEXT,
    ticker          TEXT NOT NULL,
    event_ticker    TEXT DEFAULT '',
    series_ticker   TEXT DEFAULT '',
    strategy        TEXT NOT NULL,
    side            TEXT NOT NULL,
    price_micro     INTEGER NOT NULL,
    count           INTEGER NOT NULL,
    status          TEXT DEFAULT '',
    paper           INTEGER DEFAULT 0,
    created_ts      INTEGER,
    updated_ts      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_orders_ticker ON orders (ticker, created_ts);
CREATE TABLE IF NOT EXISTS fills (
    fill_id     TEXT PRIMARY KEY,
    order_id    TEXT,
    ticker      TEXT,
    side        TEXT,
    count       INTEGER,
    price_micro INTEGER,
    fee_micro   INTEGER,
    is_taker    INTEGER,
    strategy    TEXT DEFAULT '',
    ts          INTEGER
);
CREATE TABLE IF NOT EXISTS snapshots (
    ts            INTEGER PRIMARY KEY,
    cash_micro    INTEGER,
    mtm_micro     INTEGER,
    resting_micro INTEGER,
    equity_micro  INTEGER,
    note          TEXT DEFAULT '',
    mode          TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS paper_positions (
    ticker     TEXT PRIMARY KEY,
    count      INTEGER NOT NULL,
    cost_micro INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id    TEXT PRIMARY KEY,
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL,
    price_micro INTEGER NOT NULL,
    remaining   INTEGER NOT NULL,
    post_only   INTEGER DEFAULT 0,
    reduce_only INTEGER DEFAULT 0,
    strategy    TEXT DEFAULT '',
    created_ts  INTEGER
);
CREATE TABLE IF NOT EXISTS journal (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER,
    kind   TEXT,
    detail TEXT
);
"""


class StateStore:
    def __init__(self, path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # the GUI reads from its own connections while the bot writes
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        try:  # migrate pre-1.2 databases created without the mode column
            self.conn.execute("ALTER TABLE snapshots ADD COLUMN mode TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------- kv
    def kv_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def kv_delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE key=?", (key,))
        self.conn.commit()

    def kv_get_int(self, key: str, default: int = 0) -> int:
        raw = self.kv_get(key)
        try:
            return int(raw) if raw is not None else default
        except ValueError:
            return default

    # --------------------------------------------------------------- orders
    def record_order(
        self,
        order_id: str,
        ticker: str,
        strategy: str,
        side: str,
        price_micro: int,
        count: int,
        status: str,
        client_order_id: str = "",
        event_ticker: str = "",
        series_ticker: str = "",
        paper: bool = False,
        ts: Optional[int] = None,
    ) -> None:
        now = ts if ts is not None else int(time.time())
        self.conn.execute(
            "INSERT OR REPLACE INTO orders (order_id, client_order_id, ticker, event_ticker,"
            " series_ticker, strategy, side, price_micro, count, status, paper, created_ts,"
            " updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, client_order_id, ticker, event_ticker, series_ticker, strategy,
             side, price_micro, count, status, int(paper), now, now),
        )
        self.conn.commit()

    def set_order_status(self, order_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE orders SET status=?, updated_ts=? WHERE order_id=?",
            (status, int(time.time()), order_id),
        )
        self.conn.commit()

    def strategy_of_order(self, order_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT strategy FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        return row[0] if row else None

    def latest_strategy_by_ticker(self) -> Dict[str, str]:
        """ticker -> strategy of the most recent order touching it."""
        rows = self.conn.execute(
            "SELECT ticker, strategy FROM orders ORDER BY created_ts ASC, rowid ASC"
        ).fetchall()
        return {ticker: strategy for ticker, strategy in rows}

    def order_row(self, order_id: str) -> Optional[tuple]:
        return self.conn.execute(
            "SELECT order_id, ticker, strategy, side, price_micro, count, status "
            "FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()

    # ---------------------------------------------------------------- fills
    def record_fill(
        self,
        fill_id: str,
        order_id: str,
        ticker: str,
        side: str,
        count: int,
        price_micro: int,
        fee_micro: int,
        is_taker: bool,
        ts: Optional[int],
        strategy: str = "",
    ) -> bool:
        """Insert a fill once; returns True when newly recorded."""
        if not strategy:
            strategy = self.strategy_of_order(order_id) or ""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO fills (fill_id, order_id, ticker, side, count,"
            " price_micro, fee_micro, is_taker, strategy, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fill_id, order_id, ticker, side, count, price_micro, fee_micro,
             int(is_taker), strategy, ts or int(time.time())),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def fees_paid_since(self, since_ts: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(fee_micro), 0) FROM fills WHERE ts >= ?", (since_ts,)
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------ snapshots
    def save_snapshot(self, ts: int, cash: int, mtm: int, resting: int,
                      equity: int, note: str = "", mode: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots (ts, cash_micro, mtm_micro, resting_micro,"
            " equity_micro, note, mode) VALUES (?,?,?,?,?,?,?)",
            (ts, cash, mtm, resting, equity, note, mode),
        )
        self.conn.commit()

    def latest_snapshot(self, mode: Optional[str] = None
                        ) -> Optional[Tuple[int, int, int, int, int]]:
        where, params = ("WHERE mode=?", (mode,)) if mode is not None else ("", ())
        row = self.conn.execute(
            "SELECT ts, cash_micro, mtm_micro, resting_micro, equity_micro "
            f"FROM snapshots {where} ORDER BY ts DESC LIMIT 1", params
        ).fetchone()
        return tuple(row) if row else None

    def snapshots_since(self, since_ts: int,
                        mode: Optional[str] = None) -> List[Tuple[int, int]]:
        query = "SELECT ts, equity_micro FROM snapshots WHERE ts >= ?"
        params: tuple = (since_ts,)
        if mode is not None:
            query += " AND mode=?"
            params += (mode,)
        rows = self.conn.execute(query + " ORDER BY ts", params).fetchall()
        return [(int(a), int(b)) for a, b in rows]

    # ------------------------------------------------------------ paper book
    def paper_positions(self) -> Dict[str, Tuple[int, int]]:
        rows = self.conn.execute(
            "SELECT ticker, count, cost_micro FROM paper_positions"
        ).fetchall()
        return {t: (int(c), int(cost)) for t, c, cost in rows}

    def paper_set_position(self, ticker: str, count: int, cost_micro: int) -> None:
        if count == 0:
            self.conn.execute("DELETE FROM paper_positions WHERE ticker=?", (ticker,))
        else:
            self.conn.execute(
                "INSERT INTO paper_positions (ticker, count, cost_micro) VALUES (?,?,?) "
                "ON CONFLICT(ticker) DO UPDATE SET count=excluded.count,"
                " cost_micro=excluded.cost_micro",
                (ticker, count, cost_micro),
            )
        self.conn.commit()

    def paper_orders(self) -> List[tuple]:
        return self.conn.execute(
            "SELECT order_id, ticker, side, price_micro, remaining, post_only,"
            " reduce_only, strategy, created_ts FROM paper_orders"
        ).fetchall()

    def paper_save_order(self, order_id: str, ticker: str, side: str, price_micro: int,
                         remaining: int, post_only: bool, reduce_only: bool,
                         strategy: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO paper_orders (order_id, ticker, side, price_micro,"
            " remaining, post_only, reduce_only, strategy, created_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (order_id, ticker, side, price_micro, remaining, int(post_only),
             int(reduce_only), strategy, int(time.time())),
        )
        self.conn.commit()

    def paper_update_order(self, order_id: str, remaining: int) -> None:
        if remaining <= 0:
            self.conn.execute("DELETE FROM paper_orders WHERE order_id=?", (order_id,))
        else:
            self.conn.execute(
                "UPDATE paper_orders SET remaining=? WHERE order_id=?",
                (remaining, order_id),
            )
        self.conn.commit()

    def paper_delete_order(self, order_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM paper_orders WHERE order_id=?", (order_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def paper_reset(self) -> None:
        """Fresh simulated bankroll: positions, orders, cash, the paper-scoped
        risk anchors/halts, and the paper equity history all start over."""
        for table in ("paper_positions", "paper_orders"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.execute("DELETE FROM kv WHERE key=?", ("paper_cash_micro",))
        self.conn.execute("DELETE FROM kv WHERE key LIKE 'paper:%'")
        self.conn.execute("DELETE FROM snapshots WHERE mode LIKE 'paper:%'")
        self.conn.commit()

    # --------------------------------------------------------------- journal
    def journal(self, kind: str, detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO journal (ts, kind, detail) VALUES (?,?,?)",
            (int(time.time()), kind, json.dumps(detail, default=str)),
        )
        self.conn.commit()

    def recent_journal(self, limit: int = 20) -> List[Tuple[int, str, str]]:
        rows = self.conn.execute(
            "SELECT ts, kind, detail FROM journal ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [tuple(r) for r in rows]
