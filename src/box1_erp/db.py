"""SQLite ledger: connection settings and schema (decision D-008)."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id      TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    reservation_id  TEXT NOT NULL,
    site_id         TEXT NOT NULL,
    origin          TEXT NOT NULL,
    requested_at    TEXT NOT NULL,
    correlation_id  TEXT NOT NULL DEFAULT '',
    traceparent     TEXT NOT NULL DEFAULT '',
    equivalence_key TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('PENDING','HELD','SHIPPED','REJECTED')),
    version         INTEGER NOT NULL DEFAULT 1,
    shipment_id     TEXT UNIQUE,
    reject_code     TEXT,
    reject_text     TEXT,
    raw_request     BLOB NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK ((state = 'SHIPPED') = (shipment_id IS NOT NULL)),
    CHECK ((state = 'REJECTED') = (reject_code IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS requests_state ON requests(state, created_at);

CREATE TABLE IF NOT EXISTS request_lines (
    request_id TEXT NOT NULL REFERENCES requests(request_id),
    line_no    INTEGER NOT NULL CHECK (line_no BETWEEN 1 AND 99),
    sku        TEXT NOT NULL,
    quantity   INTEGER NOT NULL CHECK (quantity >= 1),
    PRIMARY KEY (request_id, line_no)
);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY,
    request_id  TEXT NOT NULL UNIQUE REFERENCES requests(request_id),
    shipped_at  TEXT NOT NULL,
    actor_type  TEXT NOT NULL CHECK (actor_type IN ('console','worker')),
    actor_label TEXT NOT NULL DEFAULT ''
);

-- Durable publication intents: one row per result revision, plus error files.
CREATE TABLE IF NOT EXISTS outbox (
    file_id       TEXT PRIMARY KEY,          -- rsl_... or err_...
    kind          TEXT NOT NULL CHECK (kind IN ('RES','ERR')),
    request_id    TEXT,
    revision      INTEGER,
    status        TEXT,
    file_name     TEXT NOT NULL UNIQUE,
    payload       BLOB NOT NULL,             -- fixed at creation, re-emission republishes these bytes
    publish_state TEXT NOT NULL CHECK (publish_state IN ('PENDING_PUBLISH','PUBLISHED')),
    emit_seq      INTEGER NOT NULL DEFAULT 1, -- bumped by re-emission, publish marks only the seq it wrote
    emit_count    INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    published_at  TEXT,
    UNIQUE (request_id, revision)
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(publish_state, created_at);

CREATE TABLE IF NOT EXISTS request_files (
    id          INTEGER PRIMARY KEY,
    request_id  TEXT,
    direction   TEXT NOT NULL CHECK (direction IN ('IN','OUT')),
    file_name   TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS request_files_request ON request_files(request_id, id);

CREATE TABLE IF NOT EXISTS quarantine (
    id             INTEGER PRIMARY KEY,
    file_name      TEXT NOT NULL,
    stored_name    TEXT NOT NULL UNIQUE,
    sha256         TEXT NOT NULL,
    error_code     TEXT NOT NULL,
    detail         TEXT NOT NULL,
    request_id     TEXT,
    state          TEXT NOT NULL CHECK (state IN ('QUARANTINED','REPLAY_REQUESTED','REPLAYED')),
    quarantined_at TEXT NOT NULL,
    replayed_at    TEXT,
    UNIQUE (file_name, sha256)
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    request_id  TEXT,
    action      TEXT NOT NULL,
    prev_state  TEXT,
    new_state   TEXT,
    actor_type  TEXT NOT NULL CHECK (actor_type IN ('console','worker')),
    actor_label TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_request ON audit(request_id, id);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_status (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    pid          INTEGER,
    started_at   TEXT,
    heartbeat_at TEXT,
    activity     TEXT
);

CREATE TABLE IF NOT EXISTS failures (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL,
    component TEXT NOT NULL,
    message   TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "mode": "MANUAL",
    "publication_paused": "0",
    "auto_delay_seconds": "0",
    "crash_after_commit": "0",
}


def now() -> str:
    """UTC timestamp in the ERP1 form, YYYY-MM-DDTHH:MM:SS.sssZ."""
    t = datetime.now(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def parse_ts(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp()


class MissingDatabase(RuntimeError):
    """The ledger does not exist and creation was not requested (e.g. data volume not mounted)."""


def connect(path: str, *, create: bool = False) -> sqlite3.Connection:
    """Open the ledger. A missing ledger is an error unless create=True, so a missing data
    volume never silently produces an empty ledger."""
    if not create and not os.path.exists(path):
        raise MissingDatabase(path)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize(path: str) -> None:
    """Create or upgrade the schema. Safe to run repeatedly."""
    conn = connect(path, create=True)
    try:
        with transaction(conn):
            # executescript() would commit mid-way, so run the statements one by one inside the transaction.
            for stmt in (s.strip() for s in SCHEMA.split(";")):
                if stmt:
                    conn.execute(stmt)
            ts = now()
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute("INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?,?,?)", (k, v, ts))
            conn.execute("INSERT OR IGNORE INTO worker_status(id) VALUES (1)")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE: take the write lock up front (bounded by busy_timeout) so a
    read-then-write cannot be overtaken between its read and its write."""
    for attempt in range(3):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS[key]
