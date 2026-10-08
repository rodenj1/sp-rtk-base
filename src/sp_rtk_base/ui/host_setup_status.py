"""What Settings says about Host setup (sp-rtk-base#242).

The blocks themselves (setup missing, turned off, the release needs newer
setup, its requirement unreadable) are refusals, worded in
``services/update_service``. This covered module decides the drift
warning: the running SP-Base needs newer Host setup than the host has,
which a manual ``pip install`` can leave behind.
"""

from __future__ import annotations

from sp_rtk_base.update.host_setup import PLUMBING_VERSION, HostSetup

DRIFT_TEXT = (
    "This version needs a newer host setup than the host has. Some features "
    "may not work until you run:"
)


def drift_warning(
    host: HostSetup,
    *,
    running_requires: int = PLUMBING_VERSION,
    command_shown: bool = False,
) -> str | None:
    """The drift warning, or ``None``.

    ``command_shown``: a block already shows the ``install.sh`` command, so
    the warning would only repeat it.
    """
    if command_shown or host.plumbing >= running_requires:
        return None
    return DRIFT_TEXT
