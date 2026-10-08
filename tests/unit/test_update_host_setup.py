"""Host setup: what the host has, and what a release needs (sp-rtk-base#242).

The host's side is read through ``systemctl show`` (faked here with the
answers a real systemd gives); the release's side is ``deploy/plumbing-version``
at the release's tag, read through the same fetch as the update check.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from sp_rtk_base.update.host_setup import (
    FAKE_HOST_SETUP_ENV,
    PLUMBING_VERSION,
    HostRequirementError,
    HostSetup,
    host_plumbing_from_env,
    host_setup_reader_from_env,
    read_host_setup,
    required_plumbing,
)
from tests.fixtures.fake_github import FakeGitHub, http_error

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUMBING_URL = (
    "https://raw.githubusercontent.com/rodenj1/sp-rtk-base/v0.10.1/"
    "deploy/plumbing-version"
)

# What systemd answers to ``systemctl show <unit> -p LoadState,UnitFileState,
# Environment`` (properties come back in systemd's order, not the asked one).
INSTALLED_SERVICE = (
    "Environment=SP_RTK_BASE_UPDATE_DIR=/var/lib/sp-rtk-base/update "
    "PIP_NO_CACHE_DIR=1 SP_RTK_BASE_PLUMBING=3\n"
    "LoadState=loaded\n"
    "UnitFileState=static\n"
)
MISSING = "Environment=\nLoadState=not-found\nUnitFileState=\n"
PATH_ENABLED = "Environment=\nLoadState=loaded\nUnitFileState=enabled\n"
PATH_DISABLED = "Environment=\nLoadState=loaded\nUnitFileState=disabled\n"


class FakeSystemctl:
    def __init__(self, service: str, path: str) -> None:
        self.answers = {
            "sp-rtk-base-update.service": service,
            "sp-rtk-base-update.path": path,
        }
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        assert args[0] == "show"
        return self.answers[args[1]]


class TestReadingTheHost:
    def test_a_set_up_host(self) -> None:
        host = read_host_setup(FakeSystemctl(INSTALLED_SERVICE, PATH_ENABLED))

        assert host == HostSetup(installed=True, enabled=True, plumbing=3)

    def test_the_path_unit_disabled_turns_update_off(self) -> None:
        host = read_host_setup(FakeSystemctl(INSTALLED_SERVICE, PATH_DISABLED))

        assert host == HostSetup(installed=True, enabled=False, plumbing=3)

    def test_a_masked_path_unit_turns_update_off(self) -> None:
        masked = PATH_DISABLED.replace("disabled", "masked")

        host = read_host_setup(FakeSystemctl(INSTALLED_SERVICE, masked))

        assert host.enabled is False

    def test_a_missing_unit_counts_as_0(self) -> None:
        host = read_host_setup(FakeSystemctl(MISSING, MISSING))

        assert host == HostSetup(installed=False, enabled=False, plumbing=0)

    def test_a_missing_path_unit_is_a_missing_setup(self) -> None:
        host = read_host_setup(FakeSystemctl(INSTALLED_SERVICE, MISSING))

        assert host.installed is False

    def test_a_unit_without_the_plumbing_line_counts_as_0(self) -> None:
        service = "Environment=PIP_NO_CACHE_DIR=1\nLoadState=loaded\n"

        host = read_host_setup(FakeSystemctl(service, PATH_ENABLED))

        assert host == HostSetup(installed=True, enabled=True, plumbing=0)

    def test_a_quoted_environment_still_reads(self) -> None:
        service = (
            'Environment="A=with space" SP_RTK_BASE_PLUMBING=2\nLoadState=loaded\n'
        )

        assert read_host_setup(FakeSystemctl(service, PATH_ENABLED)).plumbing == 2

    def test_a_garbled_plumbing_value_counts_as_0(self) -> None:
        service = "Environment=SP_RTK_BASE_PLUMBING=two\nLoadState=loaded\n"

        assert read_host_setup(FakeSystemctl(service, PATH_ENABLED)).plumbing == 0

    def test_an_unbalanced_quote_counts_as_0(self) -> None:
        service = 'Environment="A=b SP_RTK_BASE_PLUMBING=2\nLoadState=loaded\n'

        assert read_host_setup(FakeSystemctl(service, PATH_ENABLED)).plumbing == 0

    def test_the_real_systemctl_is_asked_read_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "calls"
        shim = tmp_path / "systemctl"
        shim.write_text(
            f'#!/usr/bin/env bash\necho "$*" >> "{log}"\n'
            'case "$2" in\n'
            '  *.service) printf "LoadState=loaded\\nEnvironment=SP_RTK_BASE_PLUMBING=5\\n";;\n'
            '  *.path) printf "LoadState=loaded\\nUnitFileState=enabled\\n";;\n'
            "esac\n"
        )
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")

        assert read_host_setup() == HostSetup(installed=True, enabled=True, plumbing=5)
        assert log.read_text().splitlines() == [
            "show sp-rtk-base-update.service -p LoadState,UnitFileState,Environment",
            "show sp-rtk-base-update.path -p LoadState,UnitFileState,Environment",
        ]

    def test_systemctl_failing_counts_as_no_setup(self) -> None:
        def broken(args: Sequence[str]) -> str:
            raise subprocess.CalledProcessError(1, ["systemctl", *args])

        assert read_host_setup(broken) == HostSetup(
            installed=False, enabled=False, plumbing=0
        )

    def test_no_systemctl_at_all_counts_as_no_setup(self) -> None:
        def absent(args: Sequence[str]) -> str:
            raise FileNotFoundError("systemctl")

        assert read_host_setup(absent).installed is False

    def test_asks_only_with_show(self) -> None:
        systemctl = FakeSystemctl(INSTALLED_SERVICE, PATH_ENABLED)

        read_host_setup(systemctl)

        assert {call[0] for call in systemctl.calls} == {"show"}


class TestTheUpdatersOwnEnvironment:
    def test_reads_the_unit_environment_line(self) -> None:
        assert host_plumbing_from_env({"SP_RTK_BASE_PLUMBING": "4"}) == 4

    def test_no_line_counts_as_0(self) -> None:
        assert host_plumbing_from_env({}) == 0

    def test_a_garbled_line_counts_as_0(self) -> None:
        assert host_plumbing_from_env({"SP_RTK_BASE_PLUMBING": "x"}) == 0


class TestWhatAReleaseNeeds:
    def test_reads_plumbing_version_at_the_release_tag(self) -> None:
        github = FakeGitHub()
        github.files[PLUMBING_URL] = b"2\n"

        assert required_plumbing(github, "0.10.1") == 2
        assert github.fetched == [PLUMBING_URL]

    def test_a_release_without_the_file_cant_be_read(self) -> None:
        with pytest.raises(HostRequirementError):
            required_plumbing(FakeGitHub(), "0.10.1")

    def test_github_failing_cant_be_read(self) -> None:
        github = FakeGitHub()
        github.files[PLUMBING_URL] = b"2\n"
        github.fail(PLUMBING_URL, 403)

        with pytest.raises(HostRequirementError):
            required_plumbing(github, "0.10.1")

    def test_a_garbled_file_cant_be_read(self) -> None:
        github = FakeGitHub()
        github.files[PLUMBING_URL] = b"<html>rate limited</html>"

        with pytest.raises(HostRequirementError):
            required_plumbing(github, "0.10.1")

    def test_a_negative_number_cant_be_read(self) -> None:
        github = FakeGitHub()
        github.files[PLUMBING_URL] = b"-1\n"

        with pytest.raises(HostRequirementError):
            required_plumbing(github, "0.10.1")

    def test_the_error_says_what_failed(self) -> None:
        def down(url: str) -> bytes:
            raise http_error(url, 503)

        with pytest.raises(HostRequirementError, match="plumbing-version"):
            required_plumbing(down, "0.10.1")


def test_the_running_apps_requirement_is_the_repos_plumbing_version() -> None:
    """The package's own number is the one ``install.sh`` writes."""
    on_disk = (REPO_ROOT / "deploy" / "plumbing-version").read_text().strip()
    assert int(on_disk) == PLUMBING_VERSION


class TestTheFakeHost:
    """e2e plays the host by writing a JSON file the app reads each time."""

    def test_no_env_reads_systemctl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FAKE_HOST_SETUP_ENV, raising=False)

        assert host_setup_reader_from_env() is read_host_setup

    def test_no_file_yet_is_a_host_set_up_for_this_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FAKE_HOST_SETUP_ENV, str(tmp_path / "host.json"))

        assert host_setup_reader_from_env()() == HostSetup(
            installed=True, enabled=True, plumbing=PLUMBING_VERSION
        )

    def test_the_file_is_read_on_every_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "host.json"
        monkeypatch.setenv(FAKE_HOST_SETUP_ENV, str(path))
        read = host_setup_reader_from_env()
        path.write_text(
            json.dumps({"installed": False, "enabled": False, "plumbing": 0})
        )

        assert read() == HostSetup(installed=False, enabled=False, plumbing=0)

        path.write_text(json.dumps({"enabled": False, "plumbing": 1}))

        assert read() == HostSetup(installed=True, enabled=False, plumbing=1)
