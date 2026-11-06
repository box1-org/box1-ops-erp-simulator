"""Background worker (box1-erp-worker.service): ingestion, AUTO processing, publication, recovery.

It holds no transaction across file I/O, and every business change goes through
domain.py. The SFTP account controls what appears in inbound/ and outbound/, so
nothing here follows a link, keeps an inode the SFTP account owns, or lets one bad
entry block the others.
"""

from __future__ import annotations

import errno
import hashlib
import os
import signal
import socket
import stat
import time
from pathlib import Path

from . import db, domain, fileops, protocol
from .config import Config
from .log import Log

CRASH_EXIT_CODE = 70
BACKOFF_FIRST, BACKOFF_MAX = 5.0, 300.0
HEARTBEAT_EVERY = 2.0


def sd_notify(message: str) -> None:
    """systemd notification (READY=1, WATCHDOG=1) with the standard library only."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
    except OSError:
        pass


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in protocol.sanitize(name, 120))


def stored_name(key: str, name: str, prefix: str = "") -> str:
    """Deterministic per upload, so a re-run after a crash uses the same name."""
    return f"{prefix}{hashlib.sha256(key.encode()).hexdigest()[:16]}.{_safe(name)}"


class Backoff:
    def __init__(self):
        self.state: dict[str, tuple[float, float]] = {}

    def ready(self, key: str) -> bool:
        return time.monotonic() >= self.state.get(key, (0.0, 0.0))[0]

    def failed(self, key: str) -> float:
        delay = min(BACKOFF_MAX, self.state.get(key, (0.0, BACKOFF_FIRST / 2))[1] * 2)
        self.state[key] = (time.monotonic() + delay, delay)
        return delay

    def ok(self, key: str) -> None:
        self.state.pop(key, None)


class Worker:
    def __init__(self, cfg: Config, log: Log | None = None):
        self.cfg = cfg
        self.log = log or Log("worker")
        self.stop = False
        self.conn = None
        self.lock = None
        self.inbound_backoff = Backoff()
        self.publish_backoff = Backoff()
        self._last_beat = 0.0

    # ------------------------------------------------------------ lifecycle

    def open(self) -> None:
        for d in (self.cfg.inbound, self.cfg.outbound, self.cfg.consumed, self.cfg.archive, self.cfg.quarantine):
            if not d.is_dir():
                raise FileNotFoundError(f"required directory missing: {d}")
        if not self.cfg.ledger.exists():
            raise db.MissingDatabase(str(self.cfg.ledger))
        self.lock = db.LedgerLock(str(self.cfg.ledger))
        if not self.lock.acquire():
            raise RuntimeError("ledger is locked for restore or migration")
        self.conn = db.connect(str(self.cfg.ledger))  # never creates a ledger: see `box1-erp init`
        removed = fileops.remove_stale_temp(self.cfg.outbound) + fileops.remove_stale_temp(self.cfg.archive) \
            + fileops.remove_stale_temp(self.cfg.quarantine)
        if not self.cfg.demo_faults:
            reset = domain.reset_faults(self.conn)
            if reset:
                self.log.warning("demo faults disabled in configuration; stored fault settings cleared", cleared=reset)
        with db.transaction(self.conn):
            self.conn.execute("UPDATE worker_status SET pid=?, started_at=?, heartbeat_at=?, activity='starting' WHERE id=1",
                              (os.getpid(), db.now(), db.now()))
        pending = self.conn.execute("SELECT COUNT(*) FROM outbox WHERE publish_state='PENDING_PUBLISH'").fetchone()[0]
        self.log.info("worker started", pid=os.getpid(), mode=db.get_setting(self.conn, "mode"),
                      pending_publication=pending, removed_temp=len(removed), demo_faults=self.cfg.demo_faults)
        sd_notify("READY=1")

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self.lock is not None:
            self.lock.release()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)
        self.open()
        try:
            while not self.stop:
                started = time.monotonic()
                self.cycle()
                delay = self.cfg.poll_interval - (time.monotonic() - started)
                if delay > 0 and not self.stop:
                    time.sleep(delay)
            sd_notify("STOPPING=1")
            with db.transaction(self.conn):
                self.conn.execute("UPDATE worker_status SET activity='stopped', heartbeat_at=? WHERE id=1", (db.now(),))
            self.log.info("worker stopped")
        finally:
            self.close()

    def _on_term(self, *_):
        self.stop = True

    def cycle(self) -> None:
        """One pass. A failure in one step is recorded and does not stop the others."""
        self._last_beat = 0.0
        for step in (self.beat, self.replay, self.ingest, self.auto_process, self.publish, self.purge_consumed):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - keep the worker alive; failure is visible in the TUI
                self._failure(step.__name__, f"{type(exc).__name__}: {exc}")

    def _failure(self, component: str, message: str, **fields) -> None:
        self.log.error("step failed", step=component, error=message, **fields)
        try:
            domain.record_failure(self.conn, component, message)
        except Exception:  # noqa: BLE001,S110 - the ledger itself may be the failure
            pass

    # ------------------------------------------------------------ heartbeat

    def beat(self) -> None:
        """Heartbeat and watchdog. Called each cycle and inside long loops, throttled."""
        now = time.monotonic()
        if now - self._last_beat < HEARTBEAT_EVERY:
            return
        self._last_beat = now
        activity = []
        if self.cfg.demo_faults and db.get_setting(self.conn, "publication_paused") == "1":
            activity.append("publication paused (demo)")
        delay = int(db.get_setting(self.conn, "auto_delay_seconds")) if self.cfg.demo_faults else 0
        if delay:
            activity.append(f"auto delayed {delay}s (demo)")
        with db.transaction(self.conn):
            self.conn.execute("UPDATE worker_status SET heartbeat_at=?, activity=?, pid=? WHERE id=1",
                              (db.now(), "; ".join(activity) or "running", os.getpid()))
        sd_notify("WATCHDOG=1")

    # ------------------------------------------------------------ inbound

    def ingest(self) -> None:
        names = sorted(e.name for e in os.scandir(self.cfg.inbound))
        for name in names:
            if self.stop:
                return
            if protocol.classify("inbound", name) == "ignore":
                continue
            if not self.inbound_backoff.ready(name):
                continue
            self.beat()
            try:
                self._ingest_entry(self.cfg.inbound / name, name)
                self.inbound_backoff.ok(name)
            except FileNotFoundError:
                self.inbound_backoff.ok(name)  # the adapter removed or renamed it meanwhile
            except Exception as exc:  # noqa: BLE001 - one bad entry must not block the others
                delay = self.inbound_backoff.failed(name)
                self._failure("ingest", f"{protocol.sanitize(name, 80)}: {type(exc).__name__}: {exc}", retry_in_s=delay)

    def _ingest_entry(self, path: Path, name: str) -> None:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            self._quarantine_unopened(path, name, st, "not a regular file")
            return
        try:
            inb = fileops.open_inbound(path, protocol.MAX_FILE_BYTES)
        except (PermissionError, fileops.Unsafe) as exc:
            self._quarantine_unopened(path, name, st, f"cannot read: {type(exc).__name__}")
            return
        except OSError as exc:
            if exc.errno == errno.ELOOP:  # swapped for a link after the scan
                self._quarantine_unopened(path, name, os.lstat(path), "link")
                return
            raise
        try:
            self._ingest_open(path, name, inb)
        finally:
            inb.close()

    def _ingest_open(self, path: Path, name: str, inb: fileops.Inbound) -> None:
        stored = stored_name(inb.key, name)
        too_large = inb.size > protocol.MAX_FILE_BYTES
        keep = protocol.MAX_FILE_BYTES + 1 if too_large else None  # store only a prefix of an oversized upload
        done = domain.already_ingested(self.conn, inb.key)
        if done:  # recorded before a crash or a failed move: only finish moving it
            target = self.cfg.quarantine if done == "quarantine" else self.cfg.archive
            fileops.store_copy(inb, target, stored, keep)
            fileops.unlink_quietly(path)
            self.log.info("finished moving an already recorded upload", file=name, to=target.name)
            return
        ful = protocol.request_id_from_name(name)
        try:
            parsed = protocol.parse(inb.head, name)
        except protocol.ProtocolError as err:
            domain.quarantine(self.conn, file_name=name, stored_name=stored, sha256=inb.sha256, code=err.code,
                              detail=err.detail, request_id=ful, ingest_key=inb.key)
            fileops.store_copy(inb, self.cfg.quarantine, stored, keep)
            fileops.unlink_quietly(path)
            self.log.warning("inbound file quarantined", file=protocol.sanitize(name, 255), code=err.code, request_id=ful,
                             size=inb.size)
            return
        outcome = domain.import_request(self.conn, parsed, file_name=name, raw=inb.head, ingest_key=inb.key,
                                        quarantine_name=stored, sha256=inb.sha256)
        target = self.cfg.quarantine if outcome.outcome == "CONFLICT" else self.cfg.archive
        fileops.store_copy(inb, target, stored)
        fileops.unlink_quietly(path)
        self.log.info("inbound request", file=name, request_id=outcome.request_id, outcome=outcome.outcome,
                      correlation_id=parsed.body["correlationId"], traceparent=parsed.body["traceparent"],
                      file_id=outcome.file_id)

    def _quarantine_unopened(self, path: Path, name: str, st: os.stat_result, why: str) -> None:
        """Directories, links, devices and unreadable files move to quarantine unopened (SPEC: UNREADABLE)."""
        key = f"{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}"
        stored = stored_name(key, name, prefix="unread.")
        domain.quarantine(self.conn, file_name=name, stored_name=stored, sha256="-", code="UNREADABLE",
                          detail=f"{why}; moved unopened", request_id=protocol.request_id_from_name(name), ingest_key=key)
        fileops.move_unopened(path, self.cfg.quarantine, stored)
        self.log.warning("inbound entry quarantined unopened", file=protocol.sanitize(name, 255), code="UNREADABLE", why=why)

    def replay(self) -> None:
        rows = self.conn.execute(
            "SELECT id, file_name, stored_name FROM quarantine WHERE state='REPLAY_REQUESTED' ORDER BY id").fetchall()
        for row in rows:
            self.beat()
            path = self.cfg.quarantine / row["stored_name"]
            try:
                inb = fileops.open_stored(path, protocol.MAX_FILE_BYTES)
            except FileNotFoundError:
                domain.replay_failed(self.conn, row["id"], "UNREADABLE", "stored quarantine file is missing")
                self.log.error("replay: stored file missing", quarantine_id=row["id"])
                continue
            except (OSError, fileops.Unsafe) as exc:
                domain.replay_failed(self.conn, row["id"], "UNREADABLE", f"cannot read stored file: {type(exc).__name__}")
                continue
            try:
                parsed = protocol.parse(inb.head, row["file_name"])
            except protocol.ProtocolError as err:
                domain.replay_failed(self.conn, row["id"], err.code, err.detail)
                self.log.info("replay: still invalid", quarantine_id=row["id"], code=err.code)
                inb.close()
                continue
            inb.close()
            outcome = domain.import_request(self.conn, parsed, file_name=row["file_name"], raw=inb.head, replay=True,
                                            sha256=inb.sha256)
            if outcome.outcome == "CONFLICT":
                domain.replay_failed(self.conn, row["id"], "CONFLICTING_REUSE",
                                     f"{outcome.request_id} already exists with different content")
            else:
                domain.replay_done(self.conn, row["id"], outcome.outcome)
                os.replace(path, self.cfg.archive / row["stored_name"])
                fileops.fsync_dir(self.cfg.archive)
            self.log.info("replayed quarantined file", quarantine_id=row["id"], request_id=outcome.request_id,
                          outcome=outcome.outcome)

    # ------------------------------------------------------------ processing

    def auto_process(self) -> None:
        if db.get_setting(self.conn, "mode") != "AUTO":
            return
        delay = int(db.get_setting(self.conn, "auto_delay_seconds")) if self.cfg.demo_faults else 0
        rows = self.conn.execute("SELECT request_id, version, created_at, correlation_id, traceparent FROM requests"
                                 " WHERE state='PENDING' ORDER BY created_at").fetchall()
        for row in rows:
            if self.stop:
                return
            self.beat()
            if delay and time.time() - db.parse_ts(row["created_at"]) < delay:
                continue
            try:
                shp = domain.ship(self.conn, row["request_id"], expected_version=row["version"], actor=domain.WORKER,
                                  require_auto=True)
                self.log.info("auto shipped", request_id=row["request_id"], shipment_id=shp,
                              correlation_id=row["correlation_id"], traceparent=row["traceparent"])
            except domain.DomainError as exc:  # an operator acted first; their action stands
                self.log.info("auto ship skipped", request_id=row["request_id"], reason=str(exc))

    def publish(self) -> None:
        if self.cfg.demo_faults and db.get_setting(self.conn, "publication_paused") == "1":
            return
        rows = self.conn.execute(
            "SELECT o.file_id, o.kind, o.request_id, o.status, o.file_name, o.payload, o.emit_seq, o.emit_count,"
            " r.correlation_id, r.traceparent FROM outbox o LEFT JOIN requests r ON r.request_id=o.request_id"
            " WHERE o.publish_state='PENDING_PUBLISH' ORDER BY o.created_at, o.request_id, o.revision, o.file_id").fetchall()
        for row in rows:
            if self.stop:
                return
            if not self.publish_backoff.ready(row["file_id"]):
                continue
            self.beat()
            if (row["kind"] == "RES" and row["status"] == "SHIPPED" and row["emit_count"] == 0 and self.cfg.demo_faults
                    and domain.consume_crash_fault(self.conn)):
                self.log.error("demo fault: one-shot crash after shipment commit, before publication",
                               request_id=row["request_id"], file_id=row["file_id"])
                os._exit(CRASH_EXIT_CODE)
            try:
                fileops.publish(self.cfg.outbound, row["file_name"], row["payload"])
            except OSError as exc:
                delay = self.publish_backoff.failed(row["file_id"])
                with db.transaction(self.conn):
                    self.conn.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE file_id=?",
                                      (protocol.sanitize(str(exc), 200), row["file_id"]))
                self._failure("publish", f"{row['file_name']}: {exc}", request_id=row["request_id"], retry_in_s=delay)
                continue  # one bad target never blocks the other results
            self.publish_backoff.ok(row["file_id"])
            with db.transaction(self.conn):
                self.conn.execute(
                    "UPDATE outbox SET publish_state='PUBLISHED', published_at=?, emit_count=emit_count+1, attempts=attempts+1,"
                    " last_error=NULL WHERE file_id=? AND emit_seq=?", (db.now(), row["file_id"], row["emit_seq"]))
                self.conn.execute(
                    "INSERT INTO request_files(request_id, direction, file_name, sha256, outcome, at) VALUES (?,?,?,?,?,?)",
                    (row["request_id"], "OUT", row["file_name"], hashlib.sha256(row["payload"]).hexdigest(), "PUBLISHED", db.now()))
            self.log.info("published", file=row["file_name"], file_id=row["file_id"], request_id=row["request_id"],
                          kind=row["kind"], status=row["status"], correlation_id=row["correlation_id"],
                          traceparent=row["traceparent"])

    def purge_consumed(self) -> None:
        cutoff = time.time() - self.cfg.consumed_retention_days * 86400
        for entry in os.scandir(self.cfg.consumed):
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)
            except (FileNotFoundError, IsADirectoryError, PermissionError):
                continue  # concurrent removal by the adapter, or an entry we may not remove


def main(cfg: Config) -> int:
    log = Log("worker")
    try:
        Worker(cfg, log).run()
    except db.MissingDatabase as exc:
        log.error("ledger missing; is the data volume mounted? refusing to create one", path=str(exc))
        return 2
    except FileNotFoundError as exc:
        log.error("required path missing; is the data volume mounted?", error=str(exc))
        return 2
    except db.SchemaMismatch as exc:
        log.error("ledger schema mismatch", error=str(exc))
        return 2
    except RuntimeError as exc:
        log.error("worker cannot start", error=str(exc))
        return 1
    return 0
