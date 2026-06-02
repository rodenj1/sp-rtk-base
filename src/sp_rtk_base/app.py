"""FastAPI + NiceGUI application factory.

Creates the shared ASGI application with both REST API endpoints
and NiceGUI browser UI pages on the same server.

This module also owns the application's **lifecycle hooks**:
``startup_services()`` and ``shutdown_services()``.  These are
exposed at module scope (rather than nested closures inside
:func:`init_app`) so the application's lifecycle can be exercised
by unit tests and reused by signal handlers in :mod:`sp_rtk_base.main`
without going through NiceGUI's ``app.on_shutdown`` machinery.
"""

# pyright: reportUnknownMemberType=false
# NiceGUI's app.on_startup() has partially unknown types due to
# Starlette's handler signature. This is a third-party library limitation.

from __future__ import annotations

import logging

from fastapi import FastAPI

from sp_rtk_base import __version__
from sp_rtk_base.api.config import router as config_router
from sp_rtk_base.api.destinations import router as destinations_router
from sp_rtk_base.api.device import router as device_router
from sp_rtk_base.api.events import router as events_router
from sp_rtk_base.api.health import router as health_router
from sp_rtk_base.api.metrics import router as metrics_router
from sp_rtk_base.api.relay import router as relay_router
from sp_rtk_base.api.settings import router as settings_router

logger = logging.getLogger(__name__)

# Per-driver disconnect budget on shutdown.  Bluetooth's BlueZ teardown
# can take a few seconds on a healthy bus and ~ten on a flaky one; we
# don't want a stuck driver to hold up the rest of shutdown.
DEVICE_DISCONNECT_TIMEOUT_SECONDS: float = 10.0

# Hard budget for the relay engine's stop().  If the engine is mid-start
# (blocking Bluetooth connect on an in-flight auto-start attempt) when
# shutdown arrives, ``stop()`` will wait for the start to finish — which
# can be tens of seconds.  Cap it so a stuck engine can't blow past
# systemd's TimeoutStopSec.
RELAY_STOP_TIMEOUT_SECONDS: float = 15.0

# Time to give a cancelled auto-start task to clean up before moving on.
# The task is in either asyncio.sleep (cancels instantly) or
# asyncio.to_thread (the thread won't honour cancellation; we just
# detach and continue shutting down).
AUTO_START_TASK_CANCEL_TIMEOUT_SECONDS: float = 2.0


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


# ---------------------------------------------------------------------------
# Lifecycle hooks (module-scope so they can be unit-tested directly)
# ---------------------------------------------------------------------------


async def startup_services() -> None:
    """Initialize services on application startup.

    Thin wrapper around :func:`sp_rtk_base.services.init_services` that
    is registered as NiceGUI's startup hook.  Kept at module scope so
    tests can call it without spinning up NiceGUI.
    """
    from sp_rtk_base.services import init_services

    await init_services()


async def shutdown_services() -> None:
    """Gracefully stop the GPS device, relay engine, and event bridge.

    Order of operations matters:

    0. **Auto-start task** — cancel before anything else.  If the
       background retry loop is mid-``start_relay`` (blocking
       Bluetooth / serial / NTRIP connect), every subsequent step
       will fight it for the engine lock.  Cancel it first so the
       rest of the shutdown can proceed; the underlying daemon
       thread may keep trying for a moment but it can't block us.
    1. **Device first** — release the serial / Bluetooth handle while
       the event loop is still healthy.  If we wait until the relay
       and event bridge are already torn down, the relay engine may
       still hold the same serial port (Bug D scenario) and the
       driver's ``disconnect()`` will race the kernel reclaiming the
       fd.
    2. **Event bridge** — stop forwarding before the relay shuts down
       so no events fan out into a half-stopped subscriber.
    3. **Relay engine** — last, so any in-flight RTCM chunk finishes
       being distributed to destinations before the destination
       writers are cancelled.  Bounded by
       :data:`RELAY_STOP_TIMEOUT_SECONDS` so a stuck engine teardown
       can't blow past systemd's TimeoutStopSec.

    Each step is wrapped in its own ``try/except`` so a failure in one
    cannot prevent the others from running.  Each blocking step is also
    wrapped in :func:`asyncio.wait_for` with a budget appropriate to
    its worst-case latency.
    """
    import asyncio

    from sp_rtk_base import services as services_mod
    from sp_rtk_base.services import device_service, event_bridge, relay_service

    logger.info("Application shutting down — stopping services…")

    # 0. Cancel the auto-start retry task (v0.3.29).  If it's in
    #    asyncio.sleep, cancellation is instant.  If it's mid-attempt
    #    inside asyncio.to_thread(engine.start), the thread keeps
    #    running but the task gets detached — uvicorn won't wait on
    #    it.  Either way we move on after the budget.
    task = services_mod.auto_start_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=AUTO_START_TASK_CANCEL_TIMEOUT_SECONDS)
        except (asyncio.CancelledError, TimeoutError):
            pass
        except Exception:
            logger.exception("Error awaiting cancelled auto-start task")

    # 1. Device first — release the GPS handle (Bug B).
    if device_service.is_available and device_service.is_connected:
        try:
            await asyncio.wait_for(
                device_service.disconnect(),
                timeout=DEVICE_DISCONNECT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Device disconnect exceeded %.1fs budget; proceeding with shutdown",
                DEVICE_DISCONNECT_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Error during device disconnect")

    # 2. Event bridge.
    try:
        event_bridge.stop()
    except Exception:
        logger.exception("Error stopping event bridge")

    # 3. Relay engine — bounded so a stuck destination thread can't
    #    burn the rest of systemd's TimeoutStopSec window.
    try:
        await asyncio.wait_for(
            relay_service.stop_relay(),
            timeout=RELAY_STOP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Relay engine stop exceeded %.1fs budget; proceeding with shutdown",
            RELAY_STOP_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("Error stopping relay engine")

    logger.info("Services stopped")


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
    from sp_rtk_base.ui.pages import dashboard as _dashboard
    from sp_rtk_base.ui.pages import gps_config as _gps_config
    from sp_rtk_base.ui.pages import input as _input
    from sp_rtk_base.ui.pages import outputs as _outputs
    from sp_rtk_base.ui.pages import settings as _settings
    from sp_rtk_base.ui.pages import survey as _survey

    # Reference the modules to prevent "unused import" removal
    _ = (_dashboard, _gps_config, _input, _outputs, _settings, _survey)

    app.on_startup(startup_services)
    app.on_shutdown(shutdown_services)
