"""Console TUI (box1-erp-tui@.service): keyboard-only, 80x24, ASCII, monochrome.

Every action calls domain.py. Nothing here holds a transaction while waiting for
a key: the screen reads, the operator confirms, then one domain call commits.
There is no quit key, shell escape or SQL editing; systemd restarts the TUI.
"""

from __future__ import annotations

import curses
import os
import sys
import time

from . import db, domain, protocol
from .config import Config
from .log import Log
from .status import snapshot

MIN_W, MIN_H = 80, 24
FILTERS = ("ALL", "PENDING", "HELD", "SHIPPED", "REJECTED")
KEYS = "Enter=Detail H=Hold L=Release S=Ship R=Reject E=Re-emit M=Mode /=Find F=Filter Q=Quarant ?=Help"


def asc(value, width: int | None = None) -> str:
    """ASCII-safe text: non-ASCII as \\uXXXX, control characters as '?'."""
    out = []
    for ch in str(value if value is not None else ""):
        o = ord(ch)
        if 32 <= o < 127:
            out.append(ch)
        elif o < 32 or 127 <= o < 160:
            out.append("?")
        else:
            out.append(f"\\u{o:04x}" if o <= 0xFFFF else f"\\U{o:08x}")
    s = "".join(out)
    if width is not None:
        s = s[:width].ljust(width)
    return s


def cursor(visible: int) -> None:
    """Not every terminal (vt100, some serial consoles) can hide the cursor; that is cosmetic."""
    try:
        curses.curs_set(visible)
    except curses.error:
        pass


def fit(value, width: int) -> str:
    """ASCII-safe, padded to width; a cut value ends in '~' so truncation is visible."""
    s = asc(value)
    return (s[: width - 1] + "~") if len(s) > width else s.ljust(width)


def short_time(ts: str | None) -> str:
    return ts[11:19] if ts else "-"


class Tui:
    def __init__(self, scr, cfg: Config, label: str = "", log: Log | None = None):
        self.scr = scr
        self.cfg = cfg
        self.label = label
        self.log = log or Log("tui", stream=None)
        self.conn = None
        self.rows: list = []
        self.sel = 0
        self.top = 0
        self.filter = "ALL"
        self.search = ""
        self.message = "Ready."
        self.status = None
        self.lock = None
        self._seen = None  # (data_version, filter, search) of the rows on screen
        self.dirty = True

    # ------------------------------------------------------------ data

    def ensure_conn(self) -> bool:
        if self.conn is not None:
            return True
        lock = db.LedgerLock(str(self.cfg.ledger))
        try:
            if not lock.acquire():
                self.message = "Ledger locked: a restore or migration is in progress."
                return False
            self.conn = db.connect(str(self.cfg.ledger))
            self.lock = lock
            self.dirty = True
            return True
        except (db.MissingDatabase, db.SchemaMismatch, OSError) as exc:
            lock.release()
            self.message = f"Ledger unavailable: {exc}"
            return False

    def load(self) -> None:
        self.status = snapshot(self.conn, self.cfg)
        seen = (self.conn.execute("PRAGMA data_version").fetchone()[0], self.filter, self.search)
        if seen == self._seen and not self.dirty:
            return  # nothing changed since the last read (other connections bump data_version)
        self._seen, self.dirty = seen, False
        sql = ("SELECT r.request_id, r.order_id, r.state, r.version, r.shipment_id, r.site_id, r.origin, r.updated_at,"
               " (SELECT CASE WHEN o.last_error IS NOT NULL AND o.publish_state='PENDING_PUBLISH' THEN 'FAIL'"
               "  ELSE o.publish_state END FROM outbox o WHERE o.request_id=r.request_id AND o.kind='RES'"
               "  ORDER BY o.revision DESC LIMIT 1) AS pub,"
               " (SELECT COUNT(*) FROM request_lines l WHERE l.request_id=r.request_id) AS nlines"
               " FROM requests r WHERE 1=1")
        args: list = []
        if self.filter != "ALL":
            sql += " AND r.state=?"
            args.append(self.filter)
        if self.search:
            sql += " AND (r.request_id LIKE ? OR r.order_id LIKE ? OR r.shipment_id LIKE ? OR r.site_id LIKE ?)"
            args += [f"%{self.search}%"] * 4
        sql += " ORDER BY r.updated_at DESC, r.request_id LIMIT 1000"
        self.rows = self.conn.execute(sql, args).fetchall()
        self.sel = max(0, min(self.sel, len(self.rows) - 1))

    def current(self):
        return self.rows[self.sel] if self.rows else None

    # ------------------------------------------------------------ drawing

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addstr(y, x, asc(text)[: max(0, w - x - (1 if y == h - 1 else 0))], attr)
            except curses.error:
                pass

    def too_small(self) -> bool:
        h, w = self.scr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            self.scr.erase()
            self.put(0, 0, f"Terminal {w}x{h} is too small. The ERP console needs {MIN_W}x{MIN_H}.")
            self.put(1, 0, "Resize the terminal; the worker keeps running.")
            self.scr.refresh()
            return True
        return False

    def header(self) -> None:
        h, w = self.scr.getmaxyx()
        s = self.status
        if s.worker == "RUNNING":
            worker = f"WORKER OK {s.heartbeat_age:.0f}s"
        elif s.heartbeat_age is None:
            worker = f"WORKER {s.worker}"
        else:
            worker = f"WORKER {s.worker} {s.heartbeat_age:.0f}s AGO"
        title = f" BOX1 LEGACY ERP | MODE: {s.mode} | {worker} | {time.strftime('%H:%M:%SZ', time.gmtime())} "
        self.put(0, 0, title.ljust(w), curses.A_REVERSE | curses.A_BOLD)
        counts = (f"Pending {s.pending}  Held {s.held}  Quarantine {s.quarantined}  "
                  f"Out-pending {s.pending_output}  Op: {self.label or '-'}")
        self.put(1, 0, counts)
        alert = " | ".join(s.conditions)
        self.put(2, 0, asc(alert, w - 1), curses.A_BOLD if alert != "OK" else 0)

    def footer(self, keys: str = KEYS) -> None:
        h, w = self.scr.getmaxyx()
        self.put(h - 2, 0, asc(self.message, w - 1), curses.A_BOLD)
        self.put(h - 1, 0, asc(keys, w - 1), curses.A_REVERSE)

    def draw_ledger(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        self.header()
        self.put(3, 0, asc(f"Filter: {self.filter}  Find: {self.search or '-'}  {len(self.rows)} request(s)", w - 1))
        cols = f"{'REQUEST':<19}{'ORDER':<13}{'STATE':<9}{'SHIPMENT':<12}{'SITE':<8}{'O':<2}{'LN':<3}{'UPDATED':<9}{'OUT'}"
        self.put(4, 0, asc(cols, w - 1), curses.A_UNDERLINE)
        body = h - 7
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + body:
            self.top = self.sel - body + 1
        for i, r in enumerate(self.rows[self.top:self.top + body]):
            idx = self.top + i
            pub = {"PUBLISHED": "OK", "PENDING_PUBLISH": "WAIT", "FAIL": "FAIL"}.get(r["pub"], "-")
            origin = {"EVENT": "E", "API": "A"}.get(r["origin"], "?")
            # 19+13+9+12+8+2+3+9+4 = 79 columns (the last cell of a line is not written). O: E=event, A=API.
            line = (fit(r["request_id"], 18) + " " + fit(r["order_id"], 12) + " " + fit(r["state"], 8) + " "
                    + fit(r["shipment_id"] or "-", 11) + " " + fit(r["site_id"], 7) + " " + origin + " "
                    + fit(r["nlines"], 2) + " " + short_time(r["updated_at"]).ljust(9) + asc(pub, 4))
            self.put(5 + i, 0, asc(line, w - 1), curses.A_REVERSE if idx == self.sel else 0)
        if not self.rows:
            self.put(6, 2, "No requests match.")
        self.footer()
        self.scr.refresh()

    # ------------------------------------------------------------ input helpers

    def getkey(self, timeout_ms: int = 1000):
        """Next key, or None on timeout. Ctrl-L is handled here, so it works on every
        screen and in prompts: it repaints and is not passed on."""
        self.scr.timeout(timeout_ms)
        try:
            k = self.scr.get_wch()
        except curses.error:
            return None
        if k == "\x0c":
            self.repaint()
            return None
        return k

    def repaint(self) -> None:
        """Reset the terminal and redraw every cell (Ctrl-L, G). curses normally sends only
        the cells it believes changed, so noise on a serial line, kernel messages on the
        console or a reconnected `virtctl console` stay on screen until this runs.
        endwin() + refresh() re-sends the terminal init strings and restores raw/keypad
        modes; clearok() makes the next refresh clear the screen and send everything."""
        curses.endwin()
        self.scr.refresh()
        curses.update_lines_cols()
        self.scr.keypad(True)
        self.scr.clearok(True)
        self.scr.refresh()

    def prompt(self, question: str, max_len: int = 60, allowed=None, single: bool = False) -> str | None:
        """Line editor on the message line. Enter accepts, Esc cancels. Printable ASCII only.
        single=True returns on the first allowed key (one-key menus)."""
        buf = ""
        h, w = self.scr.getmaxyx()
        cursor(1)
        try:
            while True:
                self.put(h - 2, 0, asc(f"{question} {buf}", w - 1), curses.A_BOLD)
                self.scr.move(h - 2, min(w - 2, len(question) + 1 + len(buf)))
                self.scr.refresh()
                k = self.getkey(-1)
                if k in ("\n", "\r", curses.KEY_ENTER):
                    return buf
                if k == "\x1b":
                    return None
                if k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    buf = buf[:-1]
                elif k == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    h, w = self.scr.getmaxyx()
                elif isinstance(k, str) and 32 <= ord(k) < 127 and len(buf) < max_len and (allowed is None or k in allowed):
                    buf += k
                    if single:
                        return buf
        finally:
            cursor(0)

    def confirm(self, question: str) -> bool:
        answer = self.prompt(f"{question} Type Y to confirm, Esc to cancel:", 1, "YyNn")
        return answer is not None and answer.upper() == "Y"

    def act(self, fn, done: str) -> None:
        try:
            result = fn()
            self.dirty = True
            self.message = done.format(result=result)
            self.log.info("console action", message=self.message, label=self.label)
        except domain.StaleVersion as exc:
            self.dirty = True
            self.message = f"Not done: {exc}"
        except domain.DomainError as exc:
            self.message = f"Not done: {exc}"
        except Exception as exc:  # noqa: BLE001 - the console must stay up; details go to the journal
            self.message = f"Error: {type(exc).__name__}; see journal"
            self.log.error("console action failed", error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ actions (shared by ledger and detail)

    def action(self, key: str, row) -> bool:
        if row is None:
            self.message = "No request selected."
            return key in "hlsre"
        rid, ver = row["request_id"], row["version"]
        if key == "h":
            reason = self.prompt("Hold reason (optional):", 60)
            if reason is not None:
                self.act(lambda: domain.hold(self.conn, rid, expected_version=ver, actor=domain.CONSOLE, label=self.label,
                                             reason=reason), f"Held {rid}.")
        elif key == "l":
            self.act(lambda: domain.release(self.conn, rid, expected_version=ver, actor=domain.CONSOLE, label=self.label),
                     f"Released {rid}.")
        elif key == "s":
            if self.confirm(f"Ship {rid}?"):
                self.act(lambda: domain.ship(self.conn, rid, expected_version=ver, actor=domain.CONSOLE, label=self.label),
                         f"Shipped {rid} as {{result}}.")
            else:
                self.message = "Ship cancelled."
        elif key == "r":
            menu = "  ".join(f"{i}={c}" for i, c in enumerate(protocol.REJECTION_CODES, 1))
            choice = self.prompt(f"Code {menu}:", 1, "12345", single=True)
            if not choice:
                self.message = "Reject cancelled."
                return True
            code = protocol.REJECTION_CODES[int(choice) - 1]
            text = self.prompt("Reason (required):", 60)
            if text is None:
                self.message = "Reject cancelled."
            elif not text.strip():
                self.message = "Not done: a rejection needs a reason."
            elif self.confirm(f"Reject {rid} ({code})?"):
                self.act(lambda: domain.reject(self.conn, rid, expected_version=ver, actor=domain.CONSOLE, code=code,
                                               reason=text, label=self.label), f"Rejected {rid}.")
            else:
                self.message = "Reject cancelled."
        elif key == "e":
            self.act(lambda: domain.reemit(self.conn, rid, actor=domain.CONSOLE, label=self.label),
                     f"Re-emit queued for {rid}: {{result}} (no new shipment).")
        else:
            return False
        return True

    def change_mode(self) -> None:
        new = "AUTO" if self.status.mode == "MANUAL" else "MANUAL"
        if self.confirm(f"Switch processing mode to {new}?"):
            self.act(lambda: domain.set_mode(self.conn, new, actor=domain.CONSOLE, label=self.label), f"Mode is now {new}.")

    # ------------------------------------------------------------ screens

    def detail(self, rid: str) -> None:
        scroll = 0
        while True:
            if self.too_small():
                k = self.getkey()
                if k == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                continue
            row = self.conn.execute("SELECT * FROM requests WHERE request_id=?", (rid,)).fetchone()
            self.status = snapshot(self.conn, self.cfg)
            lines = self.detail_lines(row)
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.header()
            body = h - 6
            scroll = max(0, min(scroll, len(lines) - body))
            for i, text in enumerate(lines[scroll:scroll + body]):
                self.put(3 + i, 0, asc(text, w - 1), curses.A_BOLD if text.startswith("==") else 0)
            self.footer("B/Esc=Back  Up/Down=Scroll  H=Hold L=Release S=Ship R=Reject E=Re-emit")
            self.scr.refresh()
            k = self.getkey()
            if k is None:
                continue
            if k == curses.KEY_RESIZE:
                curses.update_lines_cols()
            elif k in ("\x1b", "b", "B"):
                return
            elif k in (curses.KEY_DOWN, "j"):
                scroll += 1
            elif k in (curses.KEY_UP, "k"):
                scroll -= 1
            elif k == curses.KEY_NPAGE:
                scroll += body
            elif k == curses.KEY_PPAGE:
                scroll -= body
            elif isinstance(k, str):
                self.action(k.lower(), row)

    def detail_lines(self, r) -> list[str]:
        out = [f"== Request {r['request_id']}  state {r['state']}  version {r['version']}",
               f"Order {r['order_id']}  Reservation {r['reservation_id']}  Site {r['site_id']}",
               f"Origin {r['origin']}  Requested {r['requested_at']}  Imported {r['created_at']}",
               f"Shipment {r['shipment_id'] or '-'}  Correlation {r['correlation_id'] or '-'}"]
        if r["state"] == "REJECTED":
            out.append(f"Rejected {r['reject_code']}: {r['reject_text']}")
        out.append("== Lines")
        for ln in self.conn.execute("SELECT line_no, sku, quantity FROM request_lines WHERE request_id=? ORDER BY line_no",
                                    (r["request_id"],)):
            out.append(f"  {ln['line_no']:>2}  {asc(ln['sku'])}  x {ln['quantity']}")
        out.append("== History and operator actions")
        for a in self.conn.execute("SELECT at, action, prev_state, new_state, actor_type, actor_label, reason FROM audit"
                                   " WHERE request_id=? ORDER BY id", (r["request_id"],)):
            who = a["actor_type"] + (f"/{a['actor_label']}" if a["actor_label"] else "")
            trans = f"{a['prev_state'] or '-'}>{a['new_state'] or '-'}"
            out.append(f"  {a['at'][:19]} {a['action']:<9} {trans:<18} {who} {a['reason']}")
        out.append("== Results (revision, status, publication, file)")
        for o in self.conn.execute("SELECT revision, status, publish_state, emit_count, file_name, last_error FROM outbox"
                                   " WHERE request_id=? AND kind='RES' ORDER BY revision", (r["request_id"],)):
            pub = {"PUBLISHED": "published", "PENDING_PUBLISH": "pending"}[o["publish_state"]]
            out.append(f"  r{o['revision']} {o['status']:<8} {pub} x{o['emit_count']} {o['file_name']}")
            if o["last_error"]:
                out.append(f"     last error: {o['last_error']}")
        out.append("== File references")
        for f in self.conn.execute("SELECT at, direction, file_name, outcome FROM request_files WHERE request_id=? ORDER BY id",
                                   (r["request_id"],)):
            out.append(f"  {f['at'][:19]} {f['direction']:<3} {f['outcome']:<12} {f['file_name']}")
        out.append("== Original request file")
        for raw in bytes(r["raw_request"]).decode("utf-8", "replace").splitlines():
            out.append("  " + raw)
        return out

    def quarantine_screen(self) -> None:
        sel = 0
        while True:
            if self.too_small():
                if self.getkey() == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                continue
            rows = self.conn.execute("SELECT id, file_name, error_code, detail, state, quarantined_at FROM quarantine"
                                     " ORDER BY id DESC LIMIT 500").fetchall()
            self.status = snapshot(self.conn, self.cfg)
            sel = max(0, min(sel, len(rows) - 1))
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.header()
            self.put(3, 0, asc(f"{'ID':<6}{'STATE':<17}{'CODE':<19}{'WHEN':<10}FILE", w - 1), curses.A_UNDERLINE)
            body = h - 8
            start = max(0, sel - body + 1)
            for i, q in enumerate(rows[start:start + body]):
                line = f"{q['id']:<6}{q['state']:<17}{q['error_code']:<19}{short_time(q['quarantined_at']):<10}{asc(q['file_name'])}"
                self.put(4 + i, 0, asc(line, w - 1), curses.A_REVERSE if start + i == sel else 0)
            if rows:
                self.put(h - 3, 0, asc("Reason: " + rows[sel]["detail"], w - 1))
            else:
                self.put(5, 2, "No quarantined files.")
            self.footer("B/Esc=Back  Up/Down=Select  P=Replay selected file (deduplication still applies)")
            self.scr.refresh()
            k = self.getkey()
            if k == curses.KEY_RESIZE:
                curses.update_lines_cols()
            elif k in ("\x1b", "b", "B"):
                return
            elif k in (curses.KEY_DOWN, "j"):
                sel += 1
            elif k in (curses.KEY_UP, "k"):
                sel -= 1
            elif k in ("p", "P") and rows:
                q = rows[sel]
                if self.confirm(f"Replay quarantined file {q['id']}?"):
                    self.act(lambda: domain.request_replay(self.conn, q["id"], actor=domain.CONSOLE, label=self.label),
                             f"Replay requested for file {q['id']}; the worker will re-read it.")

    def fault_screen(self) -> None:
        if not self.cfg.demo_faults:
            self.message = "Demo faults are disabled in /etc/box1-erp/erp.conf."
            return
        while True:
            self.status = snapshot(self.conn, self.cfg)
            f = self.status.demo_faults
            self.scr.erase()
            self.header()
            self.put(4, 2, "DEMO FAULTS (audited; visible on the status line while active)", curses.A_BOLD)
            self.put(6, 4, f"1  Pause outbound publication ........ {'ON' if f['publication_paused'] == '1' else 'off'}")
            self.put(7, 4, f"2  One-shot crash after next ship ..... {'ARMED' if f['crash_after_commit'] == '1' else 'off'}")
            self.put(8, 4, f"3  Delay automatic processing (s) ..... {f['auto_delay_seconds']}")
            self.footer("1/2/3=Change  B/Esc=Back")
            self.scr.refresh()
            k = self.getkey()
            if k in ("\x1b", "b", "B"):
                return
            if k == curses.KEY_RESIZE:
                curses.update_lines_cols()
            elif k == "1":
                new = "0" if f["publication_paused"] == "1" else "1"
                self.act(lambda: domain.set_fault(self.conn, "publication_paused", new, demo_enabled=self.cfg.demo_faults,
                                                  actor=domain.CONSOLE, label=self.label), "Publication fault changed.")
            elif k == "2":
                new = "0" if f["crash_after_commit"] == "1" else "1"
                if new == "0" or self.confirm("Arm a one-shot worker crash after the next shipment commit?"):
                    self.act(lambda: domain.set_fault(self.conn, "crash_after_commit", new, demo_enabled=self.cfg.demo_faults,
                                                      actor=domain.CONSOLE, label=self.label), "Crash fault changed.")
            elif k == "3":
                v = self.prompt("Delay seconds (0-3600):", 4, "0123456789")
                if v:
                    self.act(lambda: domain.set_fault(self.conn, "auto_delay_seconds", str(int(v)), demo_enabled=self.cfg.demo_faults,
                                                      actor=domain.CONSOLE, label=self.label), "Delay changed.")

    def help_screen(self) -> None:
        text = ["CONSOLE HELP", "",
                "Up/Down or J/K  move        Enter  request detail       / find (ID, order, site)",
                "F  cycle state filter        G  refresh and redraw     N  set operator label",
                "H  hold (stops AUTO)         L  release to PENDING      S  ship (confirm)",
                "R  reject: code, reason, confirm                        E  re-emit latest result",
                "M  switch MANUAL/AUTO        Q  quarantined files       X  demo faults (if enabled)",
                "Columns: O = origin (E event, A API), LN = line items, OUT = result publication",
                "(OK published, WAIT pending, FAIL write failing). A '~' marks a cut value.",
                "",
                "The operator label is an audit label, not an authenticated identity.",
                "SHIPPED and REJECTED are final. Re-emit never creates a shipment.",
                "Ctrl-L redraws the whole screen on any screen (after noise or a reconnect).",
                "This console has no exit: closing it leaves the worker running.",
                "", "Press any key."]
        self.scr.erase()
        for i, t in enumerate(text):
            self.put(1 + i, 2, t, curses.A_BOLD if i == 0 else 0)
        self.scr.refresh()
        self.getkey(-1)

    # ------------------------------------------------------------ main loop

    def run(self) -> None:
        cursor(0)
        curses.raw()  # Ctrl-C, Ctrl-Z and Ctrl-\ are ordinary keys: no way out to a shell
        self.scr.keypad(True)
        if not self.label:
            self.label = (self.prompt("Operator label for the audit trail (Enter to skip):", 40) or "").strip()
        while True:
            if not self.ensure_conn():
                self.scr.erase()
                self.put(0, 0, "BOX1 LEGACY ERP", curses.A_REVERSE)
                self.put(2, 0, self.message)
                self.put(3, 0, "Retrying every 5 seconds. Check the data volume (see runbook).")
                self.scr.refresh()
                self.getkey(5000)
                continue
            if self.too_small():
                if self.getkey() == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                continue
            try:
                self.load()
            except Exception as exc:  # noqa: BLE001 - e.g. a lock timeout; show it and retry
                self.message = f"Ledger read failed ({type(exc).__name__}); retrying."
                self.log.error("ledger read failed", error=str(exc))
                self.getkey(1000)
                continue
            self.draw_ledger()
            k = self.getkey()
            if k is None:
                continue
            if k == curses.KEY_RESIZE:
                curses.update_lines_cols()
                continue
            if k in (curses.KEY_DOWN, "j"):
                self.sel = min(self.sel + 1, max(0, len(self.rows) - 1))
            elif k in (curses.KEY_UP, "k"):
                self.sel = max(self.sel - 1, 0)
            elif k == curses.KEY_NPAGE:
                self.sel = min(self.sel + 10, max(0, len(self.rows) - 1))
            elif k == curses.KEY_PPAGE:
                self.sel = max(self.sel - 10, 0)
            elif k in ("\n", "\r", curses.KEY_ENTER):
                if self.current():
                    self.detail(self.current()["request_id"])
            elif isinstance(k, str):
                c = k.lower()
                if self.action(c, self.current()):
                    pass
                elif c == "m":
                    self.change_mode()
                elif c == "/":
                    s = self.prompt("Find:", 40)
                    if s is not None:
                        self.search, self.sel = s.strip(), 0
                elif c == "f":
                    self.filter = FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]
                    self.sel = 0
                elif c == "q":
                    self.quarantine_screen()
                elif c == "x":
                    self.fault_screen()
                elif c == "n":
                    s = self.prompt("Operator label:", 40)
                    if s is not None:
                        self.label = s.strip()
                elif c == "?":
                    self.help_screen()
                elif c == "g":
                    self.message = "Refreshed and redrawn."
                    self.repaint()


def main(cfg: Config, label: str = "") -> int:
    log = Log("tui", stream=None if sys.stderr.isatty() else sys.stderr)
    os.environ.setdefault("ESCDELAY", "50")
    log.info("console starting", tty=os.environ.get("TTY", ""), term=os.environ.get("TERM", ""))

    def _run(scr):
        Tui(scr, cfg, label=label, log=log).run()

    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass
    log.info("console stopped")
    return 0
