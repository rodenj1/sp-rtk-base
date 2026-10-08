"""Health check API endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sp_rtk_base_relay import __version__ as relay_version

from sp_rtk_base import __version__

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health_check() -> JSONResponse:
    """Return application health status and the SP-Base and Relay versions.

    After an Update, the updater's health check reads both versions here
    (ADR 0005), so ``relay_version`` is part of the contract.

    Returns:
        JSON response with status, version and relay_version.
    """
    return JSONResponse(
        content={
            "status": "ok",
            "version": __version__,
            "relay_version": relay_version,
        },
    )
