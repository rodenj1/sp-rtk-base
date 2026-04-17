"""Settings and input configuration API endpoints.

Provides read/write access to application settings and
input source configuration.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sp_base.models.api_models import (
    AppSettingsRequest,
    AppSettingsResponse,
    InputConfigRequest,
    InputConfigResponse,
)
from sp_base.models.config_models import AppSettings, InputProfile
from sp_base.services import get_config_service
from sp_base.services.config_service import ConfigService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["settings"])


# ---------------------------------------------------------------------------
# Application settings
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=AppSettingsResponse)
async def get_settings(
    config_svc: ConfigService = Depends(get_config_service),
) -> AppSettingsResponse:
    """Get application settings."""
    settings = config_svc.get_settings()
    return AppSettingsResponse(
        auto_start=settings.auto_start,
        status_poll_interval=settings.status_poll_interval,
    )


@router.put("/settings", response_model=AppSettingsResponse)
async def update_settings(
    request: AppSettingsRequest,
    config_svc: ConfigService = Depends(get_config_service),
) -> AppSettingsResponse:
    """Update application settings.

    Only the provided fields are updated; unset fields retain
    their current values.
    """
    current = config_svc.get_settings()

    updated = AppSettings(
        auto_start=(
            request.auto_start if request.auto_start is not None else current.auto_start
        ),
        status_poll_interval=(
            request.status_poll_interval
            if request.status_poll_interval is not None
            else current.status_poll_interval
        ),
    )
    config_svc.save_settings(updated)
    logger.info("Updated settings: auto_start=%s", updated.auto_start)

    return AppSettingsResponse(
        auto_start=updated.auto_start,
        status_poll_interval=updated.status_poll_interval,
    )


# ---------------------------------------------------------------------------
# Input source configuration
# ---------------------------------------------------------------------------


@router.get("/input", response_model=InputConfigResponse)
async def get_input_config(
    config_svc: ConfigService = Depends(get_config_service),
) -> InputConfigResponse:
    """Get the current input source configuration."""
    input_cfg = config_svc.get_input_config()
    if input_cfg is None:
        return InputConfigResponse(configured=False)

    return InputConfigResponse(
        source=input_cfg.source,
        config=input_cfg.config,
        configured=True,
    )


@router.put("/input", response_model=InputConfigResponse)
async def update_input_config(
    request: InputConfigRequest,
    config_svc: ConfigService = Depends(get_config_service),
) -> InputConfigResponse | JSONResponse:
    """Set the input source configuration.

    The relay must be restarted for input changes to take effect.
    """
    try:
        profile = InputProfile(
            source=request.source,  # type: ignore[arg-type]
            config=request.config,
        )
        config_svc.save_input_config(profile)
        logger.info("Updated input config: source=%s", request.source)

        return InputConfigResponse(
            source=profile.source,
            config=profile.config,
            configured=True,
        )
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(exc)},
        )
