"""The console under a real pseudo-terminal, rendered with pyte, at 80x24 and after resizes."""
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time

import pyte
import pytest
from helpers import drop, env_for, make_appliance, q, request_bytes, start_worker, wait_for

from box1_erp import db, domain


class Console:
    def __init__(self, cfg_path, cols=80, rows=24, term="vt100"):
        self.master, slave = pty.openpty()
        self.resize(cols, rows, signal_child=False)
        env = dict(env_for(cfg_path), TERM=term, LANG="C", LC_ALL="C")
        self.proc = subprocess.Popen([sys.executable, "-m", "box1_erp.cli", "tui", "--label", "ana"], stdin=slave, stdout=slave,
                                     stderr=subprocess.PIPE, env=env, start_new_session=True)
        os.close(slave)
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)

    def resize(self, cols, rows, signal_child=True):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        if signal_child:
            self.screen.resize(rows, cols)
            os.killpg(self.proc.pid, signal.SIGWINCH)

    def pump(self, seconds=0.3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    self.stream.feed(os.read(self.master, 65536))
                except OSError:
                    return

    def text(self):
        return "\n".join(self.screen.display)

    def wait_text(self, needle, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.2)
            if needle in self.text():
                return self.text()
        raise AssertionError(f"{needle!r} not on screen:\n{self.text()}")

    def keys(self, data: bytes, settle=0.4):
        os.write(self.master, data)
        self.pump(settle)

    def close(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        os.close(self.master)


@pytest.fixture
def appliance(tmp_path):
    cfg = make_appliance(tmp_path, demo=True)
    return cfg, str(tmp_path / "erp.conf")


def seed(cfg, n=3):
    from box1_erp.worker import Worker
    w = Worker(cfg)
    w.open()
    for i in range(1, n + 1):
        drop(cfg, f"REQ.ful_{i}.0a1b2c3d.erp1", request_bytes(ful=f"ful_{i}", order=f"ord_{i}", sku="BØLT"))
    w.cycle()
    w.conn.close()


def test_renders_at_80x24_ascii_only_and_ships_with_confirmation(appliance):
    cfg, path = appliance
    seed(cfg)
    c = Console(path)
    try:
        screen = c.wait_text("BOX1 LEGACY ERP")
        assert "MODE: MANUAL" in screen and "Pending 3" in screen and "Op: ana" in screen
        assert all(len(line) == 80 for line in c.screen.display)
        assert all(ch.isascii() for ch in c.text())
        assert c.screen.display[4].rstrip().endswith("UPDATED  OUT"), "every column fits in 80"
        assert c.screen.display[5].rstrip().endswith("OK"), c.screen.display[5]
        header = c.screen.display[4]
        assert header.split() == ["REQUEST", "ORDER", "STATE", "SHIPMENT", "SITE", "O", "LN", "UPDATED", "OUT"]
        row = c.screen.display[5].split()
        assert row[5:7] == ["E", "1"], "origin EVENT and one line item"
        assert "Enter=Detail" in c.screen.display[23]
        c.keys(b"\r")
        detail = c.wait_text("== Lines")
        assert "B\\u00d8LT" in detail, "non-ASCII shown as an escape"
        c.keys(b"s")
        c.wait_text("Type Y to confirm")
        c.keys(b"\x1b")
        c.wait_text("Ship cancelled")
        assert q(cfg, "SELECT COUNT(*) FROM shipments")[0][0] == 0
        c.keys(b"s")
        c.wait_text("Type Y to confirm")
        c.keys(b"Y\r")
        c.wait_text("Shipped ")
        assert q(cfg, "SELECT COUNT(*) FROM shipments")[0][0] == 1
        label = q(cfg, "SELECT actor_type, actor_label FROM audit WHERE action='SHIP'")[0]
        assert tuple(label) == ("console", "ana")
    finally:
        c.close()


def test_hold_release_reject_and_mode_from_the_keyboard(appliance):
    cfg, path = appliance
    seed(cfg, 2)
    c = Console(path)
    try:
        c.wait_text("Pending 2")
        c.keys(b"h")
        c.wait_text("Hold reason")
        c.keys(b"check stock\r")
        c.wait_text("Held ")
        c.wait_text("Held 1")
        c.keys(b"l")
        c.wait_text("Released ")
        c.keys(b"r")
        c.wait_text("Code 1=STOCK")
        c.keys(b"3")
        c.wait_text("Reason (required)")
        c.keys(b"\r")  # empty reason
        c.wait_text("needs a reason")
        assert q(cfg, "SELECT COUNT(*) FROM requests WHERE state='REJECTED'")[0][0] == 0
        c.keys(b"r3crushed pallet\rY\r", settle=1.0)
        c.wait_text("Rejected ")
        row = q(cfg, "SELECT reject_code, reject_text FROM requests WHERE state='REJECTED'")[0]
        assert tuple(row) == ("DAMAGED_GOODS", "crushed pallet")
        c.keys(b"m")
        c.wait_text("Switch processing mode to AUTO")
        c.keys(b"Y\r")
        c.wait_text("MODE: AUTO")
        assert q(cfg, "SELECT value FROM settings WHERE key='mode'")[0][0] == "AUTO"
    finally:
        c.close()


def test_resize_smaller_then_back_and_ctrl_c_does_not_exit(appliance):
    cfg, path = appliance
    seed(cfg, 1)
    c = Console(path)
    try:
        c.wait_text("BOX1 LEGACY ERP")
        c.resize(60, 20)
        c.wait_text("too small")
        c.resize(100, 30)
        screen = c.wait_text("BOX1 LEGACY ERP")
        assert "Enter=Detail" in c.screen.display[29]
        c.resize(80, 24)
        c.wait_text("BOX1 LEGACY ERP")
        c.keys(b"\x03\x1a\x1c", settle=0.8)  # Ctrl-C, Ctrl-Z, Ctrl-\
        assert c.proc.poll() is None, "console exited on a control key"
        assert "BOX1 LEGACY ERP" in c.text()
        del screen
    finally:
        c.close()


def test_worker_stopped_is_visible_and_the_console_stays_usable(appliance):
    cfg, path = appliance
    seed(cfg, 1)
    w = start_worker(path)
    c = Console(path)
    try:
        c.wait_text("WORKER OK")
        w.send_signal(signal.SIGTERM)
        w.wait(10)
        c.wait_text("WORKER STOPPED")
        assert "imports and publication stopped" in c.text()
        c.keys(b"h")
        c.wait_text("Hold reason")
        c.keys(b"\r")
        c.wait_text("Held ")
        assert q(cfg, "SELECT state FROM requests")[0][0] == "HELD"
        # a killed (not stopped) worker shows as unresponsive once its heartbeat ages
        w = start_worker(path)
        c.wait_text("WORKER OK")
        w.kill()
        w.wait(10)
        c.wait_text("WORKER UNRESPONSIVE", timeout=15)
    finally:
        c.close()
        if w.poll() is None:
            w.kill()


def test_tui_restart_does_not_interrupt_the_worker(appliance):
    cfg, path = appliance
    w = start_worker(path)
    try:
        conn = db.connect(str(cfg.ledger))
        domain.set_mode(conn, "AUTO", actor=domain.CONSOLE)
        conn.close()
        for i in range(3):
            c = Console(path)
            c.wait_text("BOX1 LEGACY ERP")
            drop(cfg, f"REQ.ful_{i}.0a1b2c3d.erp1", request_bytes(ful=f"ful_{i}", order=f"ord_{i}"))
            c.proc.kill()  # TUI crash or console disconnect
            c.close()
            wait_for(lambda i=i: q(cfg, "SELECT state FROM requests WHERE request_id=?", f"ful_{i}") and
                     q(cfg, "SELECT state FROM requests WHERE request_id=?", f"ful_{i}")[0][0] == "SHIPPED")
        assert w.poll() is None
        assert q(cfg, "SELECT COUNT(*) FROM shipments")[0][0] == 3
    finally:
        w.send_signal(signal.SIGTERM)
        w.wait(10)


def test_quarantine_screen_and_replay(appliance):
    cfg, path = appliance
    from box1_erp.worker import Worker
    wk = Worker(cfg)
    wk.open()
    drop(cfg, "REQ.ful_1.0a1b2c3d.erp1", b"broken\n")
    wk.cycle()
    c = Console(path)
    try:
        c.wait_text("Quarantine 1")
        c.keys(b"q")
        screen = c.wait_text("MALFORMED_RECORD")
        assert "REQ.ful_1.0a1b2c3d.erp1" in screen
        c.keys(b"p")
        c.wait_text("Type Y to confirm")
        c.keys(b"Y\r")
        c.wait_text("Replay requested")
        assert q(cfg, "SELECT state FROM quarantine")[0][0] == "REPLAY_REQUESTED"
    finally:
        c.close()
    wk.cycle()
    assert q(cfg, "SELECT state FROM quarantine")[0][0] == "QUARANTINED"  # still malformed; a new ERR was published
    assert len([p for p in cfg.outbound.iterdir() if p.name.startswith("ERR.")]) == 2


def test_demo_fault_screen_arms_and_shows_faults(appliance):
    cfg, path = appliance
    seed(cfg, 1)
    c = Console(path)
    try:
        c.wait_text("BOX1 LEGACY ERP")
        c.keys(b"x")
        c.wait_text("DEMO FAULTS")
        c.keys(b"1")
        c.wait_text("PUBLICATION PAUSED (demo)")
        c.keys(b"2")
        c.wait_text("Type Y to confirm")
        c.keys(b"Y\r")
        c.wait_text("CRASH ARMED (demo)")
        actions = [r[0] for r in q(cfg, "SELECT new_state FROM audit WHERE action='FAULT' ORDER BY id")]
        assert actions == ["publication_paused=1", "crash_after_commit=1"]
    finally:
        c.close()
