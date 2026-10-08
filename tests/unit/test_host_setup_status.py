"""What Settings says about Host setup drift (sp-rtk-base#242)."""

from __future__ import annotations

from sp_rtk_base.ui.host_setup_status import DRIFT_TEXT, drift_warning
from sp_rtk_base.update.host_setup import HostSetup


def _host(plumbing: int, *, installed: bool = True) -> HostSetup:
    return HostSetup(installed=installed, enabled=True, plumbing=plumbing)


class TestDriftWarning:
    def test_warns_when_the_running_app_needs_newer_host_setup(self) -> None:
        assert drift_warning(_host(1), running_requires=2) == (
            "This version needs a newer host setup than the host has. Some "
            "features may not work until you run:"
        )
        assert DRIFT_TEXT == drift_warning(_host(1), running_requires=2)

    def test_no_warning_when_the_host_has_enough(self) -> None:
        assert drift_warning(_host(2), running_requires=2) is None
        assert drift_warning(_host(3), running_requires=2) is None

    def test_a_host_without_the_units_has_drifted_too(self) -> None:
        assert drift_warning(_host(0, installed=False), running_requires=1) is not None

    def test_no_second_copy_when_a_block_already_shows_the_command(self) -> None:
        assert drift_warning(_host(1), running_requires=2, command_shown=True) is None
