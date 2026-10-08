"""The snapshot an Update takes before pip runs, and restores on a Rollback.

The venv is copied to ``venv.prev`` beside it (the old version's code, which
the update unit's verify line runs from), and the config dir
(``/etc/sp-rtk-base``) to ``config.prev`` beside the venv, out of the app's
reach. See ADR 0005. Importable without the web app.

The config dir is writable by the app, the venv's dir only by the update
unit, so a compromised app could try to steer the updater's writes out of
the config dir with a symlink, timed between a check and a write. The
config dir is therefore only ever walked through directory file
descriptors opened with ``O_NOFOLLOW``, and written through temp files
created with ``O_CREAT | O_EXCL | O_NOFOLLOW`` then renamed: no link in it
is ever followed, on snapshot or on restore.
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
from collections.abc import Callable
from functools import partial
from pathlib import Path

DEFAULT_CONFIG_DIR = Path("/etc/sp-rtk-base")
DISK_MARGIN_BYTES = 200 * 1024 * 1024
"""Room left over after the snapshot, for pip's downloads and the new
version's files."""


def tree_size(path: Path) -> int:
    """Bytes in the files under ``path`` (symlinks not followed); 0 if none."""
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                pass
    return total


class NotEnoughDiskSpaceError(Exception):
    """Too little room for the snapshot."""


class Snapshot:
    """The copies of ``venv`` and ``config_dir`` an Update can roll back to."""

    def __init__(self, venv: Path, config_dir: Path) -> None:
        self.venv = venv
        self.config_dir = config_dir
        self.venv_copy = venv.with_name(f"{venv.name}.prev")
        self.config_copy = venv.with_name("config.prev")

    @property
    def exists(self) -> bool:
        return self.venv_copy.exists() or self.config_copy.exists()

    def check_space(self) -> None:
        """Refuse unless the snapshot fits, with :data:`DISK_MARGIN_BYTES` to
        spare. A snapshot kept from an earlier Update counts as free.

        Raises:
            NotEnoughDiskSpaceError: says how much is needed and free.
        """
        needed = tree_size(self.venv) + tree_size(self.config_dir) + DISK_MARGIN_BYTES
        free = (
            shutil.disk_usage(self.venv.parent).free
            + tree_size(self.venv_copy)
            + tree_size(self.config_copy)
        )
        if free < needed:
            mib = 1024 * 1024
            raise NotEnoughDiskSpaceError(
                f"Not enough disk space: the Update needs {needed // mib} MiB "
                f"and {free // mib} MiB is free."
            )

    def take(self) -> None:
        """Copy the venv and the config dir, replacing any earlier copies.
        On failure, nothing is left half-copied."""
        self.remove()
        try:
            shutil.copytree(self.venv, self.venv_copy, symlinks=True)
            config = _open_dir(self.config_dir)
            try:
                _copy_out(config, self.config_copy)
            finally:
                os.close(config)
        except BaseException:
            self.remove()
            raise

    def restore(self) -> None:
        """Put the venv and the config dir back as they were; the copies stay.

        The venv is replaced whole. The config dir itself stays (the service
        user can't recreate it under ``/etc``): files that changed are put
        back, files the new version added are removed.
        """
        if not self.venv_copy.is_dir() or not self.config_copy.is_dir():
            raise FileNotFoundError("The snapshot is missing.")
        aside = self.venv.with_name(f"{self.venv.name}.failed")
        if aside.exists():
            shutil.rmtree(aside)
        if self.venv.exists():
            self.venv.rename(aside)
            shutil.rmtree(aside)
        shutil.copytree(self.venv_copy, self.venv, symlinks=True)
        config = _open_dir(self.config_dir)
        try:
            _sync_into(self.config_copy, config)
        finally:
            os.close(config)

    def remove(self) -> None:
        for copy in (self.venv_copy, self.config_copy):
            if copy.is_symlink() or copy.is_file():
                copy.unlink()
            elif copy.exists():
                shutil.rmtree(copy)


# ---------------------------------------------------------------------------
# The config dir, never following a link
# ---------------------------------------------------------------------------

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
"""``O_NONBLOCK``: a FIFO planted in the config dir can't hang the open."""
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_CHUNK = 1024 * 1024


def _open_dir(path: Path | str, dir_fd: int | None = None) -> int:
    """Open a directory, refusing a link (``ELOOP``/``ENOTDIR``)."""
    return os.open(path, _DIR_FLAGS, dir_fd=dir_fd)


def _copy_out(source_fd: int, target: Path) -> None:
    """Copy the directory open as ``source_fd`` (app-writable) to
    ``target`` (the updater's own): links are copied as links, never
    followed; anything but files, dirs and links is skipped."""
    info = os.fstat(source_fd)
    target.mkdir(mode=0o700)
    for entry in os.scandir(source_fd):
        name = entry.name
        if entry.is_symlink():
            os.symlink(os.readlink(name, dir_fd=source_fd), target / name)
        elif entry.is_dir(follow_symlinks=False):
            child = _open_dir(name, source_fd)
            try:
                _copy_out(child, target / name)
            finally:
                os.close(child)
        elif entry.is_file(follow_symlinks=False):
            fd = os.open(name, _READ_FLAGS, dir_fd=source_fd)
            try:
                file_info = os.fstat(fd)
                if not stat.S_ISREG(file_info.st_mode):
                    continue  # swapped for something else mid-walk
                out = os.open(target / name, _CREATE_FLAGS, 0o600)
                try:
                    _copy_fd(fd, out)
                    os.fchmod(out, stat.S_IMODE(file_info.st_mode))
                    os.utime(out, ns=(file_info.st_atime_ns, file_info.st_mtime_ns))
                finally:
                    os.close(out)
            finally:
                os.close(fd)
    target.chmod(stat.S_IMODE(info.st_mode))
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))


def _sync_into(source: Path, target_fd: int) -> None:
    """Make the directory open as ``target_fd`` (app-writable) match
    ``source`` (the updater's own), touching only what differs.

    Every change goes through ``target_fd``: names are removed with
    ``unlink``/``rmdir`` (which never follow a link), subdirs entered with
    ``O_NOFOLLOW``, files written to a fresh temp name created with
    ``O_EXCL | O_NOFOLLOW``, then renamed into place.
    """
    wanted = {entry.name: entry for entry in os.scandir(source)}
    for entry in os.scandir(target_fd):
        theirs = wanted.get(entry.name)
        if theirs is None or _kind(theirs) != _kind(entry):
            _remove_at(target_fd, entry.name)
    for name, entry in wanted.items():
        if entry.is_symlink():
            _replace_at(
                target_fd, name, partial(_link_at, os.readlink(entry.path), target_fd)
            )
        elif entry.is_dir(follow_symlinks=False):
            mode = stat.S_IMODE(entry.stat(follow_symlinks=False).st_mode)
            try:
                os.mkdir(name, mode, dir_fd=target_fd)
            except FileExistsError:
                pass
            child = _open_dir(name, target_fd)
            try:
                _sync_into(Path(entry.path), child)
                os.fchmod(child, mode)
            finally:
                os.close(child)
        elif not _same_file(entry, target_fd, name):
            _replace_at(target_fd, name, partial(_write_copy, entry.path, target_fd))


def _kind(entry: os.DirEntry[str]) -> str:
    if entry.is_symlink():
        return "link"
    if entry.is_dir(follow_symlinks=False):
        return "dir"
    return "file"


def _remove_at(dir_fd: int, name: str) -> None:
    """Remove ``name`` in ``dir_fd``, a whole tree if it is a directory,
    without following any link."""
    try:
        os.unlink(name, dir_fd=dir_fd)
        return
    except IsADirectoryError:
        pass
    except PermissionError:
        # Linux answers EPERM, not EISDIR, for unlink() on a directory.
        if not stat.S_ISDIR(
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
        ):
            raise
    child = _open_dir(name, dir_fd)
    try:
        for entry in os.scandir(child):
            _remove_at(child, entry.name)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=dir_fd)


def _replace_at(dir_fd: int, name: str, create: Callable[[str], None]) -> None:
    """Create a fresh temp entry with ``create``, then rename it over
    ``name``; the temp name is unguessable and never pre-exists."""
    tmp = f".{name}.{secrets.token_hex(8)}.restore"
    create(tmp)
    try:
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        _remove_at(dir_fd, tmp)
        raise


def _link_at(link: str, dir_fd: int, tmp: str) -> None:
    os.symlink(link, tmp, dir_fd=dir_fd)


def _write_copy(source: str, dir_fd: int, tmp: str) -> None:
    """Copy ``source`` (the updater's own) into a new file ``tmp`` in
    ``dir_fd``, created with ``O_EXCL | O_NOFOLLOW``."""
    info = os.stat(source, follow_symlinks=False)
    out = os.open(tmp, _CREATE_FLAGS, 0o600, dir_fd=dir_fd)
    try:
        src = os.open(source, _READ_FLAGS)
        try:
            _copy_fd(src, out)
        finally:
            os.close(src)
        os.fchmod(out, stat.S_IMODE(info.st_mode))
        os.utime(out, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.fsync(out)
    finally:
        os.close(out)


def _same_file(entry: os.DirEntry[str], dir_fd: int, name: str) -> bool:
    """Whether ``name`` in ``dir_fd`` is a regular file with ``entry``'s
    content and mode, read without following a link."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        theirs = os.fstat(fd)
        ours = entry.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(theirs.st_mode)
            or theirs.st_size != ours.st_size
            or stat.S_IMODE(theirs.st_mode) != stat.S_IMODE(ours.st_mode)
        ):
            return False
        with open(entry.path, "rb") as mine:
            return _read_all(fd) == mine.read()
    finally:
        os.close(fd)


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(fd, _CHUNK):
        chunks.append(chunk)
    return b"".join(chunks)


def _copy_fd(source: int, target: int) -> None:
    while chunk := os.read(source, _CHUNK):
        view = memoryview(chunk)
        while view:
            view = view[os.write(target, view) :]
