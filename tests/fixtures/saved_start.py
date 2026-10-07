"""Give a mocked ``RelayService`` the real start-from-saved-config logic.

Tests that mock ``RelayService`` set ``is_running`` and ``start_relay``
on the mock.  Binding the real ``check_saved`` / ``start_saved`` to it
keeps those tests running the real refusals over their own saved
config, while the engine-facing ``start_relay`` stays mocked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from sp_rtk_base.models.config_models import InputProfile
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.relay_service import RelayService, SavedStart


def with_saved_start(relay: MagicMock, config_service: ConfigService) -> MagicMock:
    """Bind the real ``check_saved`` and ``start_saved`` to ``relay``."""
    relay._config_service = config_service

    def check_saved(input_profile: InputProfile | None = None) -> SavedStart:
        return RelayService.check_saved(relay, input_profile)

    async def start_saved(trigger: str = "unknown", **kwargs: Any) -> None:
        await RelayService.start_saved(relay, trigger, **kwargs)

    relay.check_saved.side_effect = check_saved
    relay.start_saved.side_effect = start_saved
    return relay
