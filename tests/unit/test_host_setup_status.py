"""What Settings says about Host setup (sp-rtk-base#242, story 24): a host
without the Update units, or with Update turned off, says so whether or
not an update is available; otherwise drift is warned of."""

from __future__ import annotations

import pytest

from sp_rtk_base.ui.host_setup_status import DRIFT_TEXT, host_setup_notice
from sp_rtk_base.update.host_setup import HostSetup

INSTALL_COMMAND = (
    "curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/"
    "deploy/install.sh | sudo bash"
)
MISSING_TEXT = (
    "Update needs a one-time setup on this host. Run this on the base, then come back:"
)
TURNED_OFF_TEXT = "Update is turned off on this host."


def _host(plumbing: int, *, installed: bool = True, enabled: bool = True) -> HostSetup:
    return HostSetup(installed=installed, enabled=enabled, plumbing=plumbing)


class TestWithoutAnAvailableUpdate:
    """No refusal is worked out: the notice alone tells the operator."""

    def test_a_pre_update_base_is_asked_for_the_one_time_setup(self) -> None:
        notice = host_setup_notice(_host(0, installed=False, enabled=False))

        assert notice is not None
        assert notice.text == MISSING_TEXT
        assert notice.command == INSTALL_COMMAND
        assert notice.test_id == "update-host-setup"

    def test_update_turned_off(self) -> None:
        notice = host_setup_notice(_host(1, enabled=False))

        assert notice is not None
        assert notice.text == TURNED_OFF_TEXT
        assert notice.command is None

    def test_missing_and_turned_off_come_before_drift(self) -> None:
        notice = host_setup_notice(
            _host(0, installed=False, enabled=False), running_requires=2
        )

        assert notice is not None
        assert notice.text == MISSING_TEXT


class TestDrift:
    def test_warns_when_the_running_app_needs_newer_host_setup(self) -> None:
        notice = host_setup_notice(_host(1), running_requires=2)

        assert notice is not None
        assert notice.text == (
            "This version needs newer Host setup than the host has. Some "
            "features may not work until you run:"
        )
        assert notice.text == DRIFT_TEXT
        assert notice.command == INSTALL_COMMAND
        assert notice.test_id == "update-drift"

    def test_no_warning_when_the_host_has_enough(self) -> None:
        assert host_setup_notice(_host(2), running_requires=2) is None
        assert host_setup_notice(_host(3), running_requires=2) is None


class TestBesideARefusal:
    """With an Available update, a Host setup block is the refusal under
    Update; the notice doesn't say it twice."""

    @pytest.mark.parametrize(
        ("host", "refusal"),
        [
            (_host(0, installed=False, enabled=False), "host_setup_missing"),
            (_host(1, enabled=False), "update_turned_off"),
        ],
    )
    def test_the_refusal_says_it(self, host: HostSetup, refusal: str) -> None:
        assert host_setup_notice(host, refusal=refusal) is None

    def test_no_second_copy_of_the_command(self) -> None:
        assert (
            host_setup_notice(
                _host(1), running_requires=2, refusal="host_setup_outdated"
            )
            is None
        )

    def test_drift_beside_a_refusal_without_the_command(self) -> None:
        notice = host_setup_notice(
            _host(1), running_requires=2, refusal="host_requirements_unknown"
        )

        assert notice is not None
        assert notice.text == DRIFT_TEXT
