"""Destination management API endpoints.

Provides CRUD operations for relay destination profiles.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sp_base.models.api_models import (
    DestinationCreateRequest,
    DestinationListResponse,
    DestinationResponse,
    DestinationUpdateRequest,
    RelayActionResponse,
)
from sp_base.models.config_models import DestinationProfile, FilterProfile
from sp_base.services import get_config_service
from sp_base.services.config_service import ConfigService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/destinations", tags=["destinations"])


def _profile_to_response(profile: DestinationProfile) -> DestinationResponse:
    """Convert a DestinationProfile to an API response model."""
    return DestinationResponse(
        name=profile.name,
        type=profile.type,
        enabled=profile.enabled,
        config=profile.config,
        filter=profile.filter.model_dump(),
    )


@router.get("", response_model=DestinationListResponse)
async def list_destinations(
    config_svc: ConfigService = Depends(get_config_service),
) -> DestinationListResponse:
    """List all configured destinations."""
    destinations = config_svc.get_destinations()
    items = [_profile_to_response(d) for d in destinations]
    return DestinationListResponse(destinations=items, count=len(items))


@router.get("/{name}", response_model=DestinationResponse)
async def get_destination(
    name: str,
    config_svc: ConfigService = Depends(get_config_service),
) -> DestinationResponse | JSONResponse:
    """Get a single destination by name."""
    dest = config_svc.get_destination(name)
    if dest is None:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "message": f"Destination '{name}' not found"},
        )
    return _profile_to_response(dest)


@router.post("", response_model=DestinationResponse, status_code=201)
async def create_destination(
    request: DestinationCreateRequest,
    config_svc: ConfigService = Depends(get_config_service),
) -> DestinationResponse | JSONResponse:
    """Create a new destination profile.

    The destination is saved to config but not added to the running
    relay engine. Restart the relay or use the relay start endpoint
    to apply changes.
    """
    # Check for duplicate name
    existing = config_svc.get_destination(request.name)
    if existing is not None:
        return JSONResponse(
            status_code=409,
            content={
                "status": "error",
                "message": f"Destination '{request.name}' already exists",
            },
        )

    try:
        profile = DestinationProfile(
            name=request.name,
            type=request.type,  # type: ignore[arg-type]
            enabled=request.enabled,
            config=request.config,
            filter=FilterProfile.model_validate(request.filter),
        )
        config_svc.save_destination(profile)
        logger.info("Created destination: %s", request.name)
        return _profile_to_response(profile)
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(exc)},
        )


@router.put("/{name}", response_model=DestinationResponse)
async def update_destination(
    name: str,
    request: DestinationUpdateRequest,
    config_svc: ConfigService = Depends(get_config_service),
) -> DestinationResponse | JSONResponse:
    """Update an existing destination profile.

    Only the provided fields are updated; unset fields retain
    their current values.
    """
    existing = config_svc.get_destination(name)
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "message": f"Destination '{name}' not found"},
        )

    try:
        updated_data = existing.model_dump()
        if request.enabled is not None:
            updated_data["enabled"] = request.enabled
        if request.config is not None:
            updated_data["config"] = request.config
        if request.filter is not None:
            updated_data["filter"] = request.filter

        updated = DestinationProfile.model_validate(updated_data)
        config_svc.save_destination(updated)
        logger.info("Updated destination: %s", name)
        return _profile_to_response(updated)
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(exc)},
        )


@router.delete("/{name}", response_model=RelayActionResponse)
async def delete_destination(
    name: str,
    config_svc: ConfigService = Depends(get_config_service),
) -> RelayActionResponse | JSONResponse:
    """Delete a destination profile.

    Removes the destination from saved config. If the relay is
    running, the destination continues until the relay is restarted.
    """
    removed = config_svc.remove_destination(name)
    if not removed:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "message": f"Destination '{name}' not found"},
        )

    logger.info("Deleted destination: %s", name)
    return RelayActionResponse(status="ok", message=f"Destination '{name}' deleted")
