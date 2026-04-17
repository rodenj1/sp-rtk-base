"""Health check API endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from sp_base import __version__

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health_check() -> JSONResponse:
    """Return application health status and version.

    Returns:
        JSON response with status and version.
    """
    return JSONResponse(
        content={"status": "ok", "version": __version__},
    )
