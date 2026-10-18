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
from collections.abc import Awaitable

from fastapi import FastAPI

from sp_rtk_base import __version__
from sp_rtk_base.api.bluetooth import router as bluetooth_router
from sp_rtk_base.api.config import router as config_router
from sp_rtk_base.api.correction_sources import router as correction_sources_router
from sp_rtk_base.api.destinations import router as destinations_router
from sp_rtk_base.api.device import router as device_router
from sp_rtk_base.api.events import router as events_router
from sp_rtk_base.api.health import router as health_router
from sp_rtk_base.api.metrics import router as metrics_router
from sp_rtk_base.api.network import router as network_router
from sp_rtk_base.api.portal import router as portal_router
from sp_rtk_base.api.profiles import router as profiles_router
from sp_rtk_base.api.relay import router as relay_router
from sp_rtk_base.api.settings import router as settings_router
from sp_rtk_base.api.update import router as update_router

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
    api.include_router(correction_sources_router)
    api.include_router(settings_router)
    api.include_router(events_router)
    api.include_router(metrics_router)
    api.include_router(config_router)
    api.include_router(device_router)
    api.include_router(network_router)
    api.include_router(profiles_router)
    api.include_router(bluetooth_router)
    api.include_router(update_router)
    api.include_router(portal_router)
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
    from sp_rtk_base import services as services_mod

    await services_mod.init_services()

    # An Update a power cut left unfinished is failed, not left Updating;
    # the Host setup is read once here (and again on Check now), not per poll.
    await services_mod.update_service.recover_interrupted()
    await services_mod.update_service.refresh_host_setup()

    # Signal Quality polls the receiver in the background while it is
    # connected, so pages (and metrics) see it whether or not one is open.
    services_mod.signal_quality_service.start()

    # Bluetooth link strength polls the Relay status in the background, so
    # every tab shows the same verdict, already warm when a page opens.
    services_mod.bluetooth_link_service.start()

    # Check PyPI for an Available update now, then every 24 h.
    services_mod.update_check_service.start()

    # Tunnel opens and closes from the Portal agent go into the Event log.
    services_mod.portal_events.start()


async def _stop_within(stop: Awaitable[object], timeout: float, label: str) -> None:
    """Await one shutdown step for at most *timeout* seconds.  Never raises.

    A timeout logs a warning and any other error logs its traceback, both
    naming *label* ("Timed out stopping <label>", "Error stopping <label>"),
    so one step's failure can't keep the rest of shutdown from running.
    """
    import asyncio

    try:
        await asyncio.wait_for(stop, timeout=timeout)
    except TimeoutError:
        logger.warning("Timed out stopping %s", label)
    except Exception:
        logger.exception("Error stopping %s", label)


async def shutdown_services() -> None:
    """Gracefully stop the GPS device and the relay engine.

    Order of operations matters:

    0. **Auto-start task** — cancel before anything else.  If the
       background retry loop is mid-``start_relay`` (blocking
       Bluetooth / serial / NTRIP connect), every subsequent step
       will fight it for the engine lock.  Cancel it first so the
       rest of the shutdown can proceed; the underlying daemon
       thread may keep trying for a moment but it can't block us.
       **Signal Quality poller** — cancelled next, before the device
       goes away, so it is never mid-poll when the driver disconnects.
    1. **Device first** — release the serial / Bluetooth handle while
       the event loop is still healthy.  If we wait until the relay
       is already torn down, the relay engine may
       still hold the same serial port (Bug D scenario) and the
       driver's ``disconnect()`` will race the kernel reclaiming the
       fd.
    2. **Relay engine** — last, so any in-flight RTCM chunk finishes
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
    from sp_rtk_base.services import device_service, relay_service

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

    # 0b. Signal Quality poller — stop it before the device goes away so
    #     it isn't mid-poll when the driver disconnects.
    await _stop_within(
        services_mod.signal_quality_service.stop(),
        AUTO_START_TASK_CANCEL_TIMEOUT_SECONDS,
        "the Signal Quality poller",
    )

    # 0b. Bluetooth link strength poller — nothing depends on it.
    await _stop_within(
        services_mod.bluetooth_link_service.stop(),
        AUTO_START_TASK_CANCEL_TIMEOUT_SECONDS,
        "the Bluetooth link strength poller",
    )

    # 0b'. Update check schedule — nothing depends on it; just stop it.
    await _stop_within(
        services_mod.update_check_service.stop(),
        AUTO_START_TASK_CANCEL_TIMEOUT_SECONDS,
        "the update check",
    )

    # 0b''. Portal agent's Event log entries — nothing depends on it.
    await _stop_within(
        services_mod.portal_events.stop(),
        AUTO_START_TASK_CANCEL_TIMEOUT_SECONDS,
        "the Portal agent's Event log entries",
    )

    # 0c. Survey-in — stop the station's averaging before the device goes.
    await _stop_within(
        services_mod.survey_service.shutdown(),
        AUTO_START_TASK_CANCEL_TIMEOUT_SECONDS,
        "the survey-in",
    )

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

    # 2. Relay engine — bounded so a stuck destination thread can't
    #    burn the rest of systemd's TimeoutStopSec window.
    try:
        await asyncio.wait_for(
            relay_service.stop_relay(trigger="shutdown"),
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
    from nicegui import app, ui

    from sp_rtk_base.ui.storage_stand_in import STORAGE_STAND_IN

    # Every page also runs inside the Portal's sandboxed frame (ADR 0004).
    ui.add_head_html(STORAGE_STAND_IN, shared=True)

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
    from sp_rtk_base.ui.pages import portal as _portal
    from sp_rtk_base.ui.pages import settings as _settings
    from sp_rtk_base.ui.pages import survey as _survey

    # Reference the modules to prevent "unused import" removal
    _ = (_dashboard, _gps_config, _input, _outputs, _portal, _settings, _survey)

    # Network page is appliance-only (issue #28) — in managed-host mode
    # something else owns the host's network stack, so the route isn't
    # registered at all rather than rendered disabled.
    from sp_rtk_base.services import get_config_service

    if get_config_service().get_config().deployment.mode == "appliance":
        from sp_rtk_base.ui.pages import network as _network

        _ = _network

    app.on_startup(startup_services)
    app.on_shutdown(shutdown_services)
