"""Application-consistent backup and restore, with record checks and restore safety."""
import signal
import sqlite3

import pytest
from helpers import drop, make_appliance, q, request_bytes, start_worker, wait_for

from box1_erp import backup, db, domain


def _requests(cfg):
    return {tuple(r) for r in q(cfg, "SELECT request_id, state, shipment_id, version FROM requests")}


def _seed_running(tmp_path):
    cfg = make_appliance(tmp_path)
    w = start_worker(str(tmp_path / "erp.conf"))
    for i in range(5):
        drop(cfg, f"REQ.ful_{i}.0a1b2c3d.erp1", request_bytes(ful=f"ful_{i}", order=f"ord_{i}"))
    wait_for(lambda: q(cfg, "SELECT COUNT(*) FROM outbox WHERE publish_state='PUBLISHED'")[0][0] == 5)
    return cfg, w


def _stop(w):
    w.send_signal(signal.SIGTERM)
    w.wait(10)


def test_backup_while_running_then_restore_with_record_checks(tmp_path):
    cfg, w = _seed_running(tmp_path)
    try:
        conn = db.connect(str(cfg.ledger))
        shp = domain.ship(conn, "ful_1", expected_version=None, actor=domain.CONSOLE, label="ana")
        domain.reject(conn, "ful_2", expected_version=None, actor=domain.CONSOLE, code="OTHER", reason="test")
        domain.set_mode(conn, "AUTO", actor=domain.CONSOLE)
        conn.close()
        target = backup.backup(cfg, "b1")  # online, while the worker writes
        manifest = backup.verify(target)
        assert manifest["counts"]["requests"] == 5 and manifest["counts"]["shipments"] == 1
        assert manifest["spool_files"]["archive"] == 5
        assert not list(cfg.backups.glob(".*partial"))
        with pytest.raises(backup.RestoreRefused, match="ledger open"):
            backup.restore(cfg, target)
    finally:
        _stop(w)
    before = _requests(cfg)
    audit_before = q(cfg, "SELECT COUNT(*) FROM audit")[0][0]
    result = backup.restore(cfg, target)
    assert result["divergent"] == [] and result["mode"] == "MANUAL"
    assert _requests(cfg) == before and ("ful_1", "SHIPPED", shp, 2) in before
    assert q(cfg, "SELECT COUNT(*) FROM audit")[0][0] == audit_before + 1  # the RESTORE audit row
    assert q(cfg, "SELECT value FROM settings WHERE key='mode'")[0][0] == "MANUAL", "restored ledgers start in MANUAL"
    assert len(list(cfg.archive.iterdir())) == 5
    assert list(cfg.archive.parent.glob("archive.pre-restore-*")), "old archive kept aside"
    assert list(cfg.state_dir.glob("ledger.pre-restore-*.db")), "old ledger kept aside"
    conn = sqlite3.connect(cfg.ledger)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_restore_refused_while_a_console_has_the_ledger_open(tmp_path):
    cfg = make_appliance(tmp_path)
    target = backup.backup(cfg, "b")
    console = db.LedgerLock(str(cfg.ledger))  # what a running console holds
    assert console.acquire()
    try:
        with pytest.raises(backup.RestoreRefused, match="ledger open"):
            backup.restore(cfg, target)
    finally:
        console.release()
    backup.restore(cfg, target)


def test_restore_to_an_older_point_is_refused_unless_accepted(tmp_path):
    cfg, w = _seed_running(tmp_path)
    try:
        target = backup.backup(cfg, "older")
        conn = db.connect(str(cfg.ledger))
        shp = domain.ship(conn, "ful_3", expected_version=None, actor=domain.CONSOLE)  # after the backup
        conn.close()
        wait_for(lambda: q(cfg, "SELECT COUNT(*) FROM outbox WHERE publish_state='PUBLISHED'")[0][0] == 6)
    finally:
        _stop(w)
    with pytest.raises(backup.RestoreRefused, match="ful_3"):
        backup.restore(cfg, target)
    assert ("ful_3", "SHIPPED", shp, 2) in _requests(cfg), "a refused restore changes nothing"
    result = backup.restore(cfg, target, accept_divergence=True)
    assert result["divergent"] == ["ful_3"]
    old = sqlite3.connect(result["previous_ledger"])
    assert old.execute("SELECT shipment_id FROM requests WHERE request_id='ful_3'").fetchone()[0] == shp
    old.close()
    audit = q(cfg, "SELECT detail FROM audit WHERE action='RESTORE'")[0][0]
    assert "ful_3" in audit


def test_tampered_backup_is_refused(tmp_path):
    cfg = make_appliance(tmp_path)
    target = backup.backup(cfg, "b2")
    with open(target / "ledger.db", "r+b") as fh:
        fh.seek(200)
        fh.write(b"\x00\xff")
    with pytest.raises(RuntimeError, match="digest"):
        backup.verify(target)
    with pytest.raises(RuntimeError, match="digest"):
        backup.restore(cfg, target)
