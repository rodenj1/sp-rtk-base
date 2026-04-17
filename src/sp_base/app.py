"""FastAPI + NiceGUI application factory.

Creates the shared ASGI application with both REST API endpoints
and NiceGUI browser UI pages on the same server.
"""

# pyright: reportUnknownMemberType=false
# NiceGUI's app.on_startup() has partially unknown types due to
# Starlette's handler signature. This is a third-party library limitation.

from __future__ import annotations

from fastapi import FastAPI

from sp_base import __version__
from sp_base.api.config import router as config_router
from sp_base.api.destinations import router as destinations_router
from sp_base.api.device import router as device_router
from sp_base.api.events import router as events_router
from sp_base.api.health import router as health_router
from sp_base.api.metrics import router as metrics_router
from sp_base.api.relay import router as relay_router
from sp_base.api.settings import router as settings_router


def create_api_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """
    api = FastAPI(
        title="SP-Base API",
        version=__version__,
        description="REST API for controlling and monitoring the RTCM relay engine",
    )
    api.include_router(health_router)
    api.include_router(relay_router)
    api.include_router(destinations_router)
    api.include_router(settings_router)
    api.include_router(events_router)
    api.include_router(metrics_router)
    api.include_router(config_router)
    api.include_router(device_router)
    return api


def init_app() -> None:
    """Initialize the full application (FastAPI API + NiceGUI UI).

    This function registers the FastAPI API routes on the NiceGUI app,
    sets up all NiceGUI page routes, and schedules service initialization.
    Must be called before ui.run().
    """
    from nicegui import app

    api = create_api_app()

    # Mount FastAPI API routes onto the NiceGUI app
    for route in api.routes:
        app.routes.append(route)

    # Register NiceGUI page routes (imported for side effects —
    # each module uses @ui.page decorator to register its route)
    from sp_base.ui.pages import dashboard as _dashboard
    from sp_base.ui.pages import gps_config as _gps_config
    from sp_base.ui.pages import input as _input
    from sp_base.ui.pages import outputs as _outputs
    from sp_base.ui.pages import settings as _settings
    from sp_base.ui.pages import survey as _survey

    # Reference the modules to prevent "unused import" removal
    _ = (_dashboard, _gps_config, _input, _outputs, _settings, _survey)

    # Schedule service initialization on startup
    async def _startup() -> None:
        from sp_base.services import init_services

        await init_services()

    async def _shutdown() -> None:
        """Gracefully stop relay engine and event bridge on server shutdown."""
        import logging

        from sp_base.services import event_bridge, relay_service

        _logger = logging.getLogger(__name__)
        _logger.info("Application shutting down — stopping services…")

        try:
            event_bridge.stop()
        except Exception:
            _logger.exception("Error stopping event bridge")

        try:
            await relay_service.stop_relay()
        except Exception:
            _logger.exception("Error stopping relay engine")

        _logger.info("Services stopped")

    app.on_startup(_startup)
    app.on_shutdown(_shutdown)
