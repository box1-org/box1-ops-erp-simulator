"""Background worker (box1-erp-worker.service): ingestion, AUTO processing, publication, recovery.

It holds no transaction across file I/O. Every business change goes through domain.py.
"""

from __future__ import annotations

import hashlib
import os
import signal
import time
from pathlib import Path

from . import db, domain, fileops, protocol
from .config import Config
from .log import Log

CRASH_EXIT_CODE = 70


class Worker:
    def __init__(self, cfg: Config, log: Log | None = None):
        self.cfg = cfg
        self.log = log or Log("worker")
        self.stop = False
        self.conn = None

    # ------------------------------------------------------------ lifecycle

    def open(self) -> None:
        for d in (self.cfg.inbound, self.cfg.outbound, self.cfg.consumed, self.cfg.archive, self.cfg.quarantine):
            if not d.is_dir():
                raise FileNotFoundError(f"required directory missing: {d}")
        self.conn = db.connect(str(self.cfg.ledger))  # never creates a ledger: see `box1-erp init`
        removed = fileops.remove_stale_temp(self.cfg.outbound, (".RES.", ".ERR."))
        with db.transaction(self.conn):
            self.conn.execute("UPDATE worker_status SET pid=?, started_at=?, heartbeat_at=?, activity='starting' WHERE id=1",
                              (os.getpid(), db.now(), db.now()))
        pending = self.conn.execute("SELECT COUNT(*) FROM outbox WHERE publish_state='PENDING_PUBLISH'").fetchone()[0]
        self.log.info("worker started", pid=os.getpid(), mode=db.get_setting(self.conn, "mode"),
                      pending_publication=pending, removed_temp=len(removed), demo_faults=self.cfg.demo_faults)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)
        self.open()
        while not self.stop:
            started = time.monotonic()
            self.cycle()
            delay = self.cfg.poll_interval - (time.monotonic() - started)
            if delay > 0 and not self.stop:
                time.sleep(delay)
        with db.transaction(self.conn):
            self.conn.execute("UPDATE worker_status SET activity='stopped', heartbeat_at=? WHERE id=1", (db.now(),))
        self.log.info("worker stopped")
        self.conn.close()

    def _on_term(self, *_):
        self.stop = True

    def cycle(self) -> None:
        """One pass. A failure in one step is recorded and does not stop the others."""
        for step in (self.heartbeat, self.replay, self.ingest, self.auto_process, self.publish, self.purge_consumed):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - keep the worker alive; failure is visible in the TUI
                self.log.error("step failed", step=step.__name__, error=f"{type(exc).__name__}: {exc}")
                try:
                    domain.record_failure(self.conn, step.__name__, f"{type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001,S110 - the ledger itself may be the failure
                    pass

    # ------------------------------------------------------------ steps

    def heartbeat(self) -> None:
        activity = []
        if db.get_setting(self.conn, "publication_paused") == "1" and self.cfg.demo_faults:
            activity.append("publication paused (demo)")
        delay = int(db.get_setting(self.conn, "auto_delay_seconds"))
        if delay and self.cfg.demo_faults:
            activity.append(f"auto delayed {delay}s (demo)")
        with db.transaction(self.conn):
            self.conn.execute("UPDATE worker_status SET heartbeat_at=?, activity=?, pid=? WHERE id=1",
                              (db.now(), "; ".join(activity) or "running", os.getpid()))

    def ingest(self) -> None:
        names = sorted(e.name for e in os.scandir(self.cfg.inbound) if e.is_file(follow_symlinks=False))
        for name in names:
            if self.stop:
                return
            kind = protocol.classify("inbound", name)
            if kind == "ignore":
                continue
            self._ingest_one(self.cfg.inbound / name, name)

    def _stored_name(self, name: str, sha: str) -> str:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in protocol.sanitize(name, 120))
        return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.{sha[:12]}.{safe}"

    def _ingest_one(self, path: Path, name: str, *, replay_id: int | None = None) -> str:
        data = fileops.read_bounded(path, protocol.MAX_FILE_BYTES)
        sha = hashlib.sha256(path.read_bytes() if len(data) > protocol.MAX_FILE_BYTES else data).hexdigest()
        stored = self._stored_name(name, sha)
        try:
            parsed = protocol.parse(data, name)
        except protocol.ProtocolError as err:
            domain.quarantine(self.conn, file_name=name, stored_name=stored, sha256=sha, code=err.code,
                              detail=err.detail, request_id=protocol.request_id_from_name(name))
            self.log.warning("inbound file quarantined", file=protocol.sanitize(name, 255), code=err.code)
            if replay_id is None:
                fileops.move(path, self.cfg.quarantine, stored)
            return f"QUARANTINED:{err.code}"
        outcome = domain.import_request(self.conn, parsed, file_name=name, raw=data, quarantine_name=stored)
        self.log.info("inbound request", file=name, request_id=outcome.request_id, outcome=outcome.outcome,
                      correlation_id=parsed.body["correlationId"], traceparent=parsed.body["traceparent"])
        if replay_id is None:
            target = self.cfg.quarantine if outcome.outcome == "CONFLICT" else self.cfg.archive
            fileops.move(path, target, stored)
        return outcome.outcome

    def replay(self) -> None:
        rows = self.conn.execute("SELECT id, file_name, stored_name FROM quarantine WHERE state='REPLAY_REQUESTED' ORDER BY id").fetchall()
        for row in rows:
            path = self.cfg.quarantine / row["stored_name"]
            if not path.exists():
                domain.record_failure(self.conn, "replay", f"quarantined file missing: {row['stored_name']}")
                domain.replay_done(self.conn, row["id"], "file missing")
                continue
            outcome = self._ingest_one(path, row["file_name"], replay_id=row["id"])
            if not outcome.startswith("QUARANTINED") and outcome != "CONFLICT":
                domain.replay_done(self.conn, row["id"], outcome)
            self.log.info("replayed quarantined file", file=protocol.sanitize(row["file_name"], 255), outcome=outcome)

    def auto_process(self) -> None:
        if db.get_setting(self.conn, "mode") != "AUTO":
            return
        delay = int(db.get_setting(self.conn, "auto_delay_seconds")) if self.cfg.demo_faults else 0
        rows = self.conn.execute("SELECT request_id, version, created_at FROM requests WHERE state='PENDING' ORDER BY created_at").fetchall()
        for row in rows:
            if self.stop:
                return
            if delay and time.time() - db.parse_ts(row["created_at"]) < delay:
                continue
            try:
                shp = domain.ship(self.conn, row["request_id"], expected_version=row["version"], actor=domain.WORKER,
                                  require_auto=True)
                self.log.info("auto shipped", request_id=row["request_id"], shipment_id=shp)
            except domain.DomainError as exc:  # an operator acted first; their action stands
                self.log.info("auto ship skipped", request_id=row["request_id"], reason=str(exc))

    def publish(self) -> None:
        if self.cfg.demo_faults and db.get_setting(self.conn, "publication_paused") == "1":
            return
        rows = self.conn.execute(
            "SELECT file_id, kind, request_id, status, file_name, payload, emit_seq FROM outbox"
            " WHERE publish_state='PENDING_PUBLISH' ORDER BY created_at, file_id").fetchall()
        for row in rows:
            if self.stop:
                return
            if (row["kind"] == "RES" and row["status"] == "SHIPPED" and self.cfg.demo_faults
                    and domain.consume_crash_fault(self.conn)):
                self.log.error("demo fault: one-shot crash after shipment commit, before publication",
                               request_id=row["request_id"], file_id=row["file_id"])
                os._exit(CRASH_EXIT_CODE)
            try:
                fileops.publish(self.cfg.outbound, row["file_name"], row["payload"])
            except OSError as exc:
                with db.transaction(self.conn):
                    self.conn.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE file_id=?",
                                      (protocol.sanitize(str(exc), 200), row["file_id"]))
                domain.record_failure(self.conn, "publish", f"{row['file_name']}: {exc}")
                self.log.error("publication failed", file=row["file_name"], error=str(exc))
                return  # the directory is likely unusable; retry next cycle
            with db.transaction(self.conn):
                self.conn.execute(
                    "UPDATE outbox SET publish_state='PUBLISHED', published_at=?, emit_count=emit_count+1, attempts=attempts+1,"
                    " last_error=NULL WHERE file_id=? AND emit_seq=?", (db.now(), row["file_id"], row["emit_seq"]))
                self.conn.execute(
                    "INSERT INTO request_files(request_id, direction, file_name, sha256, outcome, at) VALUES (?,?,?,?,?,?)",
                    (row["request_id"], "OUT", row["file_name"], hashlib.sha256(row["payload"]).hexdigest(), "PUBLISHED", db.now()))
            self.log.info("published", file=row["file_name"], request_id=row["request_id"], kind=row["kind"], status=row["status"])

    def purge_consumed(self) -> None:
        cutoff = time.time() - self.cfg.consumed_retention_days * 86400
        for entry in os.scandir(self.cfg.consumed):
            if entry.is_file(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)


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
    return 0
