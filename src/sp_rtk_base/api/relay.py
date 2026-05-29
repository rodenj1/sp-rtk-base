"""Relay engine control API endpoints.

Provides start/stop lifecycle control and status queries.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sp_rtk_base.models.api_models import (
    RelayActionResponse,
    RelayStartRequest,
    RelayStatusResponse,
)
from sp_rtk_base.services import (
    get_config_service,
    get_event_bridge,
    get_relay_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.event_bridge import EventBridge
from sp_rtk_base.services.relay_service import RelayService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/relay", tags=["relay"])


@router.get("/status", response_model=RelayStatusResponse)
async def get_relay_status(
    relay: RelayService = Depends(get_relay_service),
) -> RelayStatusResponse:
    """Get the current relay engine status.

    Returns a complete snapshot of the relay engine state including
    input connection, all destinations, and throughput metrics.
    """
    status = await relay.get_status()
    if status is None:
        return RelayStatusResponse(running=False)

    # Convert the dataclass-based status to our API model
    status_dict: dict[str, Any] = dataclasses.asdict(status)
    return RelayStatusResponse.model_validate(status_dict)


@router.post("/start", response_model=RelayActionResponse)
async def start_relay(
    request: RelayStartRequest | None = None,
    relay: RelayService = Depends(get_relay_service),
    config_svc: ConfigService = Depends(get_config_service),
    event_bridge: EventBridge = Depends(get_event_bridge),
) -> RelayActionResponse | JSONResponse:
    """Start the relay engine.

    By default, uses the saved configuration. The relay must have
    a configured input source and at least one destination.
    """
    if relay.is_running:
        return JSONResponse(
            status_code=409,
            content={"status": "error", "message": "Relay engine is already running"},
        )

    config = config_svc.get_config()

    if config.input is None:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": "No input source configured. Configure an input source first.",
            },
        )

    enabled_dests = [d for d in config.destinations if d.enabled]
    if not enabled_dests:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": "No enabled destinations configured.",
            },
        )

    input_config = config.input.to_relay_config()
    dest_configs = [d.to_relay_config() for d in enabled_dests]

    try:
        await relay.start_relay(input_config, dest_configs)

        # Start event bridge for real-time events
        if not event_bridge.is_running:
            event_bridge.start(relay)

        return RelayActionResponse(status="ok", message="Relay engine started")
    except Exception as exc:
        logger.exception("Failed to start relay engine")
        # Map common failure shapes to better status codes:
        #   - pydantic / config-shape errors → 422 (unprocessable
        #     entity — the saved config is malformed)
        #   - network refusals / engine bring-up failures → 502
        #     (bad gateway — we tried to connect to an external
        #     service and it failed)
        #   - everything else → 500 (genuine server bug)
        exc_text = str(exc)
        exc_lower = exc_text.lower()
        if (
            "validation error" in exc_lower
            or "field required" in exc_lower
            or "configurationerror" in exc_lower
            or "input.config" in exc_lower
            or exc.__class__.__name__ == "ValidationError"
        ):
            status_code = 422
        elif (
            "connection refused" in exc_lower
            or "could not resolve" in exc_lower
            or "name or service not known" in exc_lower
            or "no route to host" in exc_lower
            or "connection timed out" in exc_lower
            or "engine" in exc_lower
        ):
            status_code = 502
        else:
            status_code = 500
        return JSONResponse(
            status_code=status_code,
            content={"status": "error", "message": exc_text},
        )


@router.post("/stop", response_model=RelayActionResponse)
async def stop_relay(
    relay: RelayService = Depends(get_relay_service),
    event_bridge: EventBridge = Depends(get_event_bridge),
) -> RelayActionResponse | JSONResponse:
    """Stop the relay engine.

    Stops the relay engine and event bridge. Safe to call when
    already stopped.
    """
    if not relay.is_running:
        return JSONResponse(
            status_code=409,
            content={"status": "error", "message": "Relay engine is not running"},
        )

    try:
        event_bridge.stop()
        await relay.stop_relay()
        return RelayActionResponse(status="ok", message="Relay engine stopped")
    except Exception as exc:
        logger.exception("Failed to stop relay engine")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(exc)},
        )
