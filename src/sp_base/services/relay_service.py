"""Relay engine service — async wrapper around sp-rtk-base-relay RelayEngine.

Provides lifecycle management (start/stop), status queries, and
dynamic destination management with async bridging via
``asyncio.to_thread()`` for blocking RelayEngine methods.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from sp_rtk_base_relay import EventSubscription, RelayEngine, RelayEvent, RelayStatus
from sp_rtk_base_relay.config import DestinationConfig, InputConfig
from sp_rtk_base_relay.exceptions import ServiceError

logger = logging.getLogger(__name__)


class RelayService:
    """Async adapter for sp-rtk-base-relay's threaded RelayEngine.

    All blocking ``RelayEngine`` methods are wrapped with
    ``asyncio.to_thread()`` so they can be called safely from
    async FastAPI handlers.

    The engine is created lazily on the first ``start_relay()`` call
    and recreated if the input configuration changes.
    """

    def __init__(self) -> None:
        self._engine: RelayEngine | None = None
        self._input_config: InputConfig | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the relay engine is currently running."""
        if self._engine is None:
            return False
        return self._engine.is_running

    @property
    def engine(self) -> RelayEngine | None:
        """Direct access to the underlying RelayEngine (or None)."""
        return self._engine

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_relay(
        self,
        input_config: InputConfig,
        destinations: list[DestinationConfig] | None = None,
    ) -> None:
        """Start the relay engine.

        Creates a new ``RelayEngine`` if one does not exist or if the
        input configuration has changed since the last start.

        Args:
            input_config: Input source configuration.
            destinations: Optional initial destinations.

        Raises:
            ServiceError: If the engine is already running.
            ConfigurationError: If the configuration is invalid.
        """
        if self._engine is not None and self._engine.is_running:
            raise ServiceError("Relay engine is already running")

        # Recreate engine if input config changed or first start
        if self._engine is None or self._input_config != input_config:
            self._engine = RelayEngine(input_config)
            self._input_config = input_config
            logger.info("Created new RelayEngine with source=%s", input_config.source)

        await asyncio.to_thread(self._engine.start, destinations)
        logger.info("Relay engine started")

    async def stop_relay(self) -> None:
        """Stop the relay engine.

        Safe to call when already stopped (no-op if engine is None
        or not running).
        """
        if self._engine is None:
            return
        if not self._engine.is_running:
            return

        await asyncio.to_thread(self._engine.stop)
        logger.info("Relay engine stopped")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_status(self) -> RelayStatus | None:
        """Get the current relay status snapshot.

        Returns:
            Frozen ``RelayStatus`` snapshot, or None if engine is
            not running.
        """
        if self._engine is None or not self._engine.is_running:
            return None

        try:
            return await asyncio.to_thread(self._engine.get_status)
        except ServiceError:
            # Engine may have stopped between the check and the call
            return None

    # ------------------------------------------------------------------
    # Destination management
    # ------------------------------------------------------------------

    async def add_destination(self, config: DestinationConfig) -> str:
        """Hot-add a destination to the running engine.

        Args:
            config: Destination configuration.

        Returns:
            The destination name.

        Raises:
            ServiceError: If the engine is not running.
            ConfigurationError: If the config is invalid or name is duplicate.
        """
        self._require_running()
        assert self._engine is not None  # guarded by _require_running  # noqa: S101
        return await asyncio.to_thread(self._engine.add_destination, config)

    async def remove_destination(self, name: str) -> None:
        """Remove a destination from the running engine.

        Args:
            name: Destination name to remove.

        Raises:
            ServiceError: If the engine is not running.
            KeyError: If the destination is not found.
        """
        self._require_running()
        assert self._engine is not None  # noqa: S101
        await asyncio.to_thread(self._engine.remove_destination, name)

    async def start_destination(self, name: str) -> None:
        """Resume a paused destination.

        Args:
            name: Destination name to start.

        Raises:
            ServiceError: If the engine is not running.
            KeyError: If the destination is not found.
        """
        self._require_running()
        assert self._engine is not None  # noqa: S101
        await asyncio.to_thread(self._engine.start_destination, name)

    async def stop_destination(self, name: str) -> None:
        """Pause a destination (keeps it registered).

        Args:
            name: Destination name to stop.

        Raises:
            ServiceError: If the engine is not running.
            KeyError: If the destination is not found.
        """
        self._require_running()
        assert self._engine is not None  # noqa: S101
        await asyncio.to_thread(self._engine.stop_destination, name)

    def get_destination_names(self) -> list[str]:
        """Get names of all registered destinations.

        Returns:
            List of destination names, or empty list if not running.
        """
        if self._engine is None:
            return []
        return self._engine.get_destination_names()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def subscribe_events(self) -> EventSubscription | None:
        """Create a new event subscription.

        Returns:
            An ``EventSubscription``, or None if engine does not exist.
        """
        if self._engine is None:
            return None
        return self._engine.subscribe_events()

    def get_recent_events(self, count: int = 50) -> list[dict[str, Any]]:
        """Get recent events from the ring buffer as dicts.

        Args:
            count: Maximum number of events to return.

        Returns:
            List of event dictionaries (oldest first).
        """
        if self._engine is None:
            return []
        events: list[RelayEvent] = self._engine.get_recent_events(count)
        return [asdict(e) for e in events]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_running(self) -> None:
        """Raise ServiceError if the engine is not running.

        Raises:
            ServiceError: If engine is None or not running.
        """
        if self._engine is None or not self._engine.is_running:
            raise ServiceError("Relay engine is not running")
