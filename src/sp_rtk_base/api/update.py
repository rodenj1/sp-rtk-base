"""Update API: the check for an Available update.

``GET /api/update`` returns the last good check (kept when a later one
fails) and whether a check is running or the latest one failed.
``POST /api/update/check`` is Check now: it checks PyPI, joining a check
already running, and returns the new status.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from sp_rtk_base.services import get_update_check_service
from sp_rtk_base.services.update_check import UpdateCheckService, UpdateCheckStatus

router = APIRouter(prefix="/api/update", tags=["update"])


@router.get("", response_model=UpdateCheckStatus)
async def get_update_status(
    service: UpdateCheckService = Depends(get_update_check_service),
) -> UpdateCheckStatus:
    """The update check as it stands; never runs a check."""
    return service.status


@router.post("/check", response_model=UpdateCheckStatus)
async def check_for_update(
    service: UpdateCheckService = Depends(get_update_check_service),
) -> UpdateCheckStatus:
    """Check now. A failed check still answers 200, with ``last_check_failed``."""
    return await service.check_now()
