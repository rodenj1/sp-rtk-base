"""Update API: the check for an Available update, and the Update itself.

``GET /api/update`` returns the last good check (kept when a later one
fails) and whether a check is running or the latest one failed.
``POST /api/update/check`` is Check now: it checks PyPI, joining a check
already running, and returns the new status. It is refused while an
Update runs.

``POST /api/update`` asks for an Update to the versions the client read
(ADR 0005), and ``GET /api/update/progress`` follows it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from sp_rtk_base.services import get_update_check_service, get_update_service
from sp_rtk_base.services.update_check import UpdateCheckService, UpdateCheckStatus
from sp_rtk_base.services.update_service import UpdateRefusedError, UpdateService
from sp_rtk_base.update.state import UPDATING_MESSAGE, UpdateStatus, Versions

NOT_AVAILABLE_MESSAGE = "There is no Available update. Check again."


class UpdateProgress(BaseModel):
    """Where the last Update is, and whether one runs now."""

    model_config = ConfigDict(frozen=True)

    status: UpdateStatus | None
    updating: bool


def _refused(code: str, message: str, command: str | None = None) -> JSONResponse:
    content = {"status": "error", "code": code, "message": message}
    if command is not None:
        content["command"] = command
    return JSONResponse(status_code=409, content=content)


def _progress(update: UpdateService) -> UpdateProgress:
    status = update.status()
    return UpdateProgress(
        status=status, updating=status is not None and not status.finished
    )


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
    update: UpdateService = Depends(get_update_service),
) -> UpdateCheckStatus | JSONResponse:
    """Check now, which also reads this host's Host setup again. A failed
    check still answers 200, with ``last_check_failed``.

    409 with ``code: updating`` while an Update runs.
    """
    if update.updating():
        return _refused("updating", UPDATING_MESSAGE)
    await update.refresh_host_setup()
    return await service.check_now()


@router.post(
    "", response_model=UpdateProgress, status_code=202, response_model_by_alias=True
)
async def request_update(
    target: Versions,
    service: UpdateCheckService = Depends(get_update_check_service),
    update: UpdateService = Depends(get_update_service),
) -> UpdateProgress | JSONResponse:
    """Ask the host to update to ``target``, the versions the client read.

    The host resolves the target itself and refuses if it differs. 409
    with a ``code`` when refused: ``not_available`` (no Available update
    is known), ``survey_running``, ``console_connected``, ``updating``, or a
    Host setup block (``host_setup_missing``, ``update_turned_off``,
    ``host_setup_outdated``, ``host_requirements_unknown``); nothing is
    written then. The two setup blocks add ``command``, the one-time
    ``install.sh`` re-run.
    """
    last = service.status.last_good
    if last is None or not last.available:
        return _refused("not_available", NOT_AVAILABLE_MESSAGE)
    try:
        await update.request(target)
    except UpdateRefusedError as exc:
        return _refused(exc.code, exc.message, exc.command)
    return _progress(update)


@router.get("/progress", response_model=UpdateProgress)
async def get_update_progress(
    update: UpdateService = Depends(get_update_service),
) -> UpdateProgress:
    """The last Update's ``status.json`` (``null`` if none), and whether
    one runs now."""
    return _progress(update)
