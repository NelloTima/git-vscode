"""SQLite storage helpers. One file per app, no ORM, parameterised queries everywhere."""
from __future__ import annotations

import random
import sqlite3
import string
from datetime import datetime

# Tables both apps keep (each in its own database file).
COMMON_SCHEMA = """
CREATE TABLE IF NOT EXISTS partners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT,
    city TEXT NOT NULL,
    nuit TEXT,
    fleet_size INTEGER DEFAULT 1,
    categories TEXT,
    status TEXT NOT NULL DEFAULT 'pendente',
    rating REAL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_ref TEXT NOT NULL,
    method TEXT NOT NULL,
    msisdn TEXT,
    amount INTEGER NOT NULL,
    provider_ref TEXT,
    status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    phone TEXT,
    summary TEXT,
    source TEXT DEFAULT 'assistente',
    created_at TEXT NOT NULL
);
"""

REF_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def short_code(prefix: str, n: int = 6) -> str:
    """Code customers can read over the phone (no O/0/I/1)."""
    return prefix + "".join(random.choices(REF_ALPHABET, k=n))


class Database:
    def __init__(self, path: str):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        """Use as `with db.connect() as conn:` (commits on success, rolls back on error)."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self, *schemas: str) -> None:
        with self.connect() as conn:
            conn.executescript(COMMON_SCHEMA)
            for s in schemas:
                conn.executescript(s)

    def record_payment(self, ref: str, method: str, msisdn: str, amount: int, result) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO payments (booking_ref, method, msisdn, amount, provider_ref, status, message,"
                         " created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (ref, method, msisdn, amount, result.provider_ref, result.status, result.message, now()))
