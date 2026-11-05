"""File publication and moves (decision D-009): temp name, fsync, rename, fsync directory."""

from __future__ import annotations

import errno
import os
from pathlib import Path


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(directory: Path, name: str, data: bytes) -> None:
    """Atomically visible and durable: readers see either no file or the whole file."""
    tmp = directory / f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, directory / name)
    fsync_dir(directory)


def move(src: Path, dst_dir: Path, name: str) -> Path:
    """Move a file out of the exchange. Rename when on one filesystem; otherwise copy durably, then unlink."""
    dst = dst_dir / name
    try:
        os.replace(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        publish(dst_dir, name, src.read_bytes())
        os.unlink(src)
    fsync_dir(dst_dir)
    fsync_dir(src.parent)
    return dst


def read_bounded(path: Path, limit: int) -> bytes:
    """Read at most limit+1 bytes, enough to detect an oversized file without loading it all."""
    with open(path, "rb") as fh:
        return fh.read(limit + 1)


def remove_stale_temp(directory: Path, prefixes: tuple[str, ...]) -> list[str]:
    removed = []
    for entry in os.scandir(directory):
        if entry.name.startswith(prefixes) and entry.name.endswith(".tmp"):
            os.unlink(entry.path)
            removed.append(entry.name)
    if removed:
        fsync_dir(directory)
    return removed
