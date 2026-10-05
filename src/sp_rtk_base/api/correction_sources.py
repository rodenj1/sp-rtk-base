"""Correction source API endpoints (issue #192).

CRUD for the saved NTRIP Correction sources a Corrected survey-in reads,
following the destinations API's conventions. The password is write-only:
no response carries it, only ``has_password``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from sp_rtk_base.api.no_echo import NoEchoRoute, describe, error_response
from sp_rtk_base.models.api_models import (
    CorrectionSourceCreateRequest,
    CorrectionSourceListResponse,
    CorrectionSourceResponse,
    CorrectionSourceUpdateRequest,
    CorrectionSourceVerifyRequest,
    RelayActionResponse,
)
from sp_rtk_base.models.config_models import (
    CorrectionSourceProfile,
    NtripCorrectionConfig,
)
from sp_rtk_base.models.verification_models import VerificationResult
from sp_rtk_base.services import (
    get_config_service,
    get_correction_verification_service,
)
from sp_rtk_base.services.config_service import (
    ConfigService,
    CorrectionSourceExistsError,
    CorrectionSourceInUseError,
    CorrectionSourceNotFoundError,
)
from sp_rtk_base.services.correction_verification import (
    CorrectionSourceVerificationService,
)
from sp_rtk_base.services.verification import VerificationRefusedError

logger = logging.getLogger(__name__)


def _describe(errors: Sequence[Any]) -> str:
    return describe(errors, "Invalid Correction source")


router = APIRouter(
    prefix="/api/correction-sources",
    tags=["correction-sources"],
    route_class=NoEchoRoute,
)


def _to_response(source: CorrectionSourceProfile) -> CorrectionSourceResponse:
    cfg = source.config
    return CorrectionSourceResponse(
        name=source.name,
        kind=source.kind,
        caster=cfg.caster,
        port=cfg.port,
        mountpoint=cfg.mountpoint,
        username=cfg.username,
        version=cfg.version,
        tls=cfg.tls,
        has_password=bool(cfg.password),
    )


@router.get("", response_model=CorrectionSourceListResponse)
async def list_correction_sources(
    config_svc: ConfigService = Depends(get_config_service),
) -> CorrectionSourceListResponse:
    """All saved Correction sources, and the one used last."""
    sources = [_to_response(s) for s in config_svc.get_correction_sources()]
    return CorrectionSourceListResponse(
        sources=sources,
        count=len(sources),
        last_used=config_svc.get_last_correction_source(),
    )


@router.post("/verify", response_model=VerificationResult)
async def verify_correction_source(
    request: CorrectionSourceVerifyRequest,
    config_svc: ConfigService = Depends(get_config_service),
    verifier: CorrectionSourceVerificationService = Depends(
        get_correction_verification_service
    ),
) -> VerificationResult | JSONResponse:
    """Verify the form's values: would a Corrected survey-in get corrections?

    Advisory: saving doesn't need it. A blank password with the ``name`` of
    a saved source uses that source's saved password. Refused with 409
    (``verification_in_progress``) while another Verification runs.
    """
    fields = request.model_dump(exclude={"name"})
    if not fields["password"] and request.name:
        saved = config_svc.get_correction_source(request.name)
        if saved is not None:
            fields["password"] = saved.saved_password_for(request.caster, request.port)
    try:
        config = NtripCorrectionConfig.model_validate(fields)
    except ValidationError as exc:
        return error_response(422, _describe(exc.errors()))
    try:
        return await verifier.verify(config)
    except VerificationRefusedError as exc:
        logger.info("Correction source Verification refused (%s)", exc.code)
        return JSONResponse(
            status_code=409,
            content={"status": "error", "message": exc.message, "code": exc.code},
        )


@router.get("/{name}", response_model=CorrectionSourceResponse)
async def get_correction_source(
    name: str,
    config_svc: ConfigService = Depends(get_config_service),
) -> CorrectionSourceResponse | JSONResponse:
    """One saved Correction source."""
    source = config_svc.get_correction_source(name)
    if source is None:
        return error_response(404, f"Correction source '{name}' not found")
    return _to_response(source)


@router.post("", response_model=CorrectionSourceResponse, status_code=201)
async def create_correction_source(
    request: CorrectionSourceCreateRequest,
    config_svc: ConfigService = Depends(get_config_service),
) -> CorrectionSourceResponse | JSONResponse:
    """Save a new Correction source. Names are unique (409)."""
    try:
        source = CorrectionSourceProfile(
            name=request.name,
            config=NtripCorrectionConfig.model_validate(
                request.model_dump(exclude={"name"})
            ),
        )
        config_svc.create_correction_source(source)
    except CorrectionSourceExistsError as exc:
        return error_response(409, str(exc))
    except ValidationError as exc:
        return error_response(422, _describe(exc.errors()))
    logger.info("Created Correction source: %s", source.name)
    return _to_response(source)


@router.put("/{name}", response_model=CorrectionSourceResponse)
async def update_correction_source(
    name: str,
    request: CorrectionSourceUpdateRequest,
    config_svc: ConfigService = Depends(get_config_service),
) -> CorrectionSourceResponse | JSONResponse:
    """Change a Correction source; a blank password keeps the saved one."""
    try:
        source = config_svc.update_correction_source(
            name,
            new_name=request.name,
            changes=request.model_dump(exclude={"name", "password", "remove_password"}),
            password=request.password,
            remove_password=request.remove_password,
        )
    except CorrectionSourceNotFoundError as exc:
        return error_response(404, str(exc))
    except CorrectionSourceInUseError as exc:
        return error_response(409, str(exc), code="in_use")
    except CorrectionSourceExistsError as exc:
        return error_response(409, str(exc))
    except ValidationError as exc:
        return error_response(422, _describe(exc.errors()))
    logger.info("Updated Correction source: %s", source.name)
    return _to_response(source)


@router.delete("/{name}", response_model=RelayActionResponse)
async def delete_correction_source(
    name: str,
    config_svc: ConfigService = Depends(get_config_service),
) -> RelayActionResponse | JSONResponse:
    """Delete a saved Correction source; 409 ``in_use`` while a survey uses it."""
    try:
        removed = config_svc.remove_correction_source(name)
    except CorrectionSourceInUseError as exc:
        return error_response(409, str(exc), code="in_use")
    if not removed:
        return error_response(404, f"Correction source '{name}' not found")
    logger.info("Deleted Correction source: %s", name)
    return RelayActionResponse(
        status="ok", message=f"Correction source '{name}' deleted"
    )
