"""The snapshot an Update restores on a Rollback never follows links in the
config dir (ADR 0005, sp-rtk-base#235).

``/etc/sp-rtk-base`` is writable by the app; the updater can also write
``/opt/sp-rtk-base``. A compromised app must not be able to steer the
updater's writes (or reads) out of the config dir with a symlink, however
it times the swap. Here a hostile process keeps planting symlinks to a
file and a directory standing in for ``/opt`` while the snapshot is taken
and restored.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from sp_rtk_base.update.snapshot import Snapshot

ROUNDS = 150


class Base:
    def __init__(self, root: Path) -> None:
        self.venv = root / "venv"
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "installed").write_text("0.9.0\n")
        self.config = root / "etc"
        (self.config / "profiles").mkdir(parents=True)
        (self.config / "config.yaml").write_text("mode: appliance\n")
        (self.config / "profiles" / "home.yaml").write_text("name: home\n")
        self.opt = root / "opt"
        """Stands in for what the updater can write and the app can't."""
        self.opt.mkdir()
        self.victim = self.opt / "victim"
        self.victim.write_text("untouched\n")
        self.snapshot = Snapshot(self.venv, self.config)


@pytest.fixture()
def base(tmp_path: Path) -> Base:
    return Base(tmp_path)


@pytest.fixture()
def hostile_app(base: Base) -> Iterator[None]:
    """Keeps planting symlinks at every name the updater might write, in
    a loop, as a compromised app racing the restore would."""
    names = [
        f"{prefix}{name}{suffix}"
        for name in ("config.yaml", "home.yaml", "new.yaml")
        for prefix, suffix in ((".", ".restore"), ("", ""))
    ]
    targets = [
        str(where / name)
        for where in (base.config, base.config / "profiles")
        for name in names
    ]
    script = (
        "import os, sys\n"
        "victim, targets = sys.argv[1], sys.argv[2:]\n"
        "while True:\n"
        "    for target in targets:\n"
        "        try:\n"
        "            os.symlink(victim, target)\n"
        "        except OSError:\n"
        "            pass\n"
    )
    app = subprocess.Popen([sys.executable, "-c", script, str(base.victim), *targets])
    try:
        yield
    finally:
        app.kill()
        app.wait()


def _differ(base: Base, round_: int) -> None:
    """The new version changed the config, so the restore must write it."""
    for path in (base.config / "config.yaml", base.config / "profiles" / "home.yaml"):
        try:
            if path.is_symlink():
                path.unlink()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW)
        except OSError:
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(f"migrated {round_}\n")


class TestRestore:
    @pytest.mark.usefixtures("hostile_app")
    def test_never_writes_through_a_planted_link(self, base: Base) -> None:
        base.snapshot.take()
        (base.snapshot.config_copy / "profiles" / "new.yaml").write_text("x\n")

        for round_ in range(ROUNDS):
            _differ(base, round_)
            try:
                base.snapshot.restore()
            except OSError:
                pass  # a race may fail the restore; it must never escape

        assert base.victim.read_text() == "untouched\n"

    def test_puts_the_config_back(self, base: Base) -> None:
        base.snapshot.take()
        _differ(base, 1)
        (base.config / "added.yaml").write_text("new\n")
        (base.config / "profiles").rename(base.config / "moved")
        (base.config / "profiles").symlink_to(base.opt)

        base.snapshot.restore()

        assert (base.config / "config.yaml").read_text() == "mode: appliance\n"
        assert (base.config / "profiles" / "home.yaml").read_text() == "name: home\n"
        assert not (base.config / "profiles").is_symlink()
        assert not (base.config / "added.yaml").exists()
        assert not (base.config / "moved").exists()
        assert sorted(os.listdir(base.opt)) == ["victim"]

    def test_keeps_file_modes(self, base: Base) -> None:
        (base.config / "config.yaml").chmod(0o600)
        base.snapshot.take()
        (base.config / "config.yaml").chmod(0o644)
        _differ(base, 1)

        base.snapshot.restore()

        assert (base.config / "config.yaml").stat().st_mode & 0o777 == 0o600

    def test_restores_a_link_the_config_had_as_a_link(self, base: Base) -> None:
        (base.config / "link.yaml").symlink_to("config.yaml")
        base.snapshot.take()
        (base.config / "link.yaml").unlink()

        base.snapshot.restore()

        assert os.readlink(base.config / "link.yaml") == "config.yaml"


class TestTake:
    def test_copies_a_link_as_a_link(self, base: Base) -> None:
        (base.config / "evil").symlink_to(base.victim)

        base.snapshot.take()

        copy = base.snapshot.config_copy / "evil"
        assert copy.is_symlink()
        assert os.readlink(copy) == str(base.victim)

    @pytest.mark.usefixtures("hostile_app")
    def test_never_reads_through_a_planted_link(self, base: Base) -> None:
        for _ in range(ROUNDS // 3):
            _differ(base, 0)
            try:
                base.snapshot.take()
            except OSError:
                continue
            for path in base.snapshot.config_copy.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    assert path.read_text() != "untouched\n"
