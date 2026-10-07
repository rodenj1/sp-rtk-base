"""The updater, ``sp-rtk-base-apply-update``: it installs from a request file
(sp-rtk-base #239, ADR 0005).

Runs against a temporary venv (whose ``bin/pip`` is fake and records what
it was asked to install), a temporary update directory, and PyPI as
recorded plus the releases a test publishes. ``TestTheUnit`` runs the
``ExecStart`` lines of ``deploy/sp-rtk-base-update.service`` themselves,
in order and with oneshot semantics, against a fake ``systemctl``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from sp_rtk_base.update.apply import main
from sp_rtk_base.update.state import UpdateFiles, UpdateRequest, UpdateStatus, Versions
from tests.fixtures.fake_pypi import FakePyPI

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_UNIT = REPO_ROOT / "deploy" / "sp-rtk-base-update.service"
PATH_UNIT = REPO_ROOT / "deploy" / "sp-rtk-base-update.path"

TARGET_APP = "0.10.1"
TARGET_RELAY = "4.2.0"
# What runs in this test environment: the dev install.
RUNNING = Versions(app="0.9.0", relay="4.1.0")
NEWER_RELEASE = "A newer release appeared; check again."


class Host:
    """A temporary venv, update directory and PyPI for one updater run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.venv = root / "venv"
        self.update_dir = root / "update"
        self.pypi_dir = root / "pypi"
        self.pip_log = root / "pip-calls"
        self.files = UpdateFiles(self.update_dir)
        self.phases: list[str] = []
        """Every phase written to status.json, in order."""
        self.pypi = FakePyPI()
        self.pypi.publish_app(TARGET_APP)
        self.pypi.publish_relay(TARGET_RELAY)
        (self.venv / "bin").mkdir(parents=True)
        self.pip_fails(False)

    def pip_fails(self, fail: bool) -> None:
        pip = self.venv / "bin" / "pip"
        pip.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "$*" >> "{self.pip_log}"\n'
            + ('echo "ERROR: No matching distribution" >&2\nexit 1\n' if fail else "")
        )
        pip.chmod(0o755)

    @property
    def pip_calls(self) -> list[str]:
        if not self.pip_log.exists():
            return []
        return self.pip_log.read_text().splitlines()

    def env(self) -> dict[str, str]:
        self.pypi.write_to(self.pypi_dir)
        return {
            "SP_RTK_BASE_UPDATE_DIR": str(self.update_dir),
            "SP_RTK_BASE_UPDATE_VENV": str(self.venv),
            "SP_RTK_BASE_FAKE_PYPI_DIR": str(self.pypi_dir),
        }

    def request(self, app: str = TARGET_APP, relay: str = TARGET_RELAY) -> None:
        self.files.write_request(UpdateRequest(app=app, relay=relay))

    def status(self) -> UpdateStatus:
        status = self.files.read_status()
        assert status is not None
        return status


@pytest.fixture()
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    h = Host(tmp_path)
    real_write = UpdateFiles.write_status

    def recording_write(self: UpdateFiles, status: UpdateStatus) -> None:
        h.phases.append(status.phase)
        real_write(self, status)

    monkeypatch.setattr(UpdateFiles, "write_status", recording_write)
    return h


def run(host: Host, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    for key, value in host.env().items():
        monkeypatch.setenv(key, value)
    return main(list(argv))


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
        assert status.to == Versions(app="0.10.1", relay="4.2.0")
        assert status.error is None

    def test_consumes_the_request(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()

        run(host, monkeypatch)

        assert not host.files.request_path.exists()

    def test_finish_after_the_restart_reports_done(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()
        run(host, monkeypatch)

        assert run(host, monkeypatch, "--finish") == 0

        status = host.status()
        assert status.phase == "done"
        assert status.finished
        assert status.from_ == RUNNING
        assert status.to == Versions(app="0.10.1", relay="4.2.0")


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
        assert status.to == Versions(app="0.10.1", relay="4.2.0")
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


class TestPipFailing:
    def test_reports_failed_with_pips_error(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host.request()
        host.pip_fails(True)

        assert run(host, monkeypatch) != 0

        assert host.phases == ["resolving", "installing", "failed"]
        status = host.status()
        assert status.reason == "install_failed"
        assert status.error is not None
        assert "No matching distribution" in status.error


class TestStopped:
    """``--stopped`` runs after the unit stops, whatever happened: an
    Update it left half-way ends in ``failed``."""

    @pytest.mark.parametrize("phase", ["resolving", "installing", "restarting"])
    def test_an_unfinished_update_is_failed(
        self, host: Host, monkeypatch: pytest.MonkeyPatch, phase: str
    ) -> None:
        host.files.write_status(
            UpdateStatus.model_validate(
                {"phase": phase, "from": RUNNING.model_dump(), "to": None}
            )
        )
        monkeypatch.setenv("SERVICE_RESULT", "timeout")

        assert run(host, monkeypatch, "--stopped") == 0

        status = host.status()
        assert status.phase == "failed"
        assert status.reason == "stopped"
        assert status.error is not None
        assert phase in status.error
        assert "timeout" in status.error
        assert status.from_ == RUNNING

    @pytest.mark.parametrize("phase", ["requested", "done", "failed"])
    def test_leaves_other_phases_alone(
        self, host: Host, monkeypatch: pytest.MonkeyPatch, phase: str
    ) -> None:
        host.files.write_status(
            UpdateStatus.model_validate({"phase": phase, "error": "kept"})
        )

        run(host, monkeypatch, "--stopped")

        assert host.status().phase == phase
        assert host.status().error == "kept"

    def test_no_status_at_all(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert run(host, monkeypatch, "--stopped") == 0
        assert host.files.read_status() is None


class TestFinish:
    @pytest.mark.parametrize("phase", ["requested", "installing", "failed"])
    def test_only_finishes_an_update_that_restarted(
        self, host: Host, monkeypatch: pytest.MonkeyPatch, phase: str
    ) -> None:
        host.files.write_status(UpdateStatus.model_validate({"phase": phase}))

        assert run(host, monkeypatch, "--finish") != 0

        assert host.status().phase == phase

    def test_no_status_at_all(
        self, host: Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert run(host, monkeypatch, "--finish") != 0
        assert host.files.read_status() is None


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


class TestTheUnit:
    """The update unit's ``ExecStart`` lines, run in order as systemd runs
    a oneshot (stop at the first failure, then ``ExecStopPost``), with the
    venv, ``systemctl`` and the update directory faked."""

    def _systemd(self, host: Host, *, systemctl_fails: bool = False) -> list[str]:
        """Run the unit; return the systemctl calls made."""
        shim = host.venv / "bin" / "sp-rtk-base-apply-update"
        shim.write_text(
            f'#!/usr/bin/env bash\nexec "{sys.executable}" -m '
            'sp_rtk_base.update.apply "$@"\n'
        )
        shim.chmod(0o755)
        systemctl_log = host.root / "systemctl-calls"
        systemctl = host.root / "systemctl"
        systemctl.write_text(
            f'#!/usr/bin/env bash\necho "$*" >> "{systemctl_log}"\n'
            + ("exit 1\n" if systemctl_fails else "")
        )
        systemctl.chmod(0o755)
        env = {**os.environ, **host.env(), "PYTHONPATH": str(REPO_ROOT)}

        def to_command(line: str) -> list[str]:
            line = line.removeprefix("+")
            line = line.replace("/opt/sp-rtk-base/venv", str(host.venv))
            line = line.replace("/usr/bin/systemctl", str(systemctl))
            return line.split()

        result = "success"
        for line in _unit_lines("ExecStart"):
            done = subprocess.run(to_command(line), env=env, check=False)
            if done.returncode != 0:
                result = "exit-code"
                break
        for line in _unit_lines("ExecStopPost"):
            subprocess.run(
                to_command(line), env={**env, "SERVICE_RESULT": result}, check=True
            )
        if not systemctl_log.exists():
            return []
        return systemctl_log.read_text().splitlines()

    def test_a_valid_request_installs_and_restarts_the_app(self, host: Host) -> None:
        host.request()

        calls = self._systemd(host)

        assert host.pip_calls[0].split()[-2:] == [
            "sp-rtk-base==0.10.1",
            "sp-rtk-base-relay==4.2.0",
        ]
        assert calls == [
            "restart sp-rtk-base.service",
            "try-restart sp-rtk-base-net-provision.service",
        ]
        assert host.status().phase == "done"
        assert not host.files.request_path.exists()

    def test_a_refusal_restarts_nothing(self, host: Host) -> None:
        host.request(app="0.10.0")

        calls = self._systemd(host)

        assert calls == []
        assert host.pip_calls == []
        assert host.status().phase == "failed"
        assert host.status().error == NEWER_RELEASE

    def test_a_failed_restart_ends_in_failed(self, host: Host) -> None:
        host.request()

        self._systemd(host, systemctl_fails=True)

        status = host.status()
        assert status.phase == "failed"
        assert "restarting" in (status.error or "")

    def test_a_second_trigger_without_a_request_restarts_nothing(
        self, host: Host
    ) -> None:
        host.request()
        self._systemd(host)
        (host.root / "systemctl-calls").unlink()

        calls = self._systemd(host)

        assert calls == []
        assert len(host.pip_calls) == 1
        assert host.status().phase == "done"

    def test_only_the_restarts_run_as_root(self) -> None:
        privileged = [line for line in _unit_lines("ExecStart") if line.startswith("+")]
        assert privileged == [
            "+/usr/bin/systemctl restart sp-rtk-base.service",
            "+/usr/bin/systemctl try-restart sp-rtk-base-net-provision.service",
        ]
        assert not any(line.startswith("+") for line in _unit_lines("ExecStopPost"))

    def test_runs_as_the_service_user_in_its_own_sandbox(self) -> None:
        text = UPDATE_UNIT.read_text()
        assert re.search(r"^Type=oneshot$", text, re.MULTILINE)
        assert re.search(r"^User=sp-rtk-base$", text, re.MULTILINE)
        assert re.search(r"^ProtectSystem=strict$", text, re.MULTILINE)
        assert re.search(r"^TimeoutStartSec=15min$", text, re.MULTILINE)
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
