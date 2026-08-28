"""Profile management API endpoints.

CRUD + export/import over :class:`sp_rtk_base.services.profile_store.ProfileStore`
— the only filesystem toucher for GPS receiver profiles.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from sp_rtk_base.models.api_models import (
    ProfileDetailResponse,
    ProfileListItem,
    ProfileListResponse,
    ProfileRenameRequest,
    RelayActionResponse,
)
from sp_rtk_base.models.hardware_identity import (
    HARDWARE_UNKNOWN,
    HardwareConfidence,
    default_selection,
    identity_from_target,
    incompatible_reason,
    is_compatible,
)
from sp_rtk_base.models.profile_models import Profile
from sp_rtk_base.services import get_device_service, get_profile_store
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.profile_store import (
    ProfileBusinessRuleError,
    ProfileConflictError,
    ProfileImmutableError,
    ProfileNotFoundError,
    ProfileStore,
    ProfileStoreError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/profiles", tags=["profiles"])

#: Maps each ProfileStore error to the HTTP status it carries — kept as a
#: single table rather than a repeated except-clause ladder in every
#: mutating endpoint below.
_STATUS_BY_ERROR: dict[type[ProfileStoreError], int] = {
    ProfileNotFoundError: 404,
    ProfileImmutableError: 403,
    ProfileConflictError: 409,
    ProfileBusinessRuleError: 400,
}


def _error_response(exc: ProfileStoreError) -> JSONResponse:
    return JSONResponse(
        status_code=_STATUS_BY_ERROR[type(exc)],
        content={"status": "error", "message": str(exc)},
    )


def _detail(store: ProfileStore, profile: Profile) -> ProfileDetailResponse:
    return ProfileDetailResponse(
        profile=profile, is_builtin=store.is_builtin(profile.name)
    )


@router.get("", response_model=ProfileListResponse)
async def list_profiles(
    store: ProfileStore = Depends(get_profile_store),
    device: DeviceService = Depends(get_device_service),
) -> ProfileListResponse:
    """List every profile, built-ins before customs, alphabetical within each.

    Tags each profile with its compatibility against the connected
    receiver's resolved hardware identity (see ``models.hardware_identity``)
    and carries that identity plus the deterministic default pick.
    """
    info = device.device_info
    identity = identity_from_target(
        info.hardware_target if info else HARDWARE_UNKNOWN,
        info.hardware_confidence if info else HardwareConfidence.UNKNOWN,
    )

    profiles = store.list_profiles()
    items = [
        ProfileListItem(
            profile=p,
            is_builtin=store.is_builtin(p.name),
            compatible=is_compatible(identity, p.hardware),
            incompatible_reason=incompatible_reason(identity, p.hardware),
        )
        for p in profiles
    ]
    return ProfileListResponse(
        profiles=items,
        count=len(items),
        hardware_target=identity.target,
        hardware_confidence=identity.confidence,
        default_selection=default_selection(
            identity, [(p.name, p.hardware) for p in profiles]
        ),
    )


@router.get("/{name}", response_model=ProfileDetailResponse)
async def get_profile(
    name: str,
    store: ProfileStore = Depends(get_profile_store),
) -> ProfileDetailResponse | JSONResponse:
    """Get a single profile by name."""
    profile = store.get_profile(name)
    if profile is None:
        return _error_response(ProfileNotFoundError(f"Profile '{name}' not found"))
    return _detail(store, profile)


@router.get("/{name}/export", response_model=Profile)
async def export_profile(
    name: str,
    store: ProfileStore = Depends(get_profile_store),
) -> Profile | JSONResponse:
    """Export a profile in the shape ``POST /api/profiles/import`` accepts."""
    try:
        return store.export_profile(name)
    except ProfileNotFoundError as exc:
        return _error_response(exc)


@router.post("", response_model=ProfileDetailResponse, status_code=201)
async def create_profile(
    request: Profile,
    store: ProfileStore = Depends(get_profile_store),
) -> ProfileDetailResponse | JSONResponse:
    """Create a new custom profile."""
    try:
        created = store.create_profile(request)
    except ProfileStoreError as exc:
        return _error_response(exc)
    logger.info("Created profile: %s", created.name)
    return _detail(store, created)


@router.post("/import", response_model=ProfileDetailResponse, status_code=201)
async def import_profile(
    request: dict[str, object],
    store: ProfileStore = Depends(get_profile_store),
) -> ProfileDetailResponse | JSONResponse:
    """Import a previously exported profile document.

    Validated as a :class:`Profile` — an unknown ``version`` (or any
    other schema violation) is a 422; a name collision is a 409.
    """
    try:
        imported = store.import_profile(request)
    except ValidationError as exc:
        return JSONResponse(
            status_code=422, content={"status": "error", "message": str(exc)}
        )
    except ProfileStoreError as exc:
        return _error_response(exc)
    logger.info("Imported profile: %s", imported.name)
    return _detail(store, imported)


@router.patch("/{name}", response_model=ProfileDetailResponse)
async def rename_profile(
    name: str,
    request: ProfileRenameRequest,
    store: ProfileStore = Depends(get_profile_store),
) -> ProfileDetailResponse | JSONResponse:
    """Rename an existing custom profile."""
    try:
        renamed = store.rename_profile(name, request.new_name)
    except ProfileStoreError as exc:
        return _error_response(exc)
    logger.info("Renamed profile: %s -> %s", name, request.new_name)
    return _detail(store, renamed)


@router.delete("/{name}", response_model=RelayActionResponse)
async def delete_profile(
    name: str,
    store: ProfileStore = Depends(get_profile_store),
) -> RelayActionResponse | JSONResponse:
    """Delete a custom profile."""
    try:
        store.delete_profile(name)
    except ProfileStoreError as exc:
        return _error_response(exc)

    logger.info("Deleted profile: %s", name)
    return RelayActionResponse(status="ok", message=f"Profile '{name}' deleted")
