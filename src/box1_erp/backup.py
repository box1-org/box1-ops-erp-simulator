"""Application-consistent backup and restore (decision D-012).

Backup: the SQLite online backup API gives a consistent snapshot while the worker
runs; the copy is integrity-checked; the spool and unconsumed exchange files go
into a tar with a manifest of row counts and sha256 digests. Everything is written
into a temporary directory and renamed into place only when complete.

Restore: needs the exclusive ledger lock, so it refuses while any worker or console
has the ledger open. It refuses to roll back past results or shipments the current
ledger has (the adapter may already hold them) unless told to accept that. It moves
the current ledger and spool aside rather than deleting them, and the restored
ledger starts in MANUAL mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import time
from pathlib import Path

from . import db, domain
from .config import Config

TABLES = ("requests", "request_lines", "shipments", "outbox", "audit", "quarantine", "settings")


class RestoreRefused(RuntimeError):
    pass


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}  # noqa: S608 - fixed names


def _fsync(path: Path) -> None:
    with open(path, "rb") as fh:
        os.fsync(fh.fileno())


def _add_tree(tar: tarfile.TarFile, root: Path, arcname: str) -> int:
    """Add regular files only, skipping anything that vanishes or changes type mid-walk
    (the worker and the adapter keep moving files while a backup runs)."""
    added = 0
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        try:
            fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                continue
            info = tarfile.TarInfo(f"{arcname}/{entry.name}")
            info.size, info.mtime, info.mode = st.st_size, int(st.st_mtime), 0o640
            with os.fdopen(os.dup(fd), "rb") as fh:
                tar.addfile(info, fh)
            added += 1
        finally:
            os.close(fd)
    return added


def backup(cfg: Config, label: str | None = None) -> Path:
    stamp = label or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = cfg.backups / stamp
    if target.exists():
        raise FileExistsError(f"backup {target} already exists")
    work = cfg.backups / f".{stamp}.partial"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    src = db.connect(str(cfg.ledger))
    dst_path = work / "ledger.db"
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"backup integrity_check failed: {check}")
        counts = _counts(dst)
        schema = dst.execute("PRAGMA user_version").fetchone()[0]
    finally:
        dst.close()
        src.close()
    spool = work / "spool.tar"
    files = {}
    with tarfile.open(spool, "w") as tar:
        for name, path in (("archive", cfg.archive), ("quarantine", cfg.quarantine),
                           ("inbound", cfg.inbound), ("outbound", cfg.outbound)):
            files[name] = _add_tree(tar, path, name)
    manifest = {"created_at": db.now(), "schema": schema, "counts": counts, "spool_files": files,
                "files": {"ledger.db": _sha(dst_path), "spool.tar": _sha(spool)}}
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for p in (dst_path, spool, work / "manifest.json"):
        _fsync(p)
    os.replace(work, target)
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


def _progress(path: Path) -> dict[str, tuple[int, str | None]]:
    """request -> (highest result revision, shipment id) in a ledger file."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0]: (r[1] or 0, r[2]) for r in conn.execute(
            "SELECT r.request_id, (SELECT MAX(revision) FROM outbox o WHERE o.request_id=r.request_id AND o.kind='RES'),"
            " r.shipment_id FROM requests r")}
    finally:
        conn.close()


def divergence(current: Path, restored: Path) -> list[str]:
    """Requests whose current state is ahead of the backup. Restoring would let them be
    shipped or revised again under revision numbers the adapter has already seen."""
    now, then = _progress(current), _progress(restored)
    out = []
    for rid, (rev, shp) in sorted(now.items()):
        old_rev, old_shp = then.get(rid, (0, None))
        if rev > old_rev or (shp and shp != old_shp):
            out.append(rid)
    return out


def restore(cfg: Config, target: Path, *, accept_divergence: bool = False) -> dict:
    lock = db.LedgerLock(str(cfg.ledger), exclusive=True)
    if not lock.acquire():
        raise RestoreRefused("the worker or a console has the ledger open; stop box1-erp-worker and box1-erp-tui@* first")
    try:
        manifest = verify(target)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        divergent: list[str] = []
        previous = None
        if cfg.ledger.exists():
            snap = cfg.state_dir / f".ledger.pre-restore-{stamp}.db"  # consistent copy, includes the WAL
            live, copy = sqlite3.connect(cfg.ledger), sqlite3.connect(snap)
            try:
                live.backup(copy)
            finally:
                copy.close()
                live.close()
            divergent = divergence(snap, target / "ledger.db")
            if divergent and not accept_divergence:
                snap.unlink()
                raise RestoreRefused(
                    f"{len(divergent)} request(s) are ahead of this backup (e.g. {', '.join(divergent[:5])}); "
                    "restoring would reuse revisions or allow a second shipment. Re-run with --accept-divergence "
                    "only after reconciling with the adapter (runbook)")
            previous = cfg.state_dir / f"ledger.pre-restore-{stamp}.db"
            os.replace(snap, previous)
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(cfg.ledger) + suffix)
                if p.exists():
                    p.unlink()
        preserved = {}
        for name, d in (("archive", cfg.archive), ("quarantine", cfg.quarantine)):
            aside = d.with_name(f"{d.name}.pre-restore-{stamp}")
            os.replace(d, aside)
            d.mkdir(mode=0o750)
            preserved[name] = str(aside)
        tmp = cfg.ledger.with_name(".ledger.db.restore.tmp")
        shutil.copyfile(target / "ledger.db", tmp)
        _fsync(tmp)
        os.replace(tmp, cfg.ledger)
        with tarfile.open(target / "spool.tar") as tar:
            for member in tar.getmembers():
                top, _, base = member.name.partition("/")
                dest = {"archive": cfg.archive, "quarantine": cfg.quarantine}.get(top)
                if dest is None or not member.isfile() or not base or "/" in base or base in (".", ".."):
                    continue  # exchange files are not restored over live SFTP areas; they are in the tar for inspection
                fd = os.open(dest / base, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
                with tar.extractfile(member) as src, os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(src, out)
        conn = db.connect(str(cfg.ledger))
        try:
            if _counts(conn) != manifest["counts"]:
                raise RuntimeError("restored ledger row counts differ from the manifest")
            domain.after_restore(conn, source=target.name, divergent=divergent)
        finally:
            conn.close()
        return {"restored": str(target), "counts": manifest["counts"], "divergent": divergent,
                "previous_ledger": str(previous) if previous else None,
                "previous_spool": preserved, "mode": "MANUAL"}
    finally:
        lock.release()
