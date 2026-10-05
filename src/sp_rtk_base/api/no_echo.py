"""Refusing a request without quoting it back (passwords are write-only).

pydantic and FastAPI quote the input in their validation messages, and the
input can be a password (Correction sources, #192; destinations, #181). A
router built with ``route_class=NoEchoRoute`` answers a malformed body with
only each field and its problem; :func:`describe` does the same for a
model-layer ``ValidationError``.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute


def describe(errors: Sequence[Any], fallback: str = "Invalid request") -> str:
    """Validation errors as text, without the values sent."""
    parts: list[str] = []
    for error in errors:
        loc = [str(p) for p in error.get("loc", ()) if p not in ("body",)]
        msg = str(error.get("msg", "invalid"))
        parts.append(f"{'.'.join(loc)}: {msg}" if loc else msg)
    return "; ".join(parts) or fallback


def error_response(
    status_code: int, message: str, code: str | None = None
) -> JSONResponse:
    """The ``{"status": "error", "message", "code"?}`` body routers answer with."""
    content = {"status": "error", "message": message}
    if code is not None:
        content["code"] = code  # what a client branches on
    return JSONResponse(status_code=status_code, content=content)


class NoEchoRoute(APIRoute):
    """Answers a malformed request body without quoting it back."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def _handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return error_response(422, describe(exc.errors()))

        return _handle
