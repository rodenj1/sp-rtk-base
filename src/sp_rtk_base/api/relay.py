"""Relay engine control API endpoints.

Provides start/stop lifecycle control and status queries.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sp_rtk_base import services as services_mod
from sp_rtk_base.models.api_models import (
    AutoStartStatusModel,
    RelayActionResponse,
    RelayStartRequest,
    RelayStatusResponse,
)
from sp_rtk_base.services import (
    get_relay_service,
)
from sp_rtk_base.services.relay_service import (
    RelayService,
    RelayStartRefusedError,
    StartRefusal,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/relay", tags=["relay"])


def _auto_start_status_model() -> AutoStartStatusModel:
    """Snapshot the module-level auto-start status as an API model."""
    snapshot = services_mod.auto_start_status
    return AutoStartStatusModel(
        state=snapshot.state,
        attempts=snapshot.attempts,
        last_error=snapshot.last_error,
        last_updated=snapshot.last_updated,
    )


@router.get("/status", response_model=RelayStatusResponse)
async def get_relay_status(
    relay: RelayService = Depends(get_relay_service),
) -> RelayStatusResponse:
    """Get the current relay engine status.

    Returns a complete snapshot of the relay engine state including
    input connection, all destinations, throughput metrics, and the
    auto-start lifecycle status (so UIs can render a banner if
    auto-start is retrying or has failed).
    """
    auto_start = _auto_start_status_model()
    status = await relay.get_status()
    if status is None:
        return RelayStatusResponse(running=False, auto_start=auto_start)

    # Convert the dataclass-based status to our API model
    status_dict: dict[str, Any] = dataclasses.asdict(status)
    status_dict["auto_start"] = auto_start.model_dump()
    return RelayStatusResponse.model_validate(status_dict)


# The HTTP status of each start refusal, for every route that starts the
# Relay.  A saved config that can't be turned into Relay config (e.g. a
# SurePath output saved with an empty Username from a pre-v0.3.15 UI) is
# unprocessable.
START_REFUSAL_STATUS: dict[StartRefusal, int] = {
    "already_running": 409,
    "console_connected": 409,
    "no_input": 400,
    "no_destinations": 400,
    "config_invalid": 422,
}


@router.post("/start", response_model=RelayActionResponse)
async def start_relay(
    request: RelayStartRequest | None = None,
    relay: RelayService = Depends(get_relay_service),
) -> RelayActionResponse | JSONResponse:
    """Start the relay engine.

    By default, uses the saved configuration. The relay must have
    a configured input source and at least one destination.

    Returns 409 with ``code: console_connected`` while the console is
    connected, whatever the Console link kind; nothing is touched.
    """
    try:
        await relay.start_saved(trigger="api")
        return RelayActionResponse(status="ok", message="Relay engine started")
    except RelayStartRefusedError as exc:
        logger.info("Start refused (%s): %s", exc.code, exc.message)
        return JSONResponse(
            status_code=START_REFUSAL_STATUS[exc.code],
            content={"status": "error", "message": exc.message, "code": exc.code},
        )
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
            or exc.__class__.__name__ in ("ValidationError", "ConfigurationError")
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
) -> RelayActionResponse | JSONResponse:
    """Stop the relay engine.

    Safe to call when already stopped.
    """
    if not relay.is_running:
        return JSONResponse(
            status_code=409,
            content={"status": "error", "message": "Relay engine is not running"},
        )

    try:
        await relay.stop_relay(trigger="api")
        return RelayActionResponse(status="ok", message="Relay engine stopped")
    except Exception as exc:
        logger.exception("Failed to stop relay engine")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(exc)},
        )
