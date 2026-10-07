"""The request file and ``status.json``: what the app and the updater share
(sp-rtk-base #239, ADR 0005).

The app writes a request and reads ``status.json``; the updater takes the
request and writes ``status.json``. Both go through
:class:`sp_rtk_base.update.state.UpdateFiles`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sp_rtk_base.update.state import (
    UPDATE_DIR_ENV,
    UpdateFiles,
    UpdateRequest,
    UpdateStatus,
    Versions,
)


class TestRequest:
    def test_the_updater_takes_the_request_the_app_wrote(self, tmp_path: Path) -> None:
        files = UpdateFiles(tmp_path)
        files.write_request(UpdateRequest(app="0.10.1", relay="4.2.0"))

        taken = files.take_request()

        assert taken is not None
        assert (taken.app, taken.relay) == ("0.10.1", "4.2.0")

    def test_taking_the_request_consumes_it(self, tmp_path: Path) -> None:
        files = UpdateFiles(tmp_path)
        files.write_request(UpdateRequest(app="0.10.1", relay="4.2.0"))

        files.take_request()

        assert files.take_request() is None
        assert list(tmp_path.iterdir()) == []

    def test_no_request_is_none(self, tmp_path: Path) -> None:
        assert UpdateFiles(tmp_path).take_request() is None

    def test_an_unreadable_request_is_consumed_and_reported(
        self, tmp_path: Path
    ) -> None:
        files = UpdateFiles(tmp_path)
        files.request_path.write_text("not json")

        with pytest.raises(ValueError, match="request"):
            files.take_request()

        assert not files.request_path.exists()

    def test_the_request_is_written_whole_or_not_at_all(self, tmp_path: Path) -> None:
        """The path unit fires on the request's name, so it must only ever
        appear complete: written beside it, then renamed."""
        files = UpdateFiles(tmp_path)
        files.write_request(UpdateRequest(app="0.10.1", relay="4.2.0"))

        assert [p.name for p in tmp_path.iterdir()] == ["request.json"]
        assert files.request_path == tmp_path / "request.json"


class TestFormat:
    """Both formats are versioned and stay readable across one release:
    the old version's updater installs the new one."""

    def test_the_request_on_disk(self, tmp_path: Path) -> None:
        files = UpdateFiles(tmp_path)
        files.write_request(UpdateRequest(app="0.10.1", relay="4.2.0"))

        on_disk = json.loads(files.request_path.read_text())

        assert on_disk["format"] == 1
        assert on_disk["app"] == "0.10.1"
        assert on_disk["relay"] == "4.2.0"
        assert "requested_at" in on_disk

    def test_the_status_on_disk(self, tmp_path: Path) -> None:
        files = UpdateFiles(tmp_path)
        files.write_status(
            UpdateStatus(
                phase="failed",
                from_=Versions(app="0.9.0", relay="4.1.0"),
                to=Versions(app="0.10.1", relay="4.2.0"),
                error="pip exploded",
            )
        )

        on_disk = json.loads(files.status_path.read_text())

        assert files.status_path == tmp_path / "status.json"
        assert on_disk["format"] == 1
        assert on_disk["phase"] == "failed"
        assert on_disk["from"] == {"app": "0.9.0", "relay": "4.1.0"}
        assert on_disk["to"] == {"app": "0.10.1", "relay": "4.2.0"}
        assert on_disk["error"] == "pip exploded"
        assert "updated_at" in on_disk

    def test_fields_a_newer_release_adds_are_ignored(self, tmp_path: Path) -> None:
        files = UpdateFiles(tmp_path)
        files.status_path.write_text(
            json.dumps(
                {
                    "format": 2,
                    "phase": "done",
                    "from": {"app": "0.9.0", "relay": "4.1.0", "python": "3.11"},
                    "to": {"app": "0.10.1", "relay": "4.2.0"},
                    "error": None,
                    "something_new": True,
                }
            )
        )
        files.request_path.write_text(
            json.dumps({"format": 2, "app": "1.0.0", "relay": "5.0.0", "why": "x"})
        )

        status = files.read_status()
        request = files.take_request()

        assert status is not None
        assert status.phase == "done"
        assert status.from_ == Versions(app="0.9.0", relay="4.1.0")
        assert request is not None
        assert request.app == "1.0.0"


class TestStatus:
    def test_no_status_yet(self, tmp_path: Path) -> None:
        assert UpdateFiles(tmp_path).read_status() is None

    def test_an_unreadable_status_reads_as_none(self, tmp_path: Path) -> None:
        files = UpdateFiles(tmp_path)
        files.status_path.write_text("{")

        assert files.read_status() is None

    def test_the_latest_phase_replaces_the_last(self, tmp_path: Path) -> None:
        files = UpdateFiles(tmp_path)
        files.write_status(UpdateStatus(phase="resolving"))
        files.write_status(UpdateStatus(phase="installing"))

        status = files.read_status()

        assert status is not None
        assert status.phase == "installing"
        assert [p.name for p in tmp_path.iterdir()] == ["status.json"]

    def test_finished_phases(self) -> None:
        assert UpdateStatus(phase="done").finished
        assert UpdateStatus(phase="failed").finished
        assert not UpdateStatus(phase="requested").finished
        assert not UpdateStatus(phase="restarting").finished


class TestDirectory:
    def test_defaults_to_the_state_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(UPDATE_DIR_ENV, raising=False)

        assert UpdateFiles().directory == Path("/var/lib/sp-rtk-base/update")

    def test_the_env_var_overrides_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SP_RTK_BASE_UPDATE_DIR", str(tmp_path))

        assert UpdateFiles().directory == tmp_path

    def test_the_constructor_wins_over_the_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SP_RTK_BASE_UPDATE_DIR", "/elsewhere")

        assert UpdateFiles(tmp_path).directory == tmp_path

    def test_writing_creates_the_directory(self, tmp_path: Path) -> None:
        files = UpdateFiles(tmp_path / "update")

        files.write_request(UpdateRequest(app="0.10.1", relay="4.2.0"))

        assert files.request_path.exists()
