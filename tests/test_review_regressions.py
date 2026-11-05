"""Regression tests for the Phase 5 reviewer findings (IDs refer to box1-docs run state review-findings/)."""
import os
import sqlite3
import subprocess
import sys
import tracemalloc

import pytest
from helpers import drop, env_for, make_appliance, q, request_bytes

from box1_erp import db, domain, fileops, protocol
from box1_erp.worker import Worker


@pytest.fixture
def cfg(tmp_path):
    return make_appliance(tmp_path)


def worker(cfg):
    w = Worker(cfg)
    w.open()
    return w


def errs(cfg):
    out = []
    for p in sorted(cfg.outbound.iterdir()):
        if p.name.startswith("ERR.") and p.is_file():
            out.append(protocol.parse(p.read_bytes(), p.name).body)
    return out


def ledger_ok(cfg):
    conn = sqlite3.connect(cfg.ledger)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_S1_planted_symlinks_in_outbound_never_reach_the_ledger(cfg):
    w = worker(cfg)
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.cycle()
    name = next(p.name for p in cfg.outbound.iterdir())
    conn = db.connect(str(cfg.ledger))
    domain.reemit(conn, "ful_1", actor=domain.CONSOLE)
    conn.close()
    # the SFTP account replaces the published file by a link to the ledger, and plants links at old-style temp names
    (cfg.outbound / name).unlink()
    os.symlink(cfg.ledger, cfg.outbound / name)
    os.symlink(cfg.ledger, cfg.outbound / f".{name}.tmp")
    before = cfg.ledger.read_bytes()[:100]
    w.cycle()
    assert ledger_ok(cfg) and cfg.ledger.read_bytes()[:100] == before
    assert not (cfg.outbound / name).is_symlink(), "the rename replaced the link itself"
    assert protocol.parse((cfg.outbound / name).read_bytes(), name).body["status"] == "PENDING"


def test_S5_M8_a_directory_at_one_result_name_does_not_stop_other_results(cfg):
    w = worker(cfg)
    conn = db.connect(str(cfg.ledger))
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.ingest()
    blocked = conn.execute("SELECT file_name FROM outbox").fetchone()[0]
    (cfg.outbound / blocked).mkdir()
    drop(cfg, "REQ.ful_2.0a1b2c3d.erp1", request_bytes(ful="ful_2", order="ord_2"))
    w.cycle()
    rows = dict(conn.execute("SELECT request_id, publish_state FROM outbox").fetchall())
    assert rows == {"ful_1": "PENDING_PUBLISH", "ful_2": "PUBLISHED"}
    assert "is a directory" in conn.execute("SELECT last_error FROM outbox WHERE request_id='ful_1'").fetchone()[0]
    conn.close()


def test_S2_M5_oversized_upload_uses_bounded_memory_and_stores_a_prefix(cfg):
    w = worker(cfg)
    big = cfg.inbound / "REQ.ful_1.0a1b2c3d.erp1"
    with open(big, "wb") as fh:
        fh.write(b"HDR|ERP1|REQ|" + b"x" * (40 << 20))
    tracemalloc.start()
    w.cycle()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 4 << 20, f"peak {peak} bytes for a 40 MiB upload"
    assert [e["errorCode"] for e in errs(cfg)] == ["FILE_TOO_LARGE"]
    stored = list(cfg.quarantine.iterdir())
    assert len(stored) == 1 and stored[0].stat().st_size == protocol.MAX_FILE_BYTES + 1
    assert not big.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode 000 files")
def test_M1_an_unreadable_upload_is_quarantined_and_does_not_block_later_files(cfg):
    w = worker(cfg)
    drop(cfg, "REQ.ful_a.0a1b2c3d.erp1", request_bytes(ful="ful_a", order="ord_a"))
    os.chmod(cfg.inbound / "REQ.ful_a.0a1b2c3d.erp1", 0)
    drop(cfg, "REQ.ful_b.0a1b2c3d.erp1", request_bytes(ful="ful_b", order="ord_b"))
    w.cycle()
    assert [r[0] for r in q(cfg, "SELECT request_id FROM requests")] == ["ful_b"]
    assert [(e["errorCode"], e["fulfillmentRequestId"]) for e in errs(cfg)] == [("UNREADABLE", "ful_a")]
    assert list(cfg.inbound.iterdir()) == []


def test_M7_S4_links_and_directories_in_inbound_move_unopened_with_an_err(cfg):
    w = worker(cfg)
    os.symlink(cfg.ledger, cfg.inbound / "REQ.ful_1.0a1b2c3d.erp1")
    (cfg.inbound / "REQ.ful_2.0a1b2c3d.erp1").mkdir()
    before = cfg.ledger.read_bytes()[:100]
    w.cycle()
    assert ledger_ok(cfg) and cfg.ledger.read_bytes()[:100] == before
    assert sorted(e["errorCode"] for e in errs(cfg)) == ["UNREADABLE", "UNREADABLE"]
    assert list(cfg.inbound.iterdir()) == []
    kinds = sorted("link" if p.is_symlink() else "dir" if p.is_dir() else "file" for p in cfg.quarantine.iterdir())
    assert kinds == ["dir", "link"]
    # replay of an unopened link refuses to follow it
    qid = q(cfg, "SELECT id FROM quarantine WHERE stored_name LIKE 'unread.%' ORDER BY id")[0][0]
    conn = db.connect(str(cfg.ledger))
    domain.request_replay(conn, qid, actor=domain.CONSOLE)
    conn.close()
    w.cycle()
    assert tuple(q(cfg, "SELECT state, error_code FROM quarantine WHERE id=?", qid)[0]) == ("QUARANTINED", "UNREADABLE")
    assert ledger_ok(cfg)


def test_S3_archived_copy_is_a_new_inode_the_sftp_account_cannot_change(cfg):
    w = worker(cfg)
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    os.link(cfg.inbound / "REQ.ful_1.0a1b2c3d.erp1", cfg.consumed / "kept-hardlink")
    w.cycle()
    archived = next(cfg.archive.iterdir())
    assert archived.stat().st_ino != (cfg.consumed / "kept-hardlink").stat().st_ino
    with open(cfg.consumed / "kept-hardlink", "r+b") as fh:
        fh.write(b"XXX")
    assert archived.read_bytes() == request_bytes()


def test_M2_a_failing_move_after_commit_does_not_republish_every_cycle(cfg):
    w = worker(cfg)
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.cycle()
    for p in cfg.outbound.iterdir():
        p.unlink()
    drop(cfg, "REQ.ful_1.00000002.erp1", request_bytes())  # equivalent repeat
    real = fileops.store_copy
    fileops.store_copy = lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device"))
    try:
        for _ in range(4):
            w.inbound_backoff.state.clear()
            w.cycle()
    finally:
        fileops.store_copy = real
    assert q(cfg, "SELECT emit_count FROM outbox WHERE revision=1")[0][0] == 2, "republished once, not every cycle"
    assert q(cfg, "SELECT COUNT(*) FROM audit WHERE action='DUPLICATE'")[0][0] == 1
    w.inbound_backoff.state.clear()
    w.cycle()
    assert list(cfg.inbound.iterdir()) == [] and len(list(cfg.archive.iterdir())) == 2


def test_A1_M3_P3_quarantine_name_survives_a_crash_before_the_move(cfg, tmp_path):
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", b"broken\n")
    code = (
        "import sys; sys.path[:0]=[{!r}]\n"
        "from box1_erp import config, fileops\nfrom box1_erp.worker import Worker\n"
        "import os\nfileops.store_copy = lambda *a, **k: os._exit(9)\n"
        "w = Worker(config.load({!r})); w.open(); w.cycle()\n"
    ).format(os.path.join(os.path.dirname(__file__), "..", "src"), str(tmp_path / "erp.conf"))
    p = subprocess.run([sys.executable, "-c", code], env=env_for(str(tmp_path / "erp.conf")), capture_output=True, timeout=30)
    assert p.returncode == 9, p.stderr
    assert q(cfg, "SELECT COUNT(*) FROM quarantine")[0][0] == 1  # committed before the crash
    import time
    time.sleep(1.1)  # a clock-based name would now differ
    w = worker(cfg)
    w.cycle()
    stored = q(cfg, "SELECT stored_name FROM quarantine")[0][0]
    assert (cfg.quarantine / stored).exists()
    assert len(errs(cfg)) == 1, "no second ERR for the same upload"


def test_M4_replay_of_changed_bytes_updates_the_same_row(cfg):
    w = worker(cfg)
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", b"broken\n")
    w.cycle()
    qid, stored = q(cfg, "SELECT id, stored_name FROM quarantine")[0]
    (cfg.quarantine / stored).write_bytes(b"still broken but different\n")
    conn = db.connect(str(cfg.ledger))
    domain.request_replay(conn, qid, actor=domain.CONSOLE)
    conn.close()
    w.cycle()
    w.cycle()
    assert [tuple(r) for r in q(cfg, "SELECT id, state FROM quarantine")] == [(qid, "QUARANTINED")]
    assert len(errs(cfg)) == 2  # the original and one for the failed replay


def test_M6_P8_every_upload_of_the_same_bad_file_gets_its_own_err(cfg):
    w = worker(cfg)
    for _ in range(2):
        drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", b"broken\n")
        w.cycle()
    assert q(cfg, "SELECT COUNT(*) FROM quarantine")[0][0] == 2
    assert len(errs(cfg)) == 2


def test_A7_crash_fault_is_not_consumed_by_re_emitting_an_old_shipment(tmp_path):
    cfg = make_appliance(tmp_path, demo=True)
    w = worker(cfg)
    conn = db.connect(str(cfg.ledger))
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.cycle()
    domain.ship(conn, "ful_1", expected_version=None, actor=domain.CONSOLE)
    w.cycle()
    domain.set_fault(conn, "crash_after_commit", "1", demo_enabled=True, actor=domain.CONSOLE)
    domain.reemit(conn, "ful_1", actor=domain.CONSOLE)
    w.cycle()  # would os._exit if the re-emission consumed the fault
    assert db.get_setting(conn, "crash_after_commit") == "1"
    conn.close()


def test_S8_stored_faults_are_cleared_when_demo_mode_is_off(tmp_path):
    demo = make_appliance(tmp_path, demo=True)
    conn = db.connect(str(demo.ledger))
    domain.set_fault(conn, "crash_after_commit", "1", demo_enabled=True, actor=domain.CONSOLE)
    conn.close()
    conf = tmp_path / "erp.conf"
    conf.write_text(conf.read_text().replace("faults = true", "faults = false"))
    from box1_erp import config
    w = worker(config.load(str(conf)))
    assert db.get_setting(w.conn, "crash_after_commit") == "0"
    assert q(demo, "SELECT action FROM audit ORDER BY id DESC LIMIT 1")[0][0] == "FAULT_RESET"


def test_P4_a_failed_commit_releases_the_write_lock(cfg):
    conn = db.connect(str(cfg.ledger))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction(conn):
            conn.execute("PRAGMA defer_foreign_keys=ON")
            conn.execute("INSERT INTO request_lines(request_id, line_no, sku, quantity) VALUES ('ful_none', 1, 'X', 1)")
    assert not conn.in_transaction
    other = db.connect(str(cfg.ledger))
    with db.transaction(other):  # would hit "database is locked" if the failed commit kept the lock
        other.execute("UPDATE settings SET value='MANUAL' WHERE key='mode'")
    other.close()
    with db.transaction(conn):
        pass
    conn.close()


def test_O6_P7_schema_version_is_checked(cfg):
    conn = sqlite3.connect(cfg.ledger)
    conn.execute("PRAGMA user_version=99")
    conn.close()
    with pytest.raises(db.SchemaMismatch, match="newer"):
        db.connect(str(cfg.ledger))
    conn = sqlite3.connect(cfg.ledger)
    conn.execute("PRAGMA user_version=0")
    conn.close()
    with pytest.raises(db.SchemaMismatch, match="migrate"):
        db.connect(str(cfg.ledger))
    db.initialize(str(cfg.ledger))  # what `box1-erp migrate` runs
    db.connect(str(cfg.ledger)).close()


def test_O4_status_check_exit_codes_and_ages(cfg, tmp_path):
    w = worker(cfg)
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.cycle()
    run = lambda: subprocess.run([sys.executable, "-m", "box1_erp.cli", "status", "--check"],  # noqa: E731
                                 env=env_for(str(tmp_path / "erp.conf")), capture_output=True, text=True, timeout=30)
    p = run()
    assert p.returncode == 0, p.stdout
    import json
    s = json.loads(p.stdout)
    assert s["oldest_pending_age_s"] is not None and s["oldest_unpublished_age_s"] is None
    drop(cfg, "notes.txt", b"x\n")
    w.cycle()
    assert run().returncode == 1  # quarantine needs attention
    w.close()
    conn = db.connect(str(cfg.ledger))
    conn.execute("UPDATE worker_status SET activity='stopped'")
    conn.close()
    assert run().returncode == 2


def test_worker_refuses_to_start_while_a_restore_holds_the_lock(cfg):
    lock = db.LedgerLock(str(cfg.ledger), exclusive=True)
    assert lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="locked"):
            Worker(cfg).open()
    finally:
        lock.release()
