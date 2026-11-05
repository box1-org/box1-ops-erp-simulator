"""`box1-erp` command: init, worker, tui, status, backup, restore, verify-backup."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import backup, db
from . import config as config_mod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="box1-erp")
    ap.add_argument("--config", help="configuration file (default /etc/box1-erp/erp.conf or $BOX1_ERP_CONFIG)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create directories and an empty ledger; refuses if a ledger exists")
    sub.add_parser("worker", help="run the background worker")
    t = sub.add_parser("tui", help="run the console")
    t.add_argument("--label", default="", help="default operator label for the audit trail")
    st = sub.add_parser("status", help="print counts, ages and worker freshness as JSON")
    st.add_argument("--check", action="store_true", help="exit 0 healthy, 1 degraded, 2 worker down (for monitoring)")
    sub.add_parser("migrate", help="bring an existing ledger to this version's schema (backup first; services stopped)")
    b = sub.add_parser("backup", help="consistent backup into <state_dir>/backups/")
    b.add_argument("--label")
    r = sub.add_parser("restore", help="restore a backup directory (worker must be stopped)")
    r.add_argument("path")
    r.add_argument("--accept-divergence", action="store_true",
                   help="restore even though the current ledger has newer revisions or shipments (see runbook)")
    v = sub.add_parser("verify-backup", help="check a backup's digests, integrity and counts")
    v.add_argument("path")
    args = ap.parse_args(argv)
    cfg = config_mod.load(args.config)

    if args.cmd == "init":
        if cfg.ledger.exists():
            print(f"ledger already exists: {cfg.ledger}", file=sys.stderr)
            return 1
        if not cfg.state_dir.is_dir():
            print(f"state directory missing (is the data volume mounted?): {cfg.state_dir}", file=sys.stderr)
            return 2
        for d in (cfg.archive, cfg.quarantine, cfg.backups):
            d.mkdir(parents=True, exist_ok=True)
        db.initialize(str(cfg.ledger))
        print(f"initialized {cfg.ledger}")
        return 0
    if args.cmd == "worker":
        from . import worker
        return worker.main(cfg)
    if args.cmd == "tui":
        from . import tui
        return tui.main(cfg, label=args.label)
    if args.cmd == "status":
        from .status import snapshot
        conn = db.connect(str(cfg.ledger))
        try:
            s = snapshot(conn, cfg)
            print(json.dumps(s.as_dict(), indent=2))
        finally:
            conn.close()
        return s.exit_code() if args.check else 0
    if args.cmd == "migrate":
        lock = db.LedgerLock(str(cfg.ledger), exclusive=True)
        if not lock.acquire():
            print("the worker or a console has the ledger open; stop box1-erp-worker and box1-erp-tui@* first", file=sys.stderr)
            return 1
        try:
            if not cfg.ledger.exists():
                print(f"no ledger at {cfg.ledger}", file=sys.stderr)
                return 2
            db.initialize(str(cfg.ledger))
            print(f"ledger at schema {db.SCHEMA_VERSION}")
            return 0
        finally:
            lock.release()
    if args.cmd == "backup":
        print(backup.backup(cfg, args.label))
        return 0
    if args.cmd == "restore":
        try:
            result = backup.restore(cfg, Path(args.path), accept_divergence=args.accept_divergence)
        except backup.RestoreRefused as exc:
            print(f"restore refused: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    if args.cmd == "verify-backup":
        print(json.dumps(backup.verify(Path(args.path))["counts"]))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
