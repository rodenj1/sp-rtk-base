"""SP-Base business logic services.

Provides singleton service instances and initialization for the
application's service layer:

- ``ConfigService`` — YAML configuration persistence
- ``RelayService`` — async wrapper around sp-rtk-base-relay RelayEngine
- ``EventBridge`` — thread-to-async event forwarding
- ``MetricsService`` — Prometheus metrics from RelayStatus
- ``DeviceService`` — GPS receiver connection & configuration (optional)
"""

from __future__ import annotations

import logging

from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.event_bridge import EventBridge
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.relay_service import RelayService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton instances
# ---------------------------------------------------------------------------

relay_service: RelayService = RelayService()
config_service: ConfigService = ConfigService()
event_bridge: EventBridge = EventBridge()
metrics_service: MetricsService = MetricsService()
device_service: DeviceService = DeviceService()


# ---------------------------------------------------------------------------
# FastAPI dependency injection helpers
# ---------------------------------------------------------------------------


def get_relay_service() -> RelayService:
    """Get the singleton RelayService instance.

    Returns:
        The application's RelayService instance.
    """
    return relay_service


def get_config_service() -> ConfigService:
    """Get the singleton ConfigService instance.

    Returns:
        The application's ConfigService instance.
    """
    return config_service


def get_event_bridge() -> EventBridge:
    """Get the singleton EventBridge instance.

    Returns:
        The application's EventBridge instance.
    """
    return event_bridge


def get_metrics_service() -> MetricsService:
    """Get the singleton MetricsService instance.

    Returns:
        The application's MetricsService instance.
    """
    return metrics_service


def get_device_service() -> DeviceService:
    """Get the singleton DeviceService instance.

    Returns:
        The application's DeviceService instance.
    """
    return device_service


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


async def init_services() -> None:
    """Initialize all services and optionally auto-start the relay.

    Loads the configuration from disk. If ``auto_start`` is enabled
    and both input config and destinations are configured, starts
    the relay engine and event bridge automatically.
    """
    global relay_service, config_service, event_bridge, device_service

    # Load config from disk (creates default if missing)
    config = config_service.load_config()
    logger.info("Services initialized — config loaded")

    # Wire up device service ↔ relay mutual exclusion
    device_service.set_relay_check(lambda: relay_service.is_running)

    # Auto-start relay if configured
    settings = config.settings
    if settings.auto_start and config.input is not None:
        destinations_configs = [
            d.to_relay_config() for d in config.destinations if d.enabled
        ]
        input_config = config.input.to_relay_config()

        try:
            await relay_service.start_relay(input_config, destinations_configs)
            logger.info("Auto-started relay engine")

            # Start event bridge after relay is running
            event_bridge.start(relay_service)
            logger.info("Auto-started event bridge")
        except Exception:
            logger.exception("Failed to auto-start relay engine")
