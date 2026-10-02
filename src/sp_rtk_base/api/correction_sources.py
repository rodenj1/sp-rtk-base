"""Correction source API endpoints (issue #192).

CRUD for the saved NTRIP Correction sources a Corrected survey-in reads,
following the destinations API's conventions. The password is write-only:
no response carries it, only ``has_password``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from sp_rtk_base.models.api_models import (
    CorrectionSourceCreateRequest,
    CorrectionSourceListResponse,
    CorrectionSourceResponse,
    CorrectionSourceUpdateRequest,
    RelayActionResponse,
)
from sp_rtk_base.models.config_models import (
    CorrectionSourceProfile,
    NtripCorrectionConfig,
)
from sp_rtk_base.services import get_config_service
from sp_rtk_base.services.config_service import (
    ConfigService,
    CorrectionSourceExistsError,
    CorrectionSourceNotFoundError,
)

logger = logging.getLogger(__name__)


def _describe(errors: Sequence[Any]) -> str:
    """Validation errors as text, without the values sent.

    pydantic and FastAPI quote the input in their messages, and the input
    here can be the password, so only each field and its problem are told.
    """
    parts: list[str] = []
    for error in errors:
        loc = [str(p) for p in error.get("loc", ()) if p not in ("body",)]
        msg = str(error.get("msg", "invalid"))
        parts.append(f"{'.'.join(loc)}: {msg}" if loc else msg)
    return "; ".join(parts) or "Invalid Correction source"


class _NoEchoRoute(APIRoute):
    """Answers a malformed request body without quoting it back."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def _handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return _error(422, _describe(exc.errors()))

        return _handle


router = APIRouter(
    prefix="/api/correction-sources",
    tags=["correction-sources"],
    route_class=_NoEchoRoute,
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


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"status": "error", "message": message}
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


@router.get("/{name}", response_model=CorrectionSourceResponse)
async def get_correction_source(
    name: str,
    config_svc: ConfigService = Depends(get_config_service),
) -> CorrectionSourceResponse | JSONResponse:
    """One saved Correction source."""
    source = config_svc.get_correction_source(name)
    if source is None:
        return _error(404, f"Correction source '{name}' not found")
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
        return _error(409, str(exc))
    except ValidationError as exc:
        return _error(422, _describe(exc.errors()))
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
        return _error(404, str(exc))
    except CorrectionSourceExistsError as exc:
        return _error(409, str(exc))
    except ValidationError as exc:
        return _error(422, _describe(exc.errors()))
    logger.info("Updated Correction source: %s", source.name)
    return _to_response(source)


@router.delete("/{name}", response_model=RelayActionResponse)
async def delete_correction_source(
    name: str,
    config_svc: ConfigService = Depends(get_config_service),
) -> RelayActionResponse | JSONResponse:
    """Delete a saved Correction source."""
    if not config_svc.remove_correction_source(name):
        return _error(404, f"Correction source '{name}' not found")
    logger.info("Deleted Correction source: %s", name)
    return RelayActionResponse(
        status="ok", message=f"Correction source '{name}' deleted"
    )
