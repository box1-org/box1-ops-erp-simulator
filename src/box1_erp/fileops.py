"""File publication and moves (decision D-009) that are safe against the SFTP account.

The SFTP account can create, rename, symlink and hardlink entries in the exchange
directories. So the worker never follows a link, never opens a predictable temp
name, and never keeps an inode the SFTP account owns: inbound files are copied
into a new file owned by the application, then the inbound name is unlinked.
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 16


class Unsafe(OSError):
    """An exchange entry or target is not what it must be (link, directory, ...)."""


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(directory: Path, data_chunks, mode: int = 0o640) -> Path:
    """Write to a fresh, unpredictable temp name. O_EXCL|O_NOFOLLOW: a planted name or link fails."""
    for _ in range(5):
        tmp = directory / f".{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            break
        except FileExistsError:
            continue
    else:
        raise Unsafe(f"could not create a temp file in {directory}")
    try:
        for chunk in data_chunks:
            view = memoryview(chunk)
            while view:
                view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp)
        raise
    os.close(fd)
    return tmp


def _check_target(path: Path) -> None:
    """A link or file at the final name is replaced by the rename (never followed);
    a directory cannot be, so that one result fails alone and is retried."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        raise Unsafe(f"{path.name} exists and is a directory")


def publish(directory: Path, name: str, data: bytes) -> None:
    """Atomically visible and durable. A rename replaces a planted symlink at the final
    name rather than following it; a planted directory makes this file fail alone."""
    _check_target(directory / name)
    tmp = _write_new(directory, [data])
    try:
        os.replace(tmp, directory / name)
    except BaseException:
        os.unlink(tmp)
        raise
    fsync_dir(directory)


@dataclass
class Inbound:
    """An inbound regular file, opened without following links."""
    fd: int
    size: int
    key: str          # identifies this upload: device, inode and mtime (a re-upload differs)
    sha256: str
    head: bytes       # at most limit+1 bytes

    def close(self) -> None:
        os.close(self.fd)

    def chunks(self, limit: int | None = None):
        os.lseek(self.fd, 0, os.SEEK_SET)
        left = limit
        while left is None or left > 0:
            chunk = os.read(self.fd, CHUNK if left is None else min(CHUNK, left))
            if not chunk:
                return
            if left is not None:
                left -= len(chunk)
            yield chunk


def open_inbound(path: Path, limit: int) -> Inbound:
    """Open without following a link and without blocking on a FIFO; require a regular
    file. Hash in chunks (bounded memory) and keep only the first limit+1 bytes."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Unsafe(f"{path.name} is not a regular file")
        h = hashlib.sha256()
        head = bytearray()
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                break
            h.update(chunk)
            if len(head) <= limit:
                head += chunk[: limit + 1 - len(head)]
        key = f"{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}"
        return Inbound(fd=fd, size=st.st_size, key=key, sha256=h.hexdigest(), head=bytes(head))
    except BaseException:
        os.close(fd)
        raise


def store_copy(src: Inbound, dst_dir: Path, name: str, limit: int | None = None) -> Path:
    """Copy into a new file owned by the application (at most `limit` bytes, for
    oversized uploads), durably, under a deterministic name."""
    tmp = _write_new(dst_dir, src.chunks(limit))
    os.replace(tmp, dst_dir / name)
    fsync_dir(dst_dir)
    return dst_dir / name


def move_unopened(path: Path, dst_dir: Path, name: str) -> Path:
    """Move an entry that cannot be read (link, directory, unreadable file) without
    opening or following it. Same filesystem only; otherwise it stays and is reported."""
    os.rename(path, dst_dir / name)  # rename moves a symlink itself, never its target
    fsync_dir(dst_dir)
    fsync_dir(path.parent)
    return dst_dir / name


def unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
        fsync_dir(path.parent)
    except FileNotFoundError:
        pass


def open_stored(path: Path, limit: int) -> Inbound:
    """Re-read a stored (application-owned) quarantine copy for replay."""
    return open_inbound(path, limit)


def remove_stale_temp(directory: Path) -> list[str]:
    """Remove this application's leftover temp files (random .<hex>.tmp names it owns).
    Entries owned by anyone else, and anything that is not a regular file, are left alone."""
    removed = []
    uid = os.getuid()
    for entry in os.scandir(directory):
        name = entry.name
        if not (name.startswith(".") and name.endswith(".tmp")):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_uid == uid and len(name) == 21:
            os.unlink(entry.path)
            removed.append(name)
    if removed:
        fsync_dir(directory)
    return removed


def is_enoent(exc: OSError) -> bool:
    return exc.errno == errno.ENOENT
