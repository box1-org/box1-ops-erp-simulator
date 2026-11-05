"""Application-consistent backup and restore, with record checks."""
import signal
import sqlite3

import pytest

from box1_erp import backup, db, domain
from helpers import drop, make_appliance, q, request_bytes, start_worker, wait_for


def test_backup_while_running_then_restore_with_record_checks(tmp_path):
    cfg = make_appliance(tmp_path)
    path = str(tmp_path / "erp.conf")
    w = start_worker(path)
    try:
        for i in range(5):
            drop(cfg, f"REQ.ful_{i}.0a1b2c3d.erp1", request_bytes(ful=f"ful_{i}", order=f"ord_{i}"))
        wait_for(lambda: q(cfg, "SELECT COUNT(*) FROM outbox WHERE publish_state='PUBLISHED'")[0][0] == 5)
        conn = db.connect(str(cfg.ledger))
        shp = domain.ship(conn, "ful_1", expected_version=None, actor=domain.CONSOLE, label="ana")
        domain.reject(conn, "ful_2", expected_version=None, actor=domain.CONSOLE, code="OTHER", reason="test")
        domain.set_mode(conn, "AUTO", actor=domain.CONSOLE)
        domain.set_mode(conn, "MANUAL", actor=domain.CONSOLE)
        conn.close()
        target = backup.backup(cfg, "b1")  # online, while the worker writes heartbeats
        manifest = backup.verify(target)
        assert manifest["counts"]["requests"] == 5 and manifest["counts"]["shipments"] == 1
        with pytest.raises(RuntimeError, match="worker is running"):
            backup.restore(cfg, target)
    finally:
        w.send_signal(signal.SIGTERM)
        w.wait(10)
    before = {tuple(r) for r in q(cfg, "SELECT request_id, state, shipment_id, version FROM requests")}
    audit_before = q(cfg, "SELECT COUNT(*) FROM audit")[0][0]
    # damage: lose the ledger and the archive
    for suffix in ("", "-wal", "-shm"):
        p = cfg.ledger.with_name(cfg.ledger.name + suffix)
        if p.exists():
            p.unlink()
    for p in cfg.archive.iterdir():
        p.unlink()
    backup.restore(cfg, target)
    after = {tuple(r) for r in q(cfg, "SELECT request_id, state, shipment_id, version FROM requests")}
    assert after == before
    assert ("ful_1", "SHIPPED", shp, 2) in after
    assert q(cfg, "SELECT COUNT(*) FROM audit")[0][0] == audit_before
    assert q(cfg, "SELECT value FROM settings WHERE key='mode'")[0][0] == "MANUAL"
    assert len(list(cfg.archive.iterdir())) == 5
    conn = sqlite3.connect(cfg.ledger)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_tampered_backup_is_refused(tmp_path):
    cfg = make_appliance(tmp_path)
    target = backup.backup(cfg, "b2")
    with open(target / "ledger.db", "r+b") as fh:
        fh.seek(200)
        fh.write(b"\x00\xff")
    with pytest.raises(RuntimeError, match="digest"):
        backup.verify(target)
