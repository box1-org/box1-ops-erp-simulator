"""Operational status read from the ledger, shared by the TUI and `box1-erp status`."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from . import db


@dataclass
class Status:
    mode: str
    worker: str               # RUNNING, STOPPED or UNRESPONSIVE
    heartbeat_age: float | None
    activity: str
    pending: int
    held: int
    quarantined: int
    pending_output: int
    publication_failures: int
    demo_faults: dict
    conditions: list[str] = field(default_factory=list)
    recent_failures: list[tuple[str, str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def snapshot(conn, cfg) -> Status:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    w = conn.execute("SELECT heartbeat_at, activity FROM worker_status WHERE id=1").fetchone()
    age = time.time() - db.parse_ts(w["heartbeat_at"]) if w and w["heartbeat_at"] else None
    activity = (w["activity"] if w else None) or "never started"
    if age is None or activity == "stopped":
        worker = "STOPPED"
    elif age > cfg.heartbeat_stale_seconds:
        worker = "UNRESPONSIVE"
    else:
        worker = "RUNNING"
    faults = {}
    if cfg.demo_faults:
        faults = {k: db.get_setting(conn, k) for k in ("publication_paused", "auto_delay_seconds", "crash_after_commit")}
    s = Status(
        mode=db.get_setting(conn, "mode"), worker=worker, heartbeat_age=age, activity=activity,
        pending=q("SELECT COUNT(*) FROM requests WHERE state='PENDING'"),
        held=q("SELECT COUNT(*) FROM requests WHERE state='HELD'"),
        quarantined=q("SELECT COUNT(*) FROM quarantine WHERE state<>'REPLAYED'"),
        pending_output=q("SELECT COUNT(*) FROM outbox WHERE publish_state='PENDING_PUBLISH'"),
        publication_failures=q("SELECT COUNT(*) FROM outbox WHERE publish_state='PENDING_PUBLISH' AND last_error IS NOT NULL"),
        demo_faults=faults,
        recent_failures=[tuple(r) for r in conn.execute("SELECT at, component, message FROM failures ORDER BY id DESC LIMIT 5")],
    )
    if worker != "RUNNING":
        s.conditions.append(f"WORKER {worker}: imports and publication stopped")
    if s.publication_failures:
        s.conditions.append("PUBLICATION FAILURE")
    if faults.get("publication_paused") == "1":
        s.conditions.append("PUBLICATION PAUSED (demo)")
    if faults.get("auto_delay_seconds", "0") != "0":
        s.conditions.append(f"AUTO DELAYED {faults['auto_delay_seconds']}s (demo)")
    if faults.get("crash_after_commit") == "1":
        s.conditions.append("CRASH ARMED (demo)")
    if s.mode == "MANUAL" and s.pending and worker == "RUNNING":
        s.conditions.append("WAITING FOR OPERATOR")
    if not s.conditions:
        s.conditions.append("OK")
    return s
