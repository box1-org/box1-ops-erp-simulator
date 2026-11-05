"""Application-consistent backup and restore (decision D-012).

The ledger is copied with the SQLite online backup API, which gives a consistent
snapshot while the worker runs; the copy is integrity-checked. The spool and
unconsumed exchange files go into a tar beside it, with a manifest of row counts
and sha256 digests. Restore runs only while the worker is stopped.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path

from . import db
from .config import Config

TABLES = ("requests", "request_lines", "shipments", "outbox", "audit", "quarantine", "settings")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}  # noqa: S608 - fixed names


def backup(cfg: Config, label: str | None = None) -> Path:
    stamp = label or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = cfg.backups / stamp
    target.mkdir(parents=True, exist_ok=False)
    src = db.connect(str(cfg.ledger))
    dst_path = target / "ledger.db"
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"backup integrity_check failed: {check}")
        counts = _counts(dst)
    finally:
        dst.close()
        src.close()
    spool = target / "spool.tar"
    with tarfile.open(spool, "w") as tar:
        for name, path in (("archive", cfg.archive), ("quarantine", cfg.quarantine),
                           ("inbound", cfg.inbound), ("outbound", cfg.outbound)):
            tar.add(path, arcname=name)
    manifest = {"created_at": db.now(), "counts": counts,
                "files": {"ledger.db": _sha(dst_path), "spool.tar": _sha(spool)}}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for p in (dst_path, spool, target / "manifest.json"):
        with open(p, "rb") as fh:
            os.fsync(fh.fileno())
    return target


def verify(target: Path) -> dict:
    manifest = json.loads((target / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        if _sha(target / name) != digest:
            raise RuntimeError(f"{name} does not match its manifest digest")
    conn = sqlite3.connect(f"file:{target / 'ledger.db'}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup ledger fails integrity_check")
        if _counts(conn) != manifest["counts"]:
            raise RuntimeError("backup ledger row counts differ from the manifest")
    finally:
        conn.close()
    return manifest


def worker_running(cfg: Config) -> bool:
    """Treat a fresh heartbeat as a running worker. systemd should also be stopped by the operator."""
    if not cfg.ledger.exists():
        return False
    conn = db.connect(str(cfg.ledger))
    try:
        row = conn.execute("SELECT heartbeat_at, activity FROM worker_status WHERE id=1").fetchone()
    finally:
        conn.close()
    if not row or not row["heartbeat_at"] or row["activity"] == "stopped":
        return False
    return time.time() - db.parse_ts(row["heartbeat_at"]) < cfg.heartbeat_stale_seconds


def restore(cfg: Config, target: Path) -> dict:
    if worker_running(cfg):
        raise RuntimeError("the worker is running; stop box1-erp-worker.service first")
    manifest = verify(target)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(cfg.ledger) + suffix)
        if p.exists():
            p.unlink()
    tmp = cfg.ledger.with_name(".ledger.db.restore.tmp")
    shutil.copyfile(target / "ledger.db", tmp)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, cfg.ledger)
    for d in (cfg.archive, cfg.quarantine):
        for entry in os.scandir(d):
            os.unlink(entry.path)
    with tarfile.open(target / "spool.tar") as tar:
        for member in tar.getmembers():
            top = member.name.split("/", 1)[0]
            if not member.isfile() or ".." in Path(member.name).parts:
                continue
            dest = {"archive": cfg.archive, "quarantine": cfg.quarantine}.get(top)
            if dest is None:
                continue  # exchange files are not restored over live SFTP areas; they are in the tar for inspection
            with tar.extractfile(member) as src, open(dest / Path(member.name).name, "wb") as out:
                shutil.copyfileobj(src, out)
    conn = db.connect(str(cfg.ledger))
    try:
        if _counts(conn) != manifest["counts"]:
            raise RuntimeError("restored ledger row counts differ from the manifest")
    finally:
        conn.close()
    return manifest
