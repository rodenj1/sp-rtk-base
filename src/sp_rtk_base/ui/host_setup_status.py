"""What Settings says about Host setup (sp-rtk-base#242).

With an Available update, the blocks (setup missing, turned off, the
release needs newer setup, its requirement unreadable) are refusals under
Update, worded in ``services/update_service``. This covered module decides
the notice that shows whether or not an update is available:

- a host without the Update units (a base installed before Update) is
  asked for the one-time ``install.sh`` re-run;
- a host with Update turned off says so;
- otherwise, drift: the running SP-Base needs newer Host setup than the
  host has, which a manual ``pip install`` can leave behind.

A refusal already saying the same thing (or showing the command) wins.
"""

from __future__ import annotations

from dataclasses import dataclass

from sp_rtk_base.services.update_service import REFUSAL_MESSAGES
from sp_rtk_base.update.host_setup import INSTALL_COMMAND, PLUMBING_VERSION, HostSetup

DRIFT_TEXT = (
    "This version needs a newer host setup than the host has. Some features "
    "may not work until you run:"
)

_HOST_BLOCKS = frozenset({"host_setup_missing", "update_turned_off"})
_COMMAND_SHOWN = frozenset({"host_setup_missing", "host_setup_outdated"})


@dataclass(frozen=True)
class HostSetupNotice:
    """A line about this host's Host setup, with the command to run if any."""

    text: str
    command: str | None
    test_id: str


def host_setup_notice(
    host: HostSetup,
    *,
    refusal: str | None = None,
    running_requires: int = PLUMBING_VERSION,
) -> HostSetupNotice | None:
    """The notice for ``host``, or ``None``.

    ``refusal``: the code of the refusal shown under Update, if any.
    """
    if refusal in _HOST_BLOCKS:
        return None
    if not host.installed:
        return HostSetupNotice(
            REFUSAL_MESSAGES["host_setup_missing"], INSTALL_COMMAND, "update-host-setup"
        )
    if not host.enabled:
        return HostSetupNotice(
            REFUSAL_MESSAGES["update_turned_off"], None, "update-host-setup"
        )
    if refusal in _COMMAND_SHOWN or host.plumbing >= running_requires:
        return None
    return HostSetupNotice(DRIFT_TEXT, INSTALL_COMMAND, "update-drift")
