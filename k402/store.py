# Payment stores persist issued offers and enforce single-use payment_ids.
# mark_used() must be atomic — it is the replay-protection primitive.
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class PaymentRecord:
    payment_id: str
    address: str
    amount_sompi: int
    expires: int
    used: bool = False
    meta: dict = field(default_factory=dict)
    # atomic units the address had already received when this offer was created. Verification
    # requires (current received - baseline) >= amount, so a REUSED address's standing balance /
    # history can never auto-satisfy an offer — only new funds paid after the offer count.
    baseline: int = 0

    @property
    def expired(self) -> bool:
        return time.time() > self.expires


class PaymentStore(Protocol):
    def create(self, record: PaymentRecord) -> None: ...
    def get(self, payment_id: str) -> Optional[PaymentRecord]: ...
    def mark_used(self, payment_id: str) -> bool:
        """True iff this call transitioned the record from unused to used."""
        ...


class MemoryStore:
    """Process-local store. Fine for a single-worker dev server."""

    def __init__(self):
        self._records: dict[str, PaymentRecord] = {}
        self._lock = threading.Lock()

    def create(self, record: PaymentRecord) -> None:
        with self._lock:
            self._records[record.payment_id] = record

    def get(self, payment_id: str) -> Optional[PaymentRecord]:
        with self._lock:
            return self._records.get(payment_id)

    def mark_used(self, payment_id: str) -> bool:
        with self._lock:
            rec = self._records.get(payment_id)
            if rec is None or rec.used:
                return False
            rec.used = True
            return True


class SqliteStore:
    """Durable store; safe across restarts and multiple workers on one host."""

    def __init__(self, path: str):
        self._path = path
        self._local = threading.local()
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS k402_payments (
                payment_id TEXT PRIMARY KEY,
                address TEXT NOT NULL,
                amount_sompi INTEGER NOT NULL,
                expires INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                meta TEXT NOT NULL DEFAULT '{}',
                baseline INTEGER NOT NULL DEFAULT 0)""")
            # migrate older dbs that predate the baseline column
            if "baseline" not in {r[1] for r in c.execute("PRAGMA table_info(k402_payments)")}:
                c.execute("ALTER TABLE k402_payments ADD COLUMN baseline INTEGER NOT NULL DEFAULT 0")

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    def create(self, record: PaymentRecord) -> None:
        self._conn().execute(
            "INSERT INTO k402_payments (payment_id, address, amount_sompi, expires, used, meta, baseline) "
            "VALUES (?,?,?,?,?,?,?)",
            (record.payment_id, record.address, record.amount_sompi,
             record.expires, int(record.used), json.dumps(record.meta), record.baseline))

    def get(self, payment_id: str) -> Optional[PaymentRecord]:
        row = self._conn().execute(
            "SELECT payment_id, address, amount_sompi, expires, used, meta, baseline "
            "FROM k402_payments WHERE payment_id=?", (payment_id,)).fetchone()
        if row is None:
            return None
        return PaymentRecord(payment_id=row[0], address=row[1], amount_sompi=row[2],
                             expires=row[3], used=bool(row[4]), meta=json.loads(row[5]),
                             baseline=row[6])

    def mark_used(self, payment_id: str) -> bool:
        cur = self._conn().execute(
            "UPDATE k402_payments SET used=1 WHERE payment_id=? AND used=0",
            (payment_id,))
        return cur.rowcount == 1
