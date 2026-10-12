"""``GET /api/portal/identity``: which Claimed base this is (spec #82).

Unauthenticated, like the rest of the local UI (ADR 0001), and read-only: it
returns only the base's ``<id>``, which the Portal agent writes at Claim. The
Portal's pages fetch it at a LAN address to tell whether the browser is on
the base's LAN, so the Portal's origin, and only it, may read it
cross-origin. An unclaimed base answers 404 with no body.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from sp_rtk_base.portal_agent.protocol import portal_url, state_path
from sp_rtk_base.portal_agent.state import read_base_id

router = APIRouter(prefix="/api/portal", tags=["portal"])


def _portal_origin() -> str:
    parts = urlsplit(portal_url())
    return f"{parts.scheme}://{parts.netloc}"


@router.get("/identity")
async def identity(request: Request) -> Response:
    base_id = read_base_id(state_path())
    response: Response = (
        Response(status_code=404) if base_id is None else JSONResponse({"id": base_id})
    )
    origin = _portal_origin()
    if request.headers.get("origin") == origin:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Vary"] = "Origin"
    response.headers["Cache-Control"] = "no-store"
    return response
