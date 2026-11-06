"""SQLite ledger: connection settings and schema (decision D-008)."""

from __future__ import annotations

import fcntl
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
    shipment_id     TEXT UNIQUE REFERENCES shipments(shipment_id),
    reject_code     TEXT,
    reject_text     TEXT,
    raw_request     BLOB NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK ((state = 'SHIPPED') = (shipment_id IS NOT NULL)),
    CHECK ((state = 'REJECTED') = (reject_code IS NOT NULL)),
    CHECK ((reject_code IS NULL) = (reject_text IS NULL))
);
CREATE INDEX IF NOT EXISTS requests_state ON requests(state, created_at);
CREATE INDEX IF NOT EXISTS requests_updated ON requests(updated_at);

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
    UNIQUE (request_id, revision),
    CHECK (kind <> 'RES' OR (request_id IS NOT NULL AND revision >= 1 AND status IN ('PENDING','HELD','SHIPPED','REJECTED'))),
    CHECK (kind <> 'ERR' OR revision IS NULL)
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(publish_state, created_at);
CREATE INDEX IF NOT EXISTS outbox_request ON outbox(request_id, revision);

CREATE TABLE IF NOT EXISTS request_files (
    id          INTEGER PRIMARY KEY,
    request_id  TEXT,
    direction   TEXT NOT NULL CHECK (direction IN ('IN','OUT')),
    file_name   TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    ingest_key  TEXT,                   -- device:inode:mtime of the upload, for crash-safe re-reads
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS request_files_key ON request_files(ingest_key);
CREATE INDEX IF NOT EXISTS request_files_request ON request_files(request_id, id);

CREATE TABLE IF NOT EXISTS quarantine (
    id             INTEGER PRIMARY KEY,
    file_name      TEXT NOT NULL,
    stored_name    TEXT NOT NULL UNIQUE,
    ingest_key     TEXT NOT NULL UNIQUE,  -- one row per upload, so every upload gets its own ERR
    sha256         TEXT NOT NULL,
    error_code     TEXT NOT NULL,
    detail         TEXT NOT NULL,
    request_id     TEXT,
    state          TEXT NOT NULL CHECK (state IN ('QUARANTINED','REPLAY_REQUESTED','REPLAYED')),
    quarantined_at TEXT NOT NULL,
    replayed_at    TEXT
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

# (version, [statements]) applied in order to ledgers below that version. v1 is the initial schema.
MIGRATIONS: list[tuple[int, list[str]]] = []

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


class SchemaMismatch(RuntimeError):
    """The ledger's schema version differs from this code's. Run `box1-erp migrate`."""


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
    if not create:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            conn.close()
            raise SchemaMismatch(f"ledger schema {version}, code expects {SCHEMA_VERSION}; run `box1-erp migrate`"
                                 if version < SCHEMA_VERSION else
                                 f"ledger schema {version} is newer than this code ({SCHEMA_VERSION}); refusing to open")
    return conn


def lock_path(ledger: str) -> str:
    return os.path.join(os.path.dirname(ledger), "ledger.lock")


class LedgerLock:
    """Shared while a worker or console has the ledger open; exclusive for restore and
    migrate, which therefore refuse while any worker or console is running (even a hung one)."""

    def __init__(self, ledger: str, exclusive: bool = False):
        self.path = lock_path(ledger)
        self.exclusive = exclusive
        self.fd: int | None = None

    def acquire(self) -> bool:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o640)
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def initialize(path: str) -> None:
    """Create the schema (CREATE ... IF NOT EXISTS), or bring an older ledger to SCHEMA_VERSION.
    Safe to run repeatedly. Future versions add ordered steps to MIGRATIONS."""
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
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current > SCHEMA_VERSION:
                raise SchemaMismatch(f"ledger schema {current} is newer than this code ({SCHEMA_VERSION})")
            for version, steps in MIGRATIONS:
                if current < version <= SCHEMA_VERSION:
                    for step in steps:
                        conn.execute(step)
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
        _rollback(conn)
        raise
    try:
        conn.execute("COMMIT")
    except BaseException:
        # A failed COMMIT (disk full, I/O error) leaves the transaction open and the write
        # lock held; roll back so this connection and the other process can continue.
        _rollback(conn)
        raise


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # keep the original exception


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS[key]
