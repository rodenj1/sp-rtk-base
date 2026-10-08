"""The snapshot an Update takes before pip runs, and restores on a Rollback.

The venv is copied to ``venv.prev`` beside it (the old version's code, which
the update unit's verify line runs from), and the config dir
(``/etc/sp-rtk-base``) to ``config.prev`` beside the venv, out of the app's
reach. See ADR 0005. Importable without the web app.
"""

from __future__ import annotations

import filecmp
import os
import shutil
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
            shutil.copytree(self.config_dir, self.config_copy, symlinks=True)
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
        _sync(self.config_copy, self.config_dir)

    def remove(self) -> None:
        for copy in (self.venv_copy, self.config_copy):
            if copy.is_symlink() or copy.is_file():
                copy.unlink()
            elif copy.exists():
                shutil.rmtree(copy)


def _sync(source: Path, target: Path) -> None:
    """Make ``target``'s contents match ``source``'s, touching only what
    differs."""
    wanted = {entry.name for entry in source.iterdir()}
    for entry in target.iterdir():
        if entry.name not in wanted:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    for entry in source.iterdir():
        there = target / entry.name
        if entry.is_dir() and not entry.is_symlink():
            if there.is_symlink() or (there.exists() and not there.is_dir()):
                there.unlink()
            if not there.exists():
                there.mkdir()
                shutil.copystat(entry, there)
            _sync(entry, there)
            continue
        if there.is_dir() and not there.is_symlink():
            shutil.rmtree(there)
        elif (
            there.exists()
            and not entry.is_symlink()
            and not there.is_symlink()
            and filecmp.cmp(entry, there, shallow=False)
        ):
            continue
        tmp = there.with_name(f".{there.name}.restore")
        tmp.unlink(missing_ok=True)
        shutil.copy2(entry, tmp, follow_symlinks=False)
        os.replace(tmp, there)
