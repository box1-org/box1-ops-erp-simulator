"""Worker scenarios on real files and a real ledger (prompt verification scenarios)."""
import os
import signal
import time

import pytest
from helpers import drop, make_appliance, q, request_bytes, start_worker, wait_for

from box1_erp import db, domain, protocol
from box1_erp.worker import CRASH_EXIT_CODE, Worker


@pytest.fixture
def cfg(tmp_path):
    return make_appliance(tmp_path)


@pytest.fixture
def demo_cfg(tmp_path):
    return make_appliance(tmp_path, demo=True)


def worker(cfg):
    w = Worker(cfg)
    w.open()
    return w


def outbound(cfg):
    return sorted(p.name for p in cfg.outbound.iterdir())


def parsed_out(cfg, prefix="RES."):
    return [protocol.parse((cfg.outbound / n).read_bytes(), n) for n in outbound(cfg) if n.startswith(prefix)]


def test_manual_mode_imports_publishes_and_does_not_ship(cfg):
    w = worker(cfg)
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.cycle()
    w.cycle()
    assert [tuple(r) for r in q(cfg, "SELECT request_id, state FROM requests")] == [("ful_1", "PENDING")]
    assert q(cfg, "SELECT COUNT(*) FROM shipments")[0][0] == 0
    assert list(cfg.inbound.iterdir()) == []
    assert len(list(cfg.archive.iterdir())) == 1
    res = parsed_out(cfg)
    assert [(r.body["revision"], r.body["status"], r.body["correlationId"]) for r in res] == [("1", "PENDING", "corr-ful_1")]


def test_temporary_names_are_ignored_until_renamed(cfg):
    w = worker(cfg)
    (cfg.inbound / ".REQ.ful_1.0a1b2c3d.erp1.tmp").write_bytes(request_bytes()[:20])
    w.cycle()
    assert q(cfg, "SELECT COUNT(*) FROM requests")[0][0] == 0
    assert (cfg.inbound / ".REQ.ful_1.0a1b2c3d.erp1.tmp").exists()
    assert q(cfg, "SELECT COUNT(*) FROM quarantine")[0][0] == 0


@pytest.mark.parametrize(("name", "data", "code"), [
    ("REQ.ful_1.0a1b2c3d.erp1", b"HDR|ERP1|REQ|2026-11-05T22:00:00.000Z\nREQ|ful_1", "TRUNCATED"),
    ("REQ.ful_1.0a1b2c3d.erp1", request_bytes()[:-12], "TRUNCATED"),
    ("REQ.ful_1.0a1b2c3d.erp1", b"garbage\n", "MALFORMED_RECORD"),
    ("REQ.ful_1.0a1b2c3d.erp1", request_bytes(qty=0), "FIELD_VALUE"),
    ("notes.txt", b"hello\n", "FILENAME_MISMATCH"),
    ("REQ.ful_1.0a1b2c3d.erp1.tmp", request_bytes(), "FILENAME_MISMATCH"),
])
def test_malformed_files_are_quarantined_with_reasons_and_err_files(cfg, name, data, code):
    w = worker(cfg)
    drop(cfg, name, data)
    w.cycle()
    rows = q(cfg, "SELECT file_name, error_code, state FROM quarantine")
    assert [tuple(r) for r in rows] == [(name, code, "QUARANTINED")]
    assert list(cfg.inbound.iterdir()) == []
    assert len(list(cfg.quarantine.iterdir())) == 1
    errs = parsed_out(cfg, "ERR.")
    assert len(errs) == 1 and errs[0].body["errorCode"] == code and errs[0].body["inboundFileName"] == name
    assert q(cfg, "SELECT COUNT(*) FROM requests")[0][0] == 0


def test_repeated_and_conflicting_request_ids(cfg):
    w = worker(cfg)
    drop(cfg, "REQ.ful_1.00000001.erp1", request_bytes())
    w.cycle()
    first = outbound(cfg)
    for p in cfg.outbound.iterdir():  # the adapter consumed it
        p.unlink()
    drop(cfg, "REQ.ful_1.00000002.erp1", request_bytes())             # equivalent repeat
    drop(cfg, "REQ.ful_1.00000003.erp1", request_bytes(qty=9))        # conflicting reuse
    w.cycle()
    assert q(cfg, "SELECT COUNT(*) FROM requests")[0][0] == 1
    assert q(cfg, "SELECT quantity FROM request_lines")[0][0] == 2
    names = outbound(cfg)
    assert first[0] in names, "the equivalent repeat re-published the same result file"
    errs = parsed_out(cfg, "ERR.")
    assert [e.body["errorCode"] for e in errs] == ["CONFLICTING_REUSE"]
    assert [tuple(r) for r in q(cfg, "SELECT outcome FROM request_files WHERE direction='IN' ORDER BY id")] == [
        ("ACCEPTED",), ("DUPLICATE",), ("QUARANTINED:CONFLICTING_REUSE",)]


def test_auto_mode_ships_with_the_same_domain_path(cfg):
    w = worker(cfg)
    conn = db.connect(str(cfg.ledger))
    domain.set_mode(conn, "AUTO", actor=domain.CONSOLE, label="ana")
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.cycle()
    w.cycle()
    row = conn.execute("SELECT state, shipment_id FROM requests").fetchone()
    assert row["state"] == "SHIPPED"
    audit = conn.execute("SELECT action, actor_type FROM audit WHERE request_id='ful_1' ORDER BY id").fetchall()
    assert [tuple(a) for a in audit] == [("IMPORT", "worker"), ("SHIP", "worker")]
    shipped = [r for r in parsed_out(cfg) if r.body["status"] == "SHIPPED"]
    assert shipped[0].body["shipmentId"] == row["shipment_id"] and shipped[0].body["revision"] == "2"
    conn.close()


def test_hold_then_auto_then_release(cfg):
    w = worker(cfg)
    conn = db.connect(str(cfg.ledger))
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    drop(cfg, "REQ.ful_2.0a1b2c3d.erp1", request_bytes(ful="ful_2", order="ord_2"))
    w.cycle()
    domain.hold(conn, "ful_1", expected_version=None, actor=domain.CONSOLE)
    domain.set_mode(conn, "AUTO", actor=domain.CONSOLE)
    w.cycle()
    states = dict(conn.execute("SELECT request_id, state FROM requests").fetchall())
    assert states == {"ful_1": "HELD", "ful_2": "SHIPPED"}
    domain.release(conn, "ful_1", expected_version=None, actor=domain.CONSOLE)
    w.cycle()
    assert conn.execute("SELECT state FROM requests WHERE request_id='ful_1'").fetchone()[0] == "SHIPPED"
    revs = [(r.body["revision"], r.body["status"]) for r in parsed_out(cfg) if r.body["fulfillmentRequestId"] == "ful_1"]
    assert sorted(revs) == [("1", "PENDING"), ("2", "HELD"), ("3", "PENDING"), ("4", "SHIPPED")]
    conn.close()


def test_reemission_republishes_identical_bytes_without_a_new_shipment(cfg):
    w = worker(cfg)
    conn = db.connect(str(cfg.ledger))
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.cycle()
    domain.ship(conn, "ful_1", expected_version=None, actor=domain.CONSOLE, label="ana")
    w.cycle()
    name = next(n for n in outbound(cfg) if ".000002." in n)
    original = (cfg.outbound / name).read_bytes()
    (cfg.outbound / name).rename(cfg.consumed / (name + ".abcdef01"))  # adapter consumed it
    domain.reemit(conn, "ful_1", actor=domain.CONSOLE, label="ana")
    w.cycle()
    assert (cfg.outbound / name).read_bytes() == original
    assert conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] == 1
    assert conn.execute("SELECT emit_count FROM outbox WHERE revision=2").fetchone()[0] == 2
    conn.close()


def test_paused_publication_then_resume(demo_cfg):
    cfg = demo_cfg
    w = worker(cfg)
    conn = db.connect(str(cfg.ledger))
    domain.set_fault(conn, "publication_paused", "1", demo_enabled=True, actor=domain.CONSOLE, label="demo")
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.cycle()
    assert outbound(cfg) == []
    assert q(cfg, "SELECT activity FROM worker_status")[0][0] == "publication paused (demo)"
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE publish_state='PENDING_PUBLISH'").fetchone()[0] == 1
    domain.set_fault(conn, "publication_paused", "0", demo_enabled=True, actor=domain.CONSOLE, label="demo")
    w.cycle()
    assert len(outbound(cfg)) == 1
    conn.close()


def test_faults_are_ignored_when_demo_mode_is_off(cfg):
    w = worker(cfg)
    conn = db.connect(str(cfg.ledger))
    conn.execute("UPDATE settings SET value='1' WHERE key='publication_paused'")  # stored state without demo mode
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.cycle()
    assert len(outbound(cfg)) == 1
    conn.close()


def test_delayed_automatic_processing(demo_cfg):
    cfg = demo_cfg
    w = worker(cfg)
    conn = db.connect(str(cfg.ledger))
    domain.set_fault(conn, "auto_delay_seconds", "2", demo_enabled=True, actor=domain.CONSOLE)
    domain.set_mode(conn, "AUTO", actor=domain.CONSOLE)
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.cycle()
    w.cycle()
    assert conn.execute("SELECT state FROM requests").fetchone()[0] == "PENDING"
    assert "auto delayed 2s" in q(cfg, "SELECT activity FROM worker_status")[0][0]
    time.sleep(2.1)
    w.cycle()
    assert conn.execute("SELECT state FROM requests").fetchone()[0] == "SHIPPED"
    conn.close()


def test_quarantine_replay_keeps_deduplication(cfg):
    w = worker(cfg)
    conn = db.connect(str(cfg.ledger))
    drop(cfg, "REQ.ful_1.00000001.erp1", request_bytes())
    drop(cfg, "REQ.ful_1.00000002.erp1", request_bytes(qty=5))  # conflict, quarantined
    w.cycle()
    (qid,) = conn.execute("SELECT id FROM quarantine").fetchone()
    domain.request_replay(conn, qid, actor=domain.CONSOLE, label="ana")
    w.cycle()
    row = conn.execute("SELECT state, error_code FROM quarantine WHERE id=?", (qid,)).fetchone()
    assert tuple(row) == ("QUARANTINED", "CONFLICTING_REUSE"), "replay must not weaken deduplication"
    assert conn.execute("SELECT quantity FROM request_lines").fetchone()[0] == 2
    # A valid file quarantined earlier (for example by an older reader) is imported on replay,
    # and its stored copy moves to the archive.
    good = request_bytes(ful="ful_7", order="ord_7")
    (cfg.quarantine / "stored-good").write_bytes(good)
    domain.quarantine(conn, file_name="REQ.ful_7.0000aaaa.erp1", stored_name="stored-good", sha256="x" * 64,
                      code="FIELD_VALUE", detail="older reader", request_id="ful_7", ingest_key="k-good")
    qid2 = conn.execute("SELECT id FROM quarantine WHERE stored_name='stored-good'").fetchone()[0]
    domain.request_replay(conn, qid2, actor=domain.CONSOLE)
    w.cycle()
    assert conn.execute("SELECT state FROM quarantine WHERE id=?", (qid2,)).fetchone()[0] == "REPLAYED"
    assert conn.execute("SELECT state FROM requests WHERE request_id='ful_7'").fetchone()[0] == "PENDING"
    assert (cfg.archive / "stored-good").exists() and not (cfg.quarantine / "stored-good").exists()
    with pytest.raises(domain.DomainError):
        domain.request_replay(conn, qid2, actor=domain.CONSOLE)
    conn.close()


def test_crash_after_commit_then_restart_recovers_original_shipment(demo_cfg, tmp_path):
    cfg = demo_cfg
    conn = db.connect(str(cfg.ledger))
    domain.set_mode(conn, "AUTO", actor=domain.CONSOLE)
    domain.set_fault(conn, "crash_after_commit", "1", demo_enabled=True, actor=domain.CONSOLE, label="demo")
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    p = start_worker(str(tmp_path / "erp.conf"))
    assert p.wait(30) == CRASH_EXIT_CODE
    shp = conn.execute("SELECT shipment_id FROM requests WHERE request_id='ful_1'").fetchone()[0]
    assert shp, "the shipment committed before the crash"
    pending = conn.execute("SELECT status FROM outbox WHERE publish_state='PENDING_PUBLISH'").fetchall()
    assert [r[0] for r in pending] == ["SHIPPED"]
    assert not any(".000002." in n for n in outbound(cfg))
    p = start_worker(str(tmp_path / "erp.conf"))  # restart: no crash loop, publishes the original
    try:
        wait_for(lambda: any(".000002." in n for n in outbound(cfg)))
        assert p.poll() is None
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(10)
    shipped = [r for r in parsed_out(cfg) if r.body["status"] == "SHIPPED"]
    assert [r.body["shipmentId"] for r in shipped] == [shp]
    assert conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] == 1
    assert db.get_setting(conn, "crash_after_commit") == "0"
    actions = [r[0] for r in conn.execute("SELECT action FROM audit WHERE action LIKE 'FAULT%' ORDER BY id")]
    assert actions == ["FAULT", "FAULT_CRASH"]
    conn.close()


def test_stale_temp_files_from_a_crash_are_cleaned_and_republished(cfg):
    w = worker(cfg)
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    w.cycle()
    name = outbound(cfg)[0]
    (cfg.outbound / name).unlink()
    (cfg.outbound / ".0123456789abcdef.tmp").write_bytes(b"partial")   # our own leftover temp name
    (cfg.outbound / ".REQ.foreign.tmp").write_bytes(b"not ours")      # someone else's: left alone
    conn = db.connect(str(cfg.ledger))
    conn.execute("UPDATE outbox SET publish_state='PENDING_PUBLISH'")  # crash before the ledger mark
    conn.close()
    w.close()
    w2 = worker(cfg)
    assert sorted(p.name for p in cfg.outbound.iterdir()) == [".REQ.foreign.tmp"]
    w2.cycle()
    assert outbound(cfg) == [".REQ.foreign.tmp", name]


def test_worker_refuses_to_start_without_its_ledger(tmp_path):
    cfg = make_appliance(tmp_path)
    for suffix in ("", "-wal", "-shm"):
        p = cfg.ledger.with_name(cfg.ledger.name + suffix)
        if p.exists():
            p.unlink()
    p = start_worker(str(tmp_path / "erp.conf"))
    assert p.wait(30) == 2
    assert b"refusing to create one" in p.stderr.read()
    assert not cfg.ledger.exists()


def test_mode_and_ledger_persist_across_worker_restart(cfg, tmp_path):
    conn = db.connect(str(cfg.ledger))
    domain.set_mode(conn, "AUTO", actor=domain.CONSOLE)
    conn.close()
    for _ in range(2):
        p = start_worker(str(tmp_path / "erp.conf"))
        wait_for(lambda: q(cfg, "SELECT activity FROM worker_status")[0][0] == "running")
        p.send_signal(signal.SIGTERM)
        assert p.wait(10) == 0
    assert q(cfg, "SELECT value FROM settings WHERE key='mode'")[0][0] == "AUTO"
    assert q(cfg, "SELECT activity FROM worker_status")[0][0] == "stopped"


def test_publication_failure_is_recorded_and_retried(cfg):
    w = worker(cfg)
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", request_bytes())
    os.chmod(cfg.outbound, 0o500)
    try:
        w.cycle()
        row = q(cfg, "SELECT publish_state, last_error FROM outbox")[0]
        assert row["publish_state"] == "PENDING_PUBLISH" and row["last_error"]
        assert q(cfg, "SELECT component FROM failures")[0][0] == "publish"
        w.cycle()  # backing off: no second attempt within 5 s
        assert q(cfg, "SELECT attempts FROM outbox")[0][0] == 1
    finally:
        os.chmod(cfg.outbound, 0o750)  # noqa: S103 - restores the test directory
    w.publish_backoff.state.clear()  # as if the backoff had elapsed
    w.cycle()
    assert q(cfg, "SELECT publish_state FROM outbox")[0][0] == "PUBLISHED"
