"""While an Update runs, nothing new starts on code about to be replaced
(ADR 0005): Start, Survey-in and Console connect each hold an
:class:`UpdateGuard`, wired to ``UpdateService.updating`` by
``services.wire_update_guard``.
"""

from __future__ import annotations

from collections.abc import Callable

from sp_rtk_base.update.state import UPDATING_MESSAGE


class UpdateGuard:
    """Says whether an Update runs, and refuses while one does."""

    def __init__(self) -> None:
        self._updating: Callable[[], bool] = lambda: False

    def wire(self, updating: Callable[[], bool]) -> None:
        """Set what says whether an Update is running."""
        self._updating = updating

    def updating(self) -> bool:
        return self._updating()

    def refuse(self, refused: Callable[[str], Exception] = RuntimeError) -> None:
        """Raise ``refused(UPDATING_MESSAGE)`` while an Update runs."""
        if self._updating():
            raise refused(UPDATING_MESSAGE)
