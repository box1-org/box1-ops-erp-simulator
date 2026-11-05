"""Domain layer on a real SQLite file: identity, transitions, audit and the one-shipment rule."""
import multiprocessing
import sqlite3

import pytest

from box1_erp import db, domain, protocol


def req_file(ful="ful_1", order="ord_1", qty=2, origin="EVENT", sku="SKU-1", nonce="0a1b2c3d"):
    data = protocol.render([
        ["HDR", "ERP1", "REQ", "2026-11-05T22:00:00.000Z"],
        ["REQ", ful, order, "res_1", "site_a", origin, "2026-11-05T21:59:00.000Z", "corr-1", ""],
        ["LIN", "1", sku, str(qty)],
        ["TRL", "4"],
    ])
    name = f"REQ.{ful}.{nonce}.erp1"
    return protocol.parse(data, name), name, data


@pytest.fixture
def ledger(tmp_path):
    path = str(tmp_path / "ledger.db")
    db.initialize(path)
    return path


@pytest.fixture
def conn(ledger):
    c = db.connect(ledger)
    yield c
    c.close()


def imported(conn, **kw):
    parsed, name, data = req_file(**kw)
    return domain.import_request(conn, parsed, file_name=name, raw=data)


def results(conn, rid):
    return conn.execute("SELECT revision, status, file_id FROM outbox WHERE request_id=? AND kind='RES' ORDER BY revision",
                        (rid,)).fetchall()


def state(conn, rid):
    return conn.execute("SELECT state, version, shipment_id FROM requests WHERE request_id=?", (rid,)).fetchone()


def test_missing_ledger_is_an_error_not_a_new_database(tmp_path):
    with pytest.raises(db.MissingDatabase):
        db.connect(str(tmp_path / "absent.db"))
    assert not (tmp_path / "absent.db").exists()


def test_settings_and_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
    assert db.get_setting(conn, "mode") == "MANUAL"


def test_initialize_is_rerunnable(ledger):
    db.initialize(ledger)
    db.initialize(ledger)


def test_import_new_then_equivalent_then_conflicting(conn):
    first = imported(conn)
    assert first.outcome == "NEW"
    assert state(conn, "ful_1")["state"] == "PENDING"
    assert [r["status"] for r in results(conn, "ful_1")] == ["PENDING"]
    again = imported(conn, origin="API", nonce="ffffffff")  # same content, other origin
    assert again.outcome == "DUPLICATE" and again.file_id == first.file_id
    assert len(results(conn, "ful_1")) == 1
    conflict = imported(conn, qty=3, nonce="deadbeef")
    assert conflict.outcome == "CONFLICT" and conflict.file_id.startswith("err_")
    row = conn.execute("SELECT kind, status FROM outbox WHERE file_id=?", (conflict.file_id,)).fetchone()
    assert tuple(row) == ("ERR", "CONFLICTING_REUSE")
    assert state(conn, "ful_1")["state"] == "PENDING"
    lines = conn.execute("SELECT quantity FROM request_lines WHERE request_id='ful_1'").fetchall()
    assert [r[0] for r in lines] == [2]


def test_hold_release_ship_with_revisions_and_audit(conn):
    imported(conn)
    v = state(conn, "ful_1")["version"]
    v = domain.hold(conn, "ful_1", expected_version=v, actor=domain.CONSOLE, label="ana")
    v = domain.release(conn, "ful_1", expected_version=v, actor=domain.CONSOLE, label="ana")
    shp = domain.ship(conn, "ful_1", expected_version=v, actor=domain.CONSOLE, label="ana")
    assert shp.startswith("shp_")
    assert [(r["revision"], r["status"]) for r in results(conn, "ful_1")] == [
        (1, "PENDING"), (2, "HELD"), (3, "PENDING"), (4, "SHIPPED")]
    audit = conn.execute("SELECT action, prev_state, new_state, actor_type, actor_label FROM audit WHERE request_id='ful_1' ORDER BY id").fetchall()
    assert [tuple(a) for a in audit] == [
        ("IMPORT", None, "PENDING", "worker", ""), ("HOLD", "PENDING", "HELD", "console", "ana"),
        ("RELEASE", "HELD", "PENDING", "console", "ana"), ("SHIP", "PENDING", "SHIPPED", "console", "ana")]
    payload = conn.execute("SELECT payload FROM outbox WHERE request_id='ful_1' AND revision=4").fetchone()[0]
    rid = conn.execute("SELECT file_id, file_name FROM outbox WHERE request_id='ful_1' AND revision=4").fetchone()
    parsed = protocol.parse(payload, rid["file_name"])
    assert parsed.body["shipmentId"] == shp and parsed.body["correlationId"] == "corr-1"


def test_terminal_states_refuse_everything(conn):
    imported(conn)
    domain.ship(conn, "ful_1", expected_version=None, actor=domain.CONSOLE)
    for action in (lambda: domain.hold(conn, "ful_1", expected_version=None, actor=domain.CONSOLE),
                   lambda: domain.release(conn, "ful_1", expected_version=None, actor=domain.CONSOLE),
                   lambda: domain.ship(conn, "ful_1", expected_version=None, actor=domain.CONSOLE),
                   lambda: domain.reject(conn, "ful_1", expected_version=None, actor=domain.CONSOLE, code="OTHER", reason="x")):
        with pytest.raises(domain.DomainError):
            action()
    assert conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] == 1


def test_reject_requires_reason_and_known_code(conn):
    imported(conn)
    with pytest.raises(domain.DomainError, match="reason"):
        domain.reject(conn, "ful_1", expected_version=None, actor=domain.CONSOLE, code="OTHER", reason="  ")
    with pytest.raises(domain.DomainError, match="reason code"):
        domain.reject(conn, "ful_1", expected_version=None, actor=domain.CONSOLE, code="LOST", reason="gone")
    assert state(conn, "ful_1")["state"] == "PENDING"
    domain.reject(conn, "ful_1", expected_version=None, actor=domain.CONSOLE, code="DAMAGED_GOODS", reason="crushed | wet")
    assert state(conn, "ful_1")["state"] == "REJECTED"
    last = results(conn, "ful_1")[-1]
    assert last["status"] == "REJECTED"


def test_stale_console_action_is_refused(conn):
    imported(conn)
    seen = state(conn, "ful_1")["version"]
    domain.hold(conn, "ful_1", expected_version=seen, actor=domain.CONSOLE)
    with pytest.raises(domain.StaleVersion):
        domain.ship(conn, "ful_1", expected_version=seen, actor=domain.CONSOLE)
    assert state(conn, "ful_1")["state"] == "HELD"


def test_reemit_keeps_identity_and_creates_no_shipment(conn):
    imported(conn)
    domain.ship(conn, "ful_1", expected_version=None, actor=domain.CONSOLE)
    before = conn.execute("SELECT file_id, payload, emit_seq FROM outbox WHERE request_id='ful_1' AND revision=2").fetchone()
    conn.execute("UPDATE outbox SET publish_state='PUBLISHED'")
    fid = domain.reemit(conn, "ful_1", actor=domain.CONSOLE, label="ana")
    after = conn.execute("SELECT file_id, payload, emit_seq, publish_state FROM outbox WHERE request_id='ful_1' AND revision=2").fetchone()
    assert fid == before["file_id"] == after["file_id"]
    assert after["payload"] == before["payload"]
    assert after["emit_seq"] == before["emit_seq"] + 1 and after["publish_state"] == "PENDING_PUBLISH"
    assert len(results(conn, "ful_1")) == 2
    assert conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] == 1


def test_auto_ship_respects_mode_and_holds(conn):
    imported(conn)
    with pytest.raises(domain.DomainError, match="not AUTO"):
        domain.ship(conn, "ful_1", expected_version=None, actor=domain.WORKER, require_auto=True)
    domain.set_mode(conn, "AUTO", actor=domain.CONSOLE, label="ana")
    domain.hold(conn, "ful_1", expected_version=None, actor=domain.CONSOLE)
    with pytest.raises(domain.DomainError, match="HELD"):
        domain.ship(conn, "ful_1", expected_version=None, actor=domain.WORKER, require_auto=True)
    # an operator may still ship a held request by hand
    domain.ship(conn, "ful_1", expected_version=None, actor=domain.CONSOLE)


def test_mode_persists_across_connections(ledger):
    c = db.connect(ledger)
    domain.set_mode(c, "AUTO", actor=domain.CONSOLE)
    c.close()
    c = db.connect(ledger)
    assert db.get_setting(c, "mode") == "AUTO"
    c.close()


def test_database_backstop_rejects_a_second_shipment(conn):
    imported(conn)
    domain.ship(conn, "ful_1", expected_version=None, actor=domain.CONSOLE)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO shipments(shipment_id, request_id, shipped_at, actor_type) VALUES ('shp_x','ful_1','t','worker')")


def test_faults_need_demo_mode_and_crash_is_one_shot(conn):
    with pytest.raises(domain.DomainError, match="disabled"):
        domain.set_fault(conn, "crash_after_commit", "1", demo_enabled=False, actor=domain.CONSOLE)
    domain.set_fault(conn, "crash_after_commit", "1", demo_enabled=True, actor=domain.CONSOLE, label="demo")
    assert domain.consume_crash_fault(conn) is True
    assert domain.consume_crash_fault(conn) is False
    actions = [r[0] for r in conn.execute("SELECT action FROM audit ORDER BY id")]
    assert actions == ["FAULT", "FAULT_CRASH"]


def _racer(path, actor, start, out):
    conn = db.connect(path)
    start.wait()
    try:
        if actor == "worker":
            out.put(("worker", domain.ship(conn, "ful_1", expected_version=None, actor=domain.WORKER, require_auto=True)))
        else:
            out.put(("console", domain.ship(conn, "ful_1", expected_version=1, actor=domain.CONSOLE, label="ana")))
    except domain.DomainError as exc:
        out.put((actor, f"refused: {exc}"))
    finally:
        conn.close()


@pytest.mark.parametrize("round_", range(20))
def test_manual_ship_racing_automatic_ship_yields_one_shipment(ledger, round_):
    c = db.connect(ledger)
    imported(c)
    domain.set_mode(c, "AUTO", actor=domain.CONSOLE)
    c.close()
    ctx = multiprocessing.get_context("spawn")
    start, out = ctx.Event(), ctx.Queue()
    procs = [ctx.Process(target=_racer, args=(ledger, a, start, out)) for a in ("worker", "console")]
    for p in procs:
        p.start()
    start.set()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    outcomes = dict(out.get(timeout=5) for _ in procs)
    shipped = [v for v in outcomes.values() if v.startswith("shp_")]
    assert len(shipped) == 1, outcomes
    c = db.connect(ledger)
    assert c.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] == 1
    assert c.execute("SELECT shipment_id FROM requests").fetchone()[0] == shipped[0]
    assert c.execute("SELECT COUNT(*) FROM outbox WHERE status='SHIPPED'").fetchone()[0] == 1
    c.close()
