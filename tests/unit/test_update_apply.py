"""The updater, ``sp-rtk-base-apply-update``: it installs from a request file
(sp-rtk-base #239), checks the new version is healthy and rolls back when it
isn't (#241, ADR 0005).

Runs against a temporary venv (whose ``bin/pip`` is fake: it records what
it was asked to install and writes the installed versions into the venv),
a temporary config dir and update directory, PyPI as recorded plus the
releases a test publishes, and a fake ``/api/health`` that answers as the
venv on disk would run. ``TestTheUnit`` runs the ``ExecStart`` and
``ExecStopPost`` lines of ``deploy/sp-rtk-base-update.service`` themselves,
in order and with oneshot semantics, against a fake ``systemctl``.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sp_rtk_base.update.apply import main
from sp_rtk_base.update.health import app_health_url, systemd_health_url
from sp_rtk_base.update.state import UpdateFiles, UpdateRequest, UpdateStatus, Versions
from tests.fixtures.fake_pypi import FakePyPI

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_UNIT = REPO_ROOT / "deploy" / "sp-rtk-base-update.service"
PATH_UNIT = REPO_ROOT / "deploy" / "sp-rtk-base-update.path"

TARGET_APP = "0.10.1"
TARGET_RELAY = "4.2.0"
TARGET = Versions(app=TARGET_APP, relay=TARGET_RELAY)
# What runs in this test environment: the dev install.
RUNNING = Versions(app="0.9.0", relay="4.1.0")
NEWER_RELEASE = "A newer release appeared; check again."
PLUMBING_FILE = (
    "raw.githubusercontent.com/rodenj1/sp-rtk-base/v0.10.1/deploy/plumbing-version"
)
INSTALL_COMMAND = (
    "curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/"
    "deploy/install.sh | sudo bash"
)

CONFIG = "deployment:\n  mode: appliance\n"

# The health check's 90 s and 30 s, shortened.
HEALTH_TIMEOUT_S = "1.5"
HEALTH_HOLD_S = "0.3"


class FakeHealth:
    """``/api/health`` as the app installed in the venv would answer it.

    A version in :attr:`broken` never answers; one in :attr:`dies` answers
    once, then stops; one in :attr:`crash_loops` answers, but systemd's
    ``NRestarts`` goes up with every request; one in :attr:`wrong_relay`
    reports another Relay.
    """

    def __init__(self, venv: Path, nrestarts: Path) -> None:
        self.venv = venv
        self.nrestarts = nrestarts
        self.broken: set[str] = set()
        self.dies: set[str] = set()
        self.crash_loops: set[str] = set()
        self.wrong_relay: set[str] = set()
        self.answered: dict[str, int] = {}
        health = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                code, body = health.answer()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/api/health"

    def answer(self) -> tuple[int, bytes]:
        app, relay = (self.venv / "installed").read_text().split()
        count = self.answered.get(app, 0)
        if app in self.broken or (app in self.dies and count >= 1):
            return 503, b'{"detail": "starting"}'
        self.answered[app] = count + 1
        if app in self.crash_loops:
            self.nrestarts.write_text(str(int(self.nrestarts.read_text()) + 1))
        if app in self.wrong_relay:
            relay = "0.0.1"
        body = {"status": "ok", "version": app, "relay_version": relay}
        return 200, json.dumps(body).encode()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Host:
    """A temporary venv, config dir, update directory, PyPI, health endpoint
    and ``systemctl`` for one updater run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.venv = root / "venv"
        self.venv_prev = root / "venv.prev"
        self.config_dir = root / "etc"
        self.update_dir = root / "update"
        self.pypi_dir = root / "pypi"
        self.pip_log = root / "pip-calls"
        self.systemctl_log = root / "systemctl-calls"
        self.nrestarts = root / "nrestarts"
        self.nrestarts.write_text("0")
        self.app_environment = root / "app-environment"
        """What ``systemctl show --value -p Environment sp-rtk-base.service``
        prints."""
        self.app_environment.write_text("SP_RTK_BASE_CONFIG=/etc/x\n")
        self.files = UpdateFiles(self.update_dir)
        self.phases: list[str] = []
        """Every phase written to status.json, in order."""
        self.pypi = FakePyPI()
        self.pypi.publish_app(TARGET_APP)
        self.pypi.publish_relay(TARGET_RELAY)
        self.required_plumbing: int | None = 1
        """The target tag's ``deploy/plumbing-version``; ``None``: no file."""
        self.host_plumbing = "1"
        """``SP_RTK_BASE_PLUMBING`` in the unit (``install.sh`` writes it)."""
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "installed").write_text(f"{RUNNING.app} {RUNNING.relay}\n")
        python = self.venv / "bin" / "python"
        python.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
        python.chmod(0o755)
        self.config_dir.mkdir()
        (self.config_dir / "config.yaml").write_text(CONFIG)
        (self.config_dir / "profiles").mkdir()
        (self.config_dir / "profiles" / "home.yaml").write_text("name: home\n")
        self.pip(fails=False)
        self.health = FakeHealth(self.venv, self.nrestarts)
        self.systemctl = root / "systemctl"
        self.systemctl_fails(False)

    def pip(self, *, fails: bool, rewrites_config: bool = False) -> None:
        """The fake pip installs into the venv on disk (and, as a release
        that migrates the config would, rewrites ``config.yaml``)."""
        pip = self.venv / "bin" / "pip"
        pip.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "$*" >> "{self.pip_log}"\n'
            'for a in "$@"; do case "$a" in\n'
            '  sp-rtk-base==*) app="${a#*==}";;\n'
            '  sp-rtk-base-relay==*) relay="${a#*==}";;\n'
            "esac; done\n"
            f'echo "$app $relay" > "{self.venv}/installed"\n'
            + (
                f'echo "migrated: true" >> "{self.config_dir}/config.yaml"\n'
                if rewrites_config
                else ""
            )
            + ('echo "ERROR: No matching distribution" >&2\nexit 1\n' if fails else "")
        )
        pip.chmod(0o755)

    def systemctl_fails(self, fail: bool) -> None:
        self.systemctl.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = show ]; then case "$*" in\n'
            f'  *Environment*) cat "{self.app_environment}";;\n'
            f'  *) cat "{self.nrestarts}";;\n'
            "esac; exit 0; fi\n"
            f'echo "$*" >> "{self.systemctl_log}"\n' + ("exit 1\n" if fail else "")
        )
        self.systemctl.chmod(0o755)

    @property
    def pip_calls(self) -> list[str]:
        if not self.pip_log.exists():
            return []
        return self.pip_log.read_text().splitlines()

    @property
    def systemctl_calls(self) -> list[str]:
        if not self.systemctl_log.exists():
            return []
        return self.systemctl_log.read_text().splitlines()

    @property
    def installed(self) -> Versions:
        app, relay = (self.venv / "installed").read_text().split()
        return Versions(app=app, relay=relay)

    @property
    def config(self) -> str:
        return (self.config_dir / "config.yaml").read_text()

    def env(self) -> dict[str, str]:
        self.pypi.write_to(self.pypi_dir)
        plumbing = self.pypi_dir / PLUMBING_FILE
        plumbing.parent.mkdir(parents=True, exist_ok=True)
        if self.required_plumbing is None:
            plumbing.unlink(missing_ok=True)
        else:
            plumbing.write_text(f"{self.required_plumbing}\n")
        return {
            "SP_RTK_BASE_PLUMBING": self.host_plumbing,
            "SP_RTK_BASE_UPDATE_DIR": str(self.update_dir),
            "SP_RTK_BASE_UPDATE_VENV": str(self.venv),
            "SP_RTK_BASE_UPDATE_CONFIG_DIR": str(self.config_dir),
            "SP_RTK_BASE_FAKE_PYPI_DIR": str(self.pypi_dir),
            "SP_RTK_BASE_UPDATE_HEALTH_URL": self.health.url,
            "SP_RTK_BASE_UPDATE_HEALTH_TIMEOUT_S": HEALTH_TIMEOUT_S,
            "SP_RTK_BASE_UPDATE_HEALTH_HOLD_S": HEALTH_HOLD_S,
            "SP_RTK_BASE_UPDATE_SYSTEMCTL": str(self.systemctl),
        }

    def request(self, app: str = TARGET_APP, relay: str = TARGET_RELAY) -> None:
        self.files.write_request(UpdateRequest(app=app, relay=relay))

    def status(self) -> UpdateStatus:
        status = self.files.read_status()
        assert status is not None
        return status

    def write_status(self, phase: str, **fields: object) -> None:
        self.files.write_status(
            UpdateStatus.model_validate(
                {"phase": phase, "from": RUNNING.model_dump(), **fields}
            )
        )


@pytest.fixture()
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    h = Host(tmp_path)
    real_write = UpdateFiles.write_status

    def recording_write(self: UpdateFiles, status: UpdateStatus) -> None:
        h.phases.append(status.phase)
        real_write(self, status)

    monkeypatch.setattr(UpdateFiles, "write_status", recording_write)
    yield h
    h.health.close()
    h.root.chmod(0o755)


def run(host: Host, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    for key, value in host.env().items():
        monkeypatch.setenv(key, value)
    return main(list(argv))


def free_disk(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    """The filesystem the venv is on has ``free`` bytes left."""
    usage = shutil.disk_usage("/")
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage._replace(free=free))


not_as_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the permissions that make the restore fail",
)


class TestAValidRequest:
    def test_installs_exactly_the_target(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()

        assert run(host, monkeypatch) == 0

        assert len(host.pip_calls) == 1
        args = host.pip_calls[0].split()
        assert args[0] == "install"
        assert args[-2:] == ["sp-rtk-base==0.10.1", "sp-rtk-base-relay==4.2.0"]

    def test_reports_each_phase_up_to_the_restart(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()

        run(host, monkeypatch)

        assert host.phases == ["resolving", "installing", "restarting"]
        status = host.status()
        assert status.from_ == RUNNING
        assert status.to == TARGET
        assert status.error is None

    def test_consumes_the_request(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()

        run(host, monkeypatch)

        assert not host.files.request_path.exists()

    def test_snapshots_the_venv_and_the_config_before_pip(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()
        host.pip(fails=False, rewrites_config=True)

        run(host, monkeypatch)

        assert host.installed == TARGET
        assert (host.venv_prev / "installed").read_text().split() == [
            RUNNING.app,
            RUNNING.relay,
        ]
        assert "migrated" in host.config


class TestRefusals:
    """A refusal before pip installs nothing and stops the unit before its
    restart lines (a non-zero exit)."""

    @pytest.mark.parametrize(
        ("app", "relay"),
        [("0.10.0", TARGET_RELAY), (TARGET_APP, "4.1.0"), ("9.9.9", "9.9.9")],
    )
    def test_a_request_for_anything_but_the_target(
        self, host: Host, monkeypatch: pytest.MonkeyPatch, app: str, relay: str
    ) -> None:
        host.request(app=app, relay=relay)

        assert run(host, monkeypatch) != 0

        assert host.pip_calls == []
        status = host.status()
        assert status.phase == "failed"
        assert status.error == NEWER_RELEASE
        assert status.reason == "newer_release"
        assert status.to == TARGET
        assert not status.rolled_back
        assert not host.files.request_path.exists()

    def test_a_newer_release_published_after_the_operator_read_the_notes(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()
        host.pypi.publish_app("0.10.2")

        assert run(host, monkeypatch) != 0

        assert host.pip_calls == []
        assert host.status().error == NEWER_RELEASE

    def test_nothing_from_the_request_reaches_pip(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request(app="0.10.1 --index-url http://evil", relay=TARGET_RELAY)

        run(host, monkeypatch)

        assert host.pip_calls == []
        assert host.status().reason == "newer_release"

    def test_an_unreadable_request(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.update_dir.mkdir()
        host.files.request_path.write_text('{"app": 1}')

        assert run(host, monkeypatch) != 0

        assert host.pip_calls == []
        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "bad_request"
        assert status.error
        assert not host.files.request_path.exists()

    def test_pypi_failing(self, host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
        host.request()
        host.pypi.down.add("https://pypi.org/pypi/sp-rtk-base/json")
        del host.pypi.docs["https://pypi.org/pypi/sp-rtk-base/json"]

        assert run(host, monkeypatch) != 0

        assert host.pip_calls == []
        status = host.status()
        assert host.phases == ["resolving", "failed"]
        assert status.reason == "check_failed"
        assert status.error
        assert status.from_ == RUNNING
        assert status.to is None

    def test_no_request_does_nothing(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trigger without a request (a second start, a stray one) must
        not go on to the restart lines."""
        assert run(host, monkeypatch) != 0

        assert host.pip_calls == []
        assert host.files.read_status() is None


class TestHostSetup:
    """The updater checks the target's Host setup itself, so a stale page
    can't get past it (sp-rtk-base#242)."""

    def test_a_target_needing_newer_host_setup_is_refused(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.required_plumbing = 2
        host.request()

        assert run(host, monkeypatch) != 0

        assert host.pip_calls == []
        status = host.status()
        assert host.phases == ["resolving", "failed"]
        assert status.reason == "host_setup"
        assert INSTALL_COMMAND in (status.error or "")
        assert status.to == Versions(app="0.10.1", relay="4.2.0")
        assert not host.files.request_path.exists()

    def test_a_host_without_the_plumbing_line_has_0(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.host_plumbing = ""
        host.request()

        assert run(host, monkeypatch) != 0

        assert host.pip_calls == []
        assert host.status().reason == "host_setup"

    def test_a_requirement_that_cant_be_read_is_refused(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.required_plumbing = None
        host.request()

        assert run(host, monkeypatch) != 0

        assert host.pip_calls == []
        status = host.status()
        assert status.reason == "host_requirements"
        assert status.error is not None
        assert status.error.startswith(
            "Couldn't check this release's host requirements"
        )

    @pytest.mark.parametrize(("required", "has"), [(1, "1"), (0, "1"), (2, "3")])
    def test_a_host_with_enough_setup_installs(
        self, host: Host, monkeypatch: pytest.MonkeyPatch, required: int, has: str
    ) -> None:
        host.required_plumbing = required
        host.host_plumbing = has
        host.request()

        assert run(host, monkeypatch) == 0

        assert len(host.pip_calls) == 1


class TestDiskSpace:
    def test_too_little_refuses_before_anything_changes(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()
        free_disk(monkeypatch, 1024)

        assert run(host, monkeypatch) != 0

        assert host.pip_calls == []
        assert not host.venv_prev.exists()
        assert host.phases == ["resolving", "failed"]
        status = host.status()
        assert status.reason == "no_disk_space"
        assert status.error is not None
        assert "not enough disk space" in status.error.lower()
        assert status.to == TARGET
        assert not status.rolled_back
        assert status.finished_at is not None

    def test_enough_goes_ahead(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()
        free_disk(monkeypatch, 10 * 1024**3)

        assert run(host, monkeypatch) == 0

        assert host.installed == TARGET


class TestPipFailing:
    def test_restores_the_snapshot_and_reports_rolled_back(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()
        host.pip(fails=True, rewrites_config=True)

        assert run(host, monkeypatch) != 0

        assert host.phases == ["resolving", "installing", "failed"]
        status = host.status()
        assert status.reason == "install_failed"
        assert status.error is not None
        assert "No matching distribution" in status.error
        assert status.rolled_back
        assert status.finished_at is not None
        assert host.installed == RUNNING
        assert host.config == CONFIG
        assert (host.config_dir / "profiles" / "home.yaml").exists()
        # The old app never stopped, so the restored version is the healthy
        # one already running: the snapshot goes.
        assert not host.venv_prev.exists()

    @not_as_root
    def test_a_failed_restore_keeps_the_snapshot_and_both_errors(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()
        host.pip(fails=True)
        pip = host.venv / "bin" / "pip"
        # pip leaves the venv's parent read-only, so the venv can't be put back.
        pip.write_text(
            pip.read_text().replace("exit 1", f'chmod 555 "{host.root}"\nexit 1')
        )

        assert run(host, monkeypatch) != 0

        host.root.chmod(0o755)
        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "install_failed"
        assert not status.rolled_back
        assert status.error is not None
        assert "No matching distribution" in status.error
        assert status.rollback_error
        assert host.venv_prev.exists()


class TestSnapshotFailing:
    @not_as_root
    def test_an_unreadable_config_changes_nothing(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()
        secret = host.config_dir / "secret.yaml"
        secret.write_text("password: x\n")
        secret.chmod(0)

        try:
            assert run(host, monkeypatch) != 0
        finally:
            secret.chmod(0o600)

        assert host.pip_calls == []
        assert not host.venv_prev.exists()
        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "snapshot_failed"
        assert not status.rolled_back


class TestACrash:
    """#239's gap: a crash after the request was taken still ends in
    ``failed``."""

    def test_unreadable_installed_metadata(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()

        def broken(_name: str) -> str:
            raise importlib.metadata.PackageNotFoundError("sp-rtk-base")

        monkeypatch.setattr(importlib.metadata, "version", broken)

        assert run(host, monkeypatch) != 0

        assert host.pip_calls == []
        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "stopped"
        assert status.error is not None
        assert "sp-rtk-base" in status.error
        assert not host.files.request_path.exists()


class TestVerify:
    """``--verify``, after the unit restarted the app: the new version is
    healthy, or it is replaced by the snapshot."""

    def _restarted(self, host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
        host.request()
        assert run(host, monkeypatch) == 0
        host.phases.clear()

    def test_a_healthy_version_is_done(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._restarted(host, monkeypatch)

        assert run(host, monkeypatch, "--verify") == 0

        assert host.phases == ["verifying", "done"]
        status = host.status()
        assert status.from_ == RUNNING
        assert status.to == TARGET
        assert status.finished_at is not None
        assert not status.rolled_back
        assert not host.files.rollback_marker_path.exists()

    @pytest.mark.parametrize("fault", ["broken", "dies", "crash_loops", "wrong_relay"])
    def test_an_unhealthy_version_is_rolled_back(
        self, host: Host, monkeypatch: pytest.MonkeyPatch, fault: str
    ) -> None:
        self._restarted(host, monkeypatch)
        getattr(host.health, fault).add(TARGET_APP)

        assert run(host, monkeypatch, "--verify") != 0

        assert host.phases == ["verifying", "rolling_back"]
        status = host.status()
        assert status.reason == "failed_to_start"
        assert status.error is not None
        assert TARGET_APP in status.error
        assert host.installed == RUNNING
        assert host.files.rollback_marker_path.exists()

    def test_a_snapshot_that_cant_be_restored_is_a_double_failure(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._restarted(host, monkeypatch)
        host.health.broken.add(TARGET_APP)
        shutil.rmtree(host.root / "config.prev")

        assert run(host, monkeypatch, "--verify") != 0

        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "failed_to_start"
        assert not status.rolled_back
        assert status.error is not None
        assert TARGET_APP in status.error
        assert status.rollback_error is not None
        assert not host.files.rollback_marker_path.exists()
        assert host.venv_prev.exists()

    @pytest.mark.parametrize("phase", ["requested", "installing", "failed", "done"])
    def test_only_verifies_an_update_that_restarted(
        self, host: Host, monkeypatch: pytest.MonkeyPatch, phase: str
    ) -> None:
        host.write_status(phase)

        assert run(host, monkeypatch, "--verify") != 0

        assert host.status().phase == phase

    def test_no_status_at_all(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert run(host, monkeypatch, "--verify") != 0
        assert host.files.read_status() is None


class TestTheHealthCheckFindsTheApp:
    """The health check asks the app where systemd started it: a drop-in
    can move it off port 8080 (docs/deployment-pi.md)."""

    def _restarted_at(
        self, host: Host, monkeypatch: pytest.MonkeyPatch, environment: str
    ) -> None:
        host.request()
        assert run(host, monkeypatch) == 0
        host.app_environment.write_text(f"{environment}\n")
        monkeypatch.delenv("SP_RTK_BASE_UPDATE_HEALTH_URL", raising=False)

    def _verify(self, host: Host, monkeypatch: pytest.MonkeyPatch) -> int:
        env = host.env()
        del env["SP_RTK_BASE_UPDATE_HEALTH_URL"]
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return main(["--verify"])

    def test_on_the_port_its_unit_sets(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = host.health.server.server_address[1]
        self._restarted_at(
            host,
            monkeypatch,
            f"SP_RTK_BASE_CONFIG=/etc/x SP_RTK_BASE_PORT={port}",
        )

        assert self._verify(host, monkeypatch) == 0

        assert host.status().phase == "done"

    def test_on_the_address_its_unit_binds(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = host.health.server.server_address[1]
        self._restarted_at(
            host,
            monkeypatch,
            f"SP_RTK_BASE_HOST=127.0.0.1 SP_RTK_BASE_PORT={port}",
        )

        assert self._verify(host, monkeypatch) == 0

    @pytest.mark.parametrize(
        ("environment", "url"),
        [
            ({}, "http://127.0.0.1:8080/api/health"),
            ({"SP_RTK_BASE_PORT": "9090"}, "http://127.0.0.1:9090/api/health"),
            ({"SP_RTK_BASE_PORT": "nine"}, "http://127.0.0.1:8080/api/health"),
            ({"SP_RTK_BASE_HOST": "0.0.0.0"}, "http://127.0.0.1:8080/api/health"),
            ({"SP_RTK_BASE_HOST": "::"}, "http://[::1]:8080/api/health"),
            (
                {"SP_RTK_BASE_HOST": "192.168.4.1", "SP_RTK_BASE_PORT": "81"},
                "http://192.168.4.1:81/api/health",
            ),
        ],
    )
    def test_where_the_app_listens(self, environment: dict[str, str], url: str) -> None:
        assert app_health_url(environment) == url

    def test_systemd_unreadable_means_the_default(self, tmp_path: Path) -> None:
        assert (
            systemd_health_url(str(tmp_path / "no-systemctl"))
            == "http://127.0.0.1:8080/api/health"
        )


class TestStopped:
    """``--stopped`` runs after the unit stops, whatever happened: it checks
    a rolled-back version, clears the snapshot, and fails an Update left
    half-way."""

    @pytest.mark.parametrize("phase", ["resolving", "restarting", "verifying"])
    def test_an_unfinished_update_is_failed(
        self, host: Host, monkeypatch: pytest.MonkeyPatch, phase: str
    ) -> None:
        host.write_status(phase, to=None)
        monkeypatch.setenv("SERVICE_RESULT", "timeout")

        assert run(host, monkeypatch, "--stopped") == 0

        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "stopped"
        assert status.error is not None
        assert phase in status.error
        assert "timeout" in status.error
        assert status.from_ == RUNNING
        assert status.finished_at is not None

    def _rolled_back(self, host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
        host.request()
        assert run(host, monkeypatch) == 0
        host.health.broken.add(TARGET_APP)
        assert run(host, monkeypatch, "--verify") != 0

    def test_after_a_rollback_the_old_version_is_checked(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._rolled_back(host, monkeypatch)
        host.phases.clear()

        assert run(host, monkeypatch, "--stopped") == 0

        assert host.phases == ["failed"]
        status = host.status()
        assert status.reason == "failed_to_start"
        assert status.rolled_back
        assert status.rollback_error is None
        assert status.finished_at is not None
        assert status.from_ == RUNNING
        assert status.to == TARGET
        assert not host.venv_prev.exists()
        assert not host.files.rollback_marker_path.exists()

    def test_after_a_rollback_the_old_version_fails_too(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._rolled_back(host, monkeypatch)
        host.health.broken.add(RUNNING.app)

        assert run(host, monkeypatch, "--stopped") == 0

        status = host.status()
        assert status.phase == "failed"
        assert not status.rolled_back
        assert status.error is not None
        assert TARGET_APP in status.error
        assert status.rollback_error is not None
        assert RUNNING.app in status.rollback_error
        assert host.venv_prev.exists()

    def test_a_rollback_without_the_marker_is_not_checked_again(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The root line only restarts while the marker exists: without it
        the old version never restarted, so the Update stopped half-way."""
        self._rolled_back(host, monkeypatch)
        host.files.take_rollback_marker()

        assert run(host, monkeypatch, "--stopped") == 0

        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "stopped"
        assert host.health.answered.get(RUNNING.app, 0) == 0

    def test_stopped_while_installing_restores_the_snapshot(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()
        host.pip(fails=False, rewrites_config=True)
        run(host, monkeypatch)
        # As if pip had timed out half-way.
        host.write_status("installing", to=TARGET.model_dump())
        monkeypatch.setenv("SERVICE_RESULT", "timeout")

        assert run(host, monkeypatch, "--stopped") == 0

        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "stopped"
        assert status.rolled_back
        assert host.installed == RUNNING
        assert host.config == CONFIG
        assert not host.venv_prev.exists()

    @pytest.mark.parametrize("phase", ["requested", "done", "failed"])
    def test_leaves_other_phases_alone(
        self, host: Host, monkeypatch: pytest.MonkeyPatch, phase: str
    ) -> None:
        host.write_status(phase, error="kept")

        run(host, monkeypatch, "--stopped")

        assert host.status().phase == phase
        assert host.status().error == "kept"

    def test_no_status_at_all(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert run(host, monkeypatch, "--stopped") == 0
        assert host.files.read_status() is None

    def test_a_planted_marker_without_a_rollback_changes_nothing(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.write_status("done", to=TARGET.model_dump())
        host.files.mark_rollback()

        assert run(host, monkeypatch, "--stopped") == 0

        assert host.status().phase == "done"
        assert not host.files.rollback_marker_path.exists()


def test_the_updater_can_run_without_the_web_app() -> None:
    code = (
        "import sys; import sp_rtk_base.update.apply; "
        "loaded = [m for m in ('nicegui', 'fastapi', 'sp_rtk_base.services')"
        " if m in sys.modules]; assert not loaded, loaded"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_console_script_is_declared() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'sp-rtk-base-apply-update = "sp_rtk_base.update.apply:run"' in pyproject


# ---------------------------------------------------------------------------
# The unit itself
# ---------------------------------------------------------------------------


def _unit_lines(key: str) -> list[str]:
    return [
        line.split("=", 1)[1]
        for line in UPDATE_UNIT.read_text().splitlines()
        if line.startswith(f"{key}=")
    ]


def _prefixes(line: str) -> tuple[str, str]:
    """Split an Exec line's ``-``/``+`` prefixes from its command."""
    match = re.match(r"^([-+@:!]*)(.*)$", line)
    assert match is not None
    return match.group(1), match.group(2)


class TestTheUnit:
    """The update unit's ``Exec`` lines, run in order as systemd runs a
    oneshot (stop at the first failure not prefixed ``-``, then every
    ``ExecStopPost`` line), with the venv, the config dir, ``systemctl``,
    the health endpoint and the update directory faked."""

    def _systemd(self, host: Host) -> list[str]:
        """Run the unit; return the systemctl calls made (other than
        ``show``)."""
        shim = host.venv / "bin" / "sp-rtk-base-apply-update"
        shim.write_text(
            f'#!/usr/bin/env bash\nexec "{sys.executable}" -m '
            'sp_rtk_base.update.apply "$@"\n'
        )
        shim.chmod(0o755)
        if host.systemctl_log.exists():
            host.systemctl_log.unlink()
        env = {**os.environ, **host.env(), "PYTHONPATH": str(REPO_ROOT)}

        def to_command(command: str) -> list[str]:
            command = command.replace("/opt/sp-rtk-base/venv", str(host.venv))
            command = command.replace("/usr/bin/systemctl", str(host.systemctl))
            command = command.replace(
                "/var/lib/sp-rtk-base/update", str(host.update_dir)
            )
            return shlex.split(command)

        result = "success"
        for line in _unit_lines("ExecStart"):
            prefixes, command = _prefixes(line)
            done = subprocess.run(to_command(command), env=env, check=False)
            if done.returncode != 0 and "-" not in prefixes:
                result = "exit-code"
                break
        for line in _unit_lines("ExecStopPost"):
            prefixes, command = _prefixes(line)
            subprocess.run(
                to_command(command),
                env={**env, "SERVICE_RESULT": result},
                check="-" not in prefixes,
            )
        return host.systemctl_calls

    RESTARTS = [
        "restart sp-rtk-base.service",
        "try-restart sp-rtk-base-net-provision.service",
    ]

    def test_a_healthy_update_is_done(self, host: Host) -> None:
        host.request()

        calls = self._systemd(host)

        assert host.pip_calls[0].split()[-2:] == [
            "sp-rtk-base==0.10.1",
            "sp-rtk-base-relay==4.2.0",
        ]
        assert calls == self.RESTARTS
        status = host.status()
        assert status.phase == "done"
        assert status.to == TARGET
        assert host.installed == TARGET
        assert not host.files.request_path.exists()
        assert not host.venv_prev.exists()

    def test_a_refusal_restarts_nothing(self, host: Host) -> None:
        host.request(app="0.10.0")

        calls = self._systemd(host)

        assert calls == []
        assert host.pip_calls == []
        assert host.status().phase == "failed"
        assert host.status().error == NEWER_RELEASE

    def test_a_host_setup_refusal_restarts_nothing(self, host: Host) -> None:
        host.required_plumbing = 2
        host.request()

        calls = self._systemd(host)

        assert calls == []
        assert host.pip_calls == []
        assert host.status().reason == "host_setup"

    def test_a_pip_failure_restarts_nothing(self, host: Host) -> None:
        host.request()
        host.pip(fails=True, rewrites_config=True)

        calls = self._systemd(host)

        assert calls == []
        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "install_failed"
        assert status.rolled_back
        assert host.installed == RUNNING
        assert host.config == CONFIG

    def test_a_version_that_fails_to_start_is_rolled_back(self, host: Host) -> None:
        host.request()
        host.pip(fails=False, rewrites_config=True)
        host.health.broken.add(TARGET_APP)

        calls = self._systemd(host)

        # The new version's restart, then the old one's.
        assert calls == self.RESTARTS + self.RESTARTS
        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "failed_to_start"
        assert status.rolled_back
        assert status.rollback_error is None
        assert status.from_ == RUNNING
        assert status.to == TARGET
        assert status.finished_at is not None
        assert host.installed == RUNNING
        assert host.config == CONFIG
        # The old version was health-checked too.
        assert host.health.answered.get(RUNNING.app, 0) >= 2
        assert not host.files.rollback_marker_path.exists()
        assert not host.venv_prev.exists()

    def test_a_double_failure_stops_after_one_attempt(self, host: Host) -> None:
        host.request()
        host.health.broken.update({TARGET_APP, RUNNING.app})

        calls = self._systemd(host)

        assert calls == self.RESTARTS + self.RESTARTS
        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "failed_to_start"
        assert not status.rolled_back
        assert status.error is not None
        assert TARGET_APP in status.error
        assert status.rollback_error is not None
        assert RUNNING.app in status.rollback_error
        assert host.venv_prev.exists()
        assert not host.files.rollback_marker_path.exists()

    def test_a_failed_restart_is_judged_by_the_health_check(self, host: Host) -> None:
        host.request()
        host.systemctl_fails(True)
        host.health.broken.add(TARGET_APP)

        self._systemd(host)

        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "failed_to_start"
        assert status.rolled_back
        assert host.installed == RUNNING

    def test_a_second_trigger_without_a_request_restarts_nothing(
        self, host: Host
    ) -> None:
        host.request()
        self._systemd(host)

        calls = self._systemd(host)

        assert calls == []
        assert len(host.pip_calls) == 1
        assert host.status().phase == "done"

    def test_a_planted_marker_gains_only_a_restart(self, host: Host) -> None:
        host.write_status("done", to=TARGET.model_dump())
        host.files.mark_rollback()

        calls = self._systemd(host)

        assert calls == self.RESTARTS
        assert host.pip_calls == []
        assert host.status().phase == "done"
        assert not host.files.rollback_marker_path.exists()

    def test_verify_runs_the_old_code_from_the_snapshot(self) -> None:
        """A release that fails on import can't stop its own rollback."""
        (verify,) = [line for line in _unit_lines("ExecStart") if "--verify" in line]
        assert verify == (
            "/opt/sp-rtk-base/venv.prev/bin/python -I -m sp_rtk_base.update.apply "
            "--verify"
        )

    def test_only_the_restarts_run_as_root(self) -> None:
        privileged = [
            line
            for line in _unit_lines("ExecStart") + _unit_lines("ExecStopPost")
            if "+" in _prefixes(line)[0]
        ]
        assert privileged == [
            "-+/usr/bin/systemctl restart sp-rtk-base.service",
            "-+/usr/bin/systemctl try-restart sp-rtk-base-net-provision.service",
            "-+/bin/sh -c 'if [ -e /var/lib/sp-rtk-base/update/rollback ]; then "
            "/usr/bin/systemctl restart sp-rtk-base.service; "
            "/usr/bin/systemctl try-restart sp-rtk-base-net-provision.service; fi'",
        ]

    def test_runs_as_the_service_user_in_its_own_sandbox(self) -> None:
        text = UPDATE_UNIT.read_text()
        assert re.search(r"^Type=oneshot$", text, re.MULTILINE)
        assert re.search(r"^User=sp-rtk-base$", text, re.MULTILINE)
        assert re.search(r"^ProtectSystem=strict$", text, re.MULTILINE)
        assert re.search(r"^TimeoutStartSec=15min$", text, re.MULTILINE)
        # Room for the old version's 90 s + 30 s health check after a Rollback.
        assert re.search(r"^TimeoutStopSec=5min$", text, re.MULTILINE)
        (rw,) = _unit_lines("ReadWritePaths")
        assert sorted(rw.split()) == [
            "/etc/sp-rtk-base",
            "/opt/sp-rtk-base",
            "/var/lib/sp-rtk-base",
        ]
        assert "[Install]" not in text  # started only by the path unit

    def test_the_path_unit_watches_the_request_file(self) -> None:
        text = PATH_UNIT.read_text()
        assert re.search(
            r"^PathExists=/var/lib/sp-rtk-base/update/request\.json$",
            text,
            re.MULTILINE,
        )
        assert re.search(r"^Unit=sp-rtk-base-update\.service$", text, re.MULTILINE)
        assert "[Install]" in text
