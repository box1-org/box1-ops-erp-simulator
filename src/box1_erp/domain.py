"""Domain layer shared by the worker and the console TUI.

Every business rule lives here: request identity and equivalence, state
transitions, shipment creation, audit records and durable result intents.
Each public function is one short BEGIN IMMEDIATE transaction; none waits for
user input or file I/O while it holds the lock (decision D-008).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass

from . import protocol
from .db import get_setting, now, transaction

CONSOLE = "console"
WORKER = "worker"
MODES = ("MANUAL", "AUTO")

# Allowed business transitions. SHIPPED and REJECTED are terminal.
TRANSITIONS = {
    "HOLD": {"PENDING": "HELD"},
    "RELEASE": {"HELD": "PENDING"},
    "SHIP": {"PENDING": "SHIPPED", "HELD": "SHIPPED"},
    "REJECT": {"PENDING": "REJECTED", "HELD": "REJECTED"},
}
AUTO_SHIP_FROM = ("PENDING",)  # automatic processing respects holds
MAX_LABEL = 40
MAX_REASON = 200


class DomainError(Exception):
    """A rule refused the action. The message is safe to show on the console."""


class StaleVersion(DomainError):
    pass


@dataclass(frozen=True)
class ImportOutcome:
    outcome: str  # NEW, DUPLICATE or CONFLICT
    request_id: str
    file_id: str | None  # result re-published (NEW, DUPLICATE) or error file (CONFLICT)


def _audit(conn, *, request_id, action, prev, new, actor, label="", reason="", detail=""):
    conn.execute(
        "INSERT INTO audit(at, request_id, action, prev_state, new_state, actor_type, actor_label, reason, detail)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (now(), request_id, action, prev, new, actor, label, reason, detail),
    )


def _check_actor(actor: str, label: str) -> str:
    if actor not in (CONSOLE, WORKER):
        raise DomainError(f"unknown actor {actor}")
    label = protocol.sanitize(label.strip(), MAX_LABEL) if label.strip() else ""
    return label


def _new_result(conn, req: sqlite3.Row, status: str, *, status_at: str, shipment_id: str = "",
                reason_code: str = "", reason_text: str = "") -> str:
    revision = conn.execute("SELECT COALESCE(MAX(revision),0)+1 FROM outbox WHERE request_id=? AND kind='RES'",
                            (req["request_id"],)).fetchone()[0]
    result_id = "rsl_" + secrets.token_hex(16)
    payload = protocol.render_result(
        created_at=now(), result_id=result_id, ful_id=req["request_id"], revision=revision, status=status,
        order_id=req["order_id"], shipment_id=shipment_id, status_at=status_at, reason_code=reason_code,
        reason_text=reason_text, correlation_id=req["correlation_id"], traceparent=req["traceparent"])
    name = protocol.result_name(req["request_id"], revision, result_id)
    conn.execute(
        "INSERT INTO outbox(file_id, kind, request_id, revision, status, file_name, payload, publish_state, created_at)"
        " VALUES (?,?,?,?,?,?,?,'PENDING_PUBLISH',?)",
        (result_id, "RES", req["request_id"], revision, status, name, payload, now()))
    return result_id


def _new_error(conn, *, inbound_name: str, request_id: str | None, code: str, detail: str) -> str:
    error_id = "err_" + secrets.token_hex(16)
    ful = request_id if request_id and protocol.FUL_ID.fullmatch(request_id) else ""
    payload = protocol.render_error(created_at=now(), error_id=error_id, inbound_name=inbound_name, ful_id=ful,
                                    code=code, detail=detail)
    conn.execute(
        "INSERT INTO outbox(file_id, kind, request_id, status, file_name, payload, publish_state, created_at)"
        " VALUES (?,?,?,?,?,?,'PENDING_PUBLISH',?)",
        (error_id, "ERR", ful or None, code, protocol.error_name(error_id), payload, now()))
    return error_id


def _equivalence_json(parsed: protocol.ParsedFile) -> str:
    return json.dumps(protocol.equivalence_key(parsed), separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------- inbound

def import_request(conn, parsed: protocol.ParsedFile, *, file_name: str, raw: bytes, ingest_key: str | None = None,
                   quarantine_name: str | None = None, sha256: str | None = None, replay: bool = False) -> ImportOutcome:
    """Record a valid request file. NEW creates the request and revision 1; an equivalent
    repeat re-publishes the latest result unchanged; a conflicting repeat is quarantined
    with an ERR file in the same transaction (on replay the caller updates the replayed row instead)."""
    if parsed.file_type != "REQ":
        raise DomainError("not a request file")
    body = parsed.body
    rid = body["fulfillmentRequestId"]
    key = _equivalence_json(parsed)
    sha = sha256 or hashlib.sha256(raw).hexdigest()
    with transaction(conn):
        existing = conn.execute("SELECT * FROM requests WHERE request_id=?", (rid,)).fetchone()
        ts = now()
        if existing is None:
            conn.execute(
                "INSERT INTO requests(request_id, order_id, reservation_id, site_id, origin, requested_at, correlation_id,"
                " traceparent, equivalence_key, state, version, raw_request, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,'PENDING',1,?,?,?)",
                (rid, body["orderId"], body["reservationId"], body["siteId"], body["origin"], body["requestedAt"],
                 body["correlationId"], body["traceparent"], key, raw, ts, ts))
            conn.executemany("INSERT INTO request_lines(request_id, line_no, sku, quantity) VALUES (?,?,?,?)",
                             [(rid, int(ln["lineNo"]), ln["sku"], int(ln["quantity"])) for ln in parsed.lines])
            req = conn.execute("SELECT * FROM requests WHERE request_id=?", (rid,)).fetchone()
            file_id = _new_result(conn, req, "PENDING", status_at=ts)
            _record_file(conn, rid, "IN", file_name, sha, "ACCEPTED", ingest_key)
            _audit(conn, request_id=rid, action="IMPORT", prev=None, new="PENDING", actor=WORKER, detail=file_name)
            return ImportOutcome("NEW", rid, file_id)
        if existing["equivalence_key"] == key:
            latest = conn.execute("SELECT file_id FROM outbox WHERE request_id=? AND kind='RES' ORDER BY revision DESC LIMIT 1",
                                  (rid,)).fetchone()
            _republish(conn, latest["file_id"])
            _record_file(conn, rid, "IN", file_name, sha, "DUPLICATE", ingest_key)
            _audit(conn, request_id=rid, action="DUPLICATE", prev=existing["state"], new=existing["state"], actor=WORKER,
                   detail=file_name)
            return ImportOutcome("DUPLICATE", rid, latest["file_id"])
        if replay:  # the caller marks the replayed quarantine row
            return ImportOutcome("CONFLICT", rid, None)
        error_id = _quarantine_row(conn, file_name=file_name, stored_name=quarantine_name or f"{sha[:16]}.{file_name}",
                                   sha256=sha, ingest_key=ingest_key or f"{file_name}:{sha}", code="CONFLICTING_REUSE",
                                   detail=f"{rid} already exists with different content", request_id=rid)
        _audit(conn, request_id=rid, action="CONFLICT", prev=existing["state"], new=existing["state"], actor=WORKER,
               detail=file_name)
        return ImportOutcome("CONFLICT", rid, error_id)


def _record_file(conn, request_id, direction, file_name, sha, outcome, ingest_key=None):
    conn.execute("INSERT INTO request_files(request_id, direction, file_name, sha256, outcome, ingest_key, at) VALUES (?,?,?,?,?,?,?)",
                 (request_id, direction, file_name, sha, outcome, ingest_key, now()))


def already_ingested(conn, ingest_key: str) -> str | None:
    """Where an upload was already recorded (after a crash or a failed move): 'archive',
    'quarantine' or None. The worker then only finishes moving the file."""
    if conn.execute("SELECT 1 FROM quarantine WHERE ingest_key=?", (ingest_key,)).fetchone():
        return "quarantine"
    if conn.execute("SELECT 1 FROM request_files WHERE ingest_key=? AND direction='IN'", (ingest_key,)).fetchone():
        return "archive"
    return None


def quarantine(conn, *, file_name: str, stored_name: str, sha256: str, code: str, detail: str,
               request_id: str | None, ingest_key: str) -> str | None:
    """Record a quarantined upload and its ERR file. Idempotent per upload (ingest_key): a crash
    before the file move re-runs this without a second record; a new upload of the same name
    and bytes is a new upload and gets its own record and ERR file (SPEC section 4)."""
    with transaction(conn):
        return _quarantine_row(conn, file_name=file_name, stored_name=stored_name, sha256=sha256, ingest_key=ingest_key,
                               code=code, detail=detail, request_id=request_id)


def _quarantine_row(conn, *, file_name, stored_name, sha256, ingest_key, code, detail, request_id) -> str | None:
    if conn.execute("SELECT 1 FROM quarantine WHERE ingest_key=?", (ingest_key,)).fetchone():
        return None
    error_id = _new_error(conn, inbound_name=file_name, request_id=request_id, code=code, detail=detail)
    conn.execute(
        "INSERT INTO quarantine(file_name, stored_name, ingest_key, sha256, error_code, detail, request_id, state, quarantined_at)"
        " VALUES (?,?,?,?,?,?,?,'QUARANTINED',?)",
        (protocol.sanitize(file_name, 255), stored_name, ingest_key, sha256, code, protocol.sanitize(detail, 200), request_id, now()))
    if request_id:
        _record_file(conn, request_id, "IN", file_name, sha256, f"QUARANTINED:{code}")
    _audit(conn, request_id=request_id, action="QUARANTINE", prev=None, new=None, actor=WORKER, reason=code, detail=file_name)
    return error_id


def request_replay(conn, quarantine_id: int, *, actor: str, label: str = "") -> None:
    """Operator asks the worker to re-read a quarantined file. Deduplication still applies."""
    label = _check_actor(actor, label)
    with transaction(conn):
        row = conn.execute("SELECT * FROM quarantine WHERE id=?", (quarantine_id,)).fetchone()
        if row is None:
            raise DomainError("no such quarantined file")
        if row["state"] != "QUARANTINED":
            raise DomainError(f"file is {row['state']}, not QUARANTINED")
        conn.execute("UPDATE quarantine SET state='REPLAY_REQUESTED' WHERE id=?", (quarantine_id,))
        _audit(conn, request_id=row["request_id"], action="REPLAY_REQUESTED", prev=None, new=None, actor=actor, label=label,
               detail=row["file_name"])


def replay_failed(conn, quarantine_id: int, code: str, detail: str) -> str:
    """The replayed file is still not acceptable (or is missing): back to QUARANTINED, with a new ERR."""
    with transaction(conn):
        row = conn.execute("SELECT * FROM quarantine WHERE id=?", (quarantine_id,)).fetchone()
        error_id = _new_error(conn, inbound_name=row["file_name"], request_id=row["request_id"], code=code, detail=detail)
        conn.execute("UPDATE quarantine SET state='QUARANTINED', error_code=?, detail=?, quarantined_at=? WHERE id=?",
                     (code, protocol.sanitize(detail, 200), now(), quarantine_id))
        _audit(conn, request_id=row["request_id"], action="REPLAY_FAILED", prev=None, new=None, actor=WORKER, reason=code,
               detail=row["file_name"])
        return error_id


def replay_done(conn, quarantine_id: int, outcome: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE quarantine SET state='REPLAYED', replayed_at=? WHERE id=? AND state='REPLAY_REQUESTED'",
                     (now(), quarantine_id))
        row = conn.execute("SELECT request_id, file_name FROM quarantine WHERE id=?", (quarantine_id,)).fetchone()
        _audit(conn, request_id=row["request_id"], action="REPLAYED", prev=None, new=None, actor=WORKER, detail=f"{row['file_name']}: {outcome}")


# ---------------------------------------------------------------- business actions

def _load(conn, request_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        raise DomainError(f"no request {request_id}")
    return row


def _transition(conn, req, action: str, expected_version: int | None, allowed_from=None) -> str:
    table = TRANSITIONS[action]
    if allowed_from is not None:
        table = {k: v for k, v in table.items() if k in allowed_from}
    if expected_version is not None and req["version"] != expected_version:
        raise StaleVersion(f"{req['request_id']} changed since it was displayed (now {req['state']}); refreshed")
    if req["state"] not in table:
        raise DomainError(f"cannot {action.lower()} a {req['state']} request")
    return table[req["state"]]


def _apply(conn, req, new_state: str, *, extra_sql: str = "", extra_args: tuple = ()) -> None:
    cur = conn.execute(
        f"UPDATE requests SET state=?, version=version+1, updated_at=?{extra_sql} WHERE request_id=? AND version=?",  # noqa: S608
        (new_state, now(), *extra_args, req["request_id"], req["version"]))
    if cur.rowcount != 1:  # cannot happen under BEGIN IMMEDIATE; kept as a guard
        raise StaleVersion("concurrent change")


def hold(conn, request_id: str, *, expected_version: int | None, actor: str, label: str = "", reason: str = "") -> int:
    label = _check_actor(actor, label)
    with transaction(conn):
        req = _load(conn, request_id)
        new = _transition(conn, req, "HOLD", expected_version)
        _apply(conn, req, new)
        ts = now()
        rev_id = _new_result(conn, req, new, status_at=ts)
        _audit(conn, request_id=request_id, action="HOLD", prev=req["state"], new=new, actor=actor, label=label,
               reason=protocol.sanitize(reason, MAX_REASON) if reason else "", detail=rev_id)
        return req["version"] + 1


def release(conn, request_id: str, *, expected_version: int | None, actor: str, label: str = "") -> int:
    label = _check_actor(actor, label)
    with transaction(conn):
        req = _load(conn, request_id)
        new = _transition(conn, req, "RELEASE", expected_version)
        _apply(conn, req, new)
        rev_id = _new_result(conn, req, new, status_at=now())
        _audit(conn, request_id=request_id, action="RELEASE", prev=req["state"], new=new, actor=actor, label=label, detail=rev_id)
        return req["version"] + 1


def ship(conn, request_id: str, *, expected_version: int | None, actor: str, label: str = "",
         require_auto: bool = False) -> str:
    """Create the one shipment for a request. Shipment, transition, audit and result intent
    commit together. require_auto is the worker's path: it re-checks the mode and refuses held
    requests inside the same transaction, so a hold or mode change that commits first wins."""
    label = _check_actor(actor, label)
    with transaction(conn):
        req = _load(conn, request_id)
        if require_auto and get_setting(conn, "mode") != "AUTO":
            raise DomainError("mode is not AUTO")
        new = _transition(conn, req, "SHIP", expected_version, AUTO_SHIP_FROM if require_auto else None)
        shipment_id = "shp_" + secrets.token_hex(16)
        ts = now()
        conn.execute("INSERT INTO shipments(shipment_id, request_id, shipped_at, actor_type, actor_label) VALUES (?,?,?,?,?)",
                     (shipment_id, request_id, ts, actor, label))
        _apply(conn, req, new, extra_sql=", shipment_id=?", extra_args=(shipment_id,))
        rev_id = _new_result(conn, req, new, status_at=ts, shipment_id=shipment_id)
        _audit(conn, request_id=request_id, action="SHIP", prev=req["state"], new=new, actor=actor, label=label,
               detail=f"{shipment_id} {rev_id}")
        return shipment_id


def reject(conn, request_id: str, *, expected_version: int | None, actor: str, code: str, reason: str,
           label: str = "") -> None:
    label = _check_actor(actor, label)
    if code not in protocol.REJECTION_CODES:
        raise DomainError(f"reason code must be one of {', '.join(protocol.REJECTION_CODES)}")
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("a rejection needs a reason")
    reason = protocol.sanitize(reason, MAX_REASON)
    with transaction(conn):
        req = _load(conn, request_id)
        new = _transition(conn, req, "REJECT", expected_version)
        _apply(conn, req, new, extra_sql=", reject_code=?, reject_text=?", extra_args=(code, reason))
        rev_id = _new_result(conn, req, new, status_at=now(), reason_code=code, reason_text=reason)
        _audit(conn, request_id=request_id, action="REJECT", prev=req["state"], new=new, actor=actor, label=label,
               reason=f"{code}: {reason}", detail=rev_id)


def _republish(conn, file_id: str) -> None:
    # last_error is kept: an ongoing publication failure stays visible until a write succeeds.
    conn.execute("UPDATE outbox SET publish_state='PENDING_PUBLISH', emit_seq=emit_seq+1 WHERE file_id=?", (file_id,))


def reemit(conn, request_id: str, *, actor: str, label: str = "") -> str:
    """Publish the latest result again: same result ID, revision and bytes. No new shipment."""
    label = _check_actor(actor, label)
    with transaction(conn):
        req = _load(conn, request_id)
        latest = conn.execute("SELECT file_id, revision FROM outbox WHERE request_id=? AND kind='RES' ORDER BY revision DESC LIMIT 1",
                              (request_id,)).fetchone()
        _republish(conn, latest["file_id"])
        _audit(conn, request_id=request_id, action="REEMIT", prev=req["state"], new=req["state"], actor=actor, label=label,
               detail=f"{latest['file_id']} revision {latest['revision']}")
        return latest["file_id"]


# ---------------------------------------------------------------- settings and demo faults

def set_mode(conn, mode: str, *, actor: str, label: str = "") -> None:
    label = _check_actor(actor, label)
    if mode not in MODES:
        raise DomainError(f"mode must be one of {MODES}")
    with transaction(conn):
        prev = get_setting(conn, "mode")
        conn.execute("UPDATE settings SET value=?, updated_at=? WHERE key='mode'", (mode, now()))
        _audit(conn, request_id=None, action="SET_MODE", prev=prev, new=mode, actor=actor, label=label)


FAULTS = {"publication_paused": ("0", "1"), "crash_after_commit": ("0", "1")}


def set_fault(conn, key: str, value: str, *, demo_enabled: bool, actor: str, label: str = "") -> None:
    """Demo faults (D-010). Refused unless demo mode is enabled in the root-owned config."""
    label = _check_actor(actor, label)
    if not demo_enabled:
        raise DomainError("demo faults are disabled in /etc/box1-erp/erp.conf")
    if key == "auto_delay_seconds":
        if not value.isdigit() or int(value) > 3600:
            raise DomainError("delay must be 0-3600 seconds")
    elif key not in FAULTS or value not in FAULTS[key]:
        raise DomainError(f"unknown fault setting {key}={value}")
    with transaction(conn):
        prev = get_setting(conn, key)
        conn.execute("UPDATE settings SET value=?, updated_at=? WHERE key=?", (value, now(), key))
        _audit(conn, request_id=None, action="FAULT", prev=f"{key}={prev}", new=f"{key}={value}", actor=actor, label=label)


def reset_faults(conn) -> list[str]:
    """With demo mode off, clear stored fault settings so re-enabling demo mode later does
    not silently re-arm an old fault. Audited when anything changes."""
    changed = []
    with transaction(conn):
        for key in ("publication_paused", "auto_delay_seconds", "crash_after_commit"):
            if get_setting(conn, key) != "0":
                changed.append(f"{key}={get_setting(conn, key)}")
                conn.execute("UPDATE settings SET value='0', updated_at=? WHERE key=?", (now(), key))
        if changed:
            _audit(conn, request_id=None, action="FAULT_RESET", prev=", ".join(changed), new="all off", actor=WORKER,
                   detail="demo faults disabled in configuration")
    return changed


def after_restore(conn, *, source: str, divergent: list[str]) -> None:
    """Restored ledgers start in MANUAL so nothing ships automatically before review."""
    with transaction(conn):
        prev = get_setting(conn, "mode")
        conn.execute("UPDATE settings SET value='MANUAL', updated_at=? WHERE key='mode'", (now(),))
        _audit(conn, request_id=None, action="RESTORE", prev=prev, new="MANUAL", actor=CONSOLE, label="restore",
               detail=protocol.sanitize(f"{source}; divergent: {', '.join(divergent) or 'none'}", 200))


def consume_crash_fault(conn) -> bool:
    """Disarm the one-shot crash in its own committed transaction. True if it was armed.
    The caller exits only after this commits, so a restart never crashes again."""
    with transaction(conn):
        if get_setting(conn, "crash_after_commit") != "1":
            return False
        conn.execute("UPDATE settings SET value='0', updated_at=? WHERE key='crash_after_commit'", (now(),))
        _audit(conn, request_id=None, action="FAULT_CRASH", prev="crash_after_commit=1", new="crash_after_commit=0",
               actor=WORKER, detail="one-shot crash after commit, before publication")
        return True


def record_failure(conn, component: str, message: str) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO failures(at, component, message) VALUES (?,?,?)",
                     (now(), component, protocol.sanitize(message, 200)))
        conn.execute("DELETE FROM failures WHERE id <= (SELECT MAX(id) - 200 FROM failures)")
