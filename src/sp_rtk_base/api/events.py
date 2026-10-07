"""Events API endpoints — REST polling and WebSocket streaming.

Provides access to relay engine events both via REST (polling)
and WebSocket (real-time push).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from sp_rtk_base.models.api_models import EventListResponse, EventResponse
from sp_rtk_base.services import get_relay_service
from sp_rtk_base.services.relay_service import RelayService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("", response_model=EventListResponse)
async def get_recent_events(
    count: int = 50,
    relay: RelayService = Depends(get_relay_service),
) -> EventListResponse:
    """Get recent events from the relay engine ring buffer.

    Args:
        count: Maximum number of events to return (default 50).
    """
    events_dicts = relay.get_recent_events(count)
    events = [EventResponse.model_validate(e) for e in events_dicts]
    return EventListResponse(events=events, count=len(events))


@router.websocket("/ws")
async def websocket_events(
    websocket: WebSocket,
    relay: RelayService = Depends(get_relay_service),
) -> None:
    """WebSocket endpoint for real-time event streaming.

    Opens this client's own live event stream and pushes each event to
    it as a JSON message, with a ``{"type": "ping"}`` keepalive when the
    Relay is quiet.  The stream follows every relay start and stop until
    the client disconnects or the server shuts down.
    """
    await websocket.accept()
    logger.info("WebSocket client connected for event streaming")
    stream = relay.stream_events()

    try:
        while True:
            # A vanished client (browser tab closed, network drop) shows
            # up here: the queue.get() blocks for the full timeout, we
            # come back to send a keepalive ping, and the underlying
            # ASGI socket has already been closed by uvicorn.  Without
            # this guard, send_json raises a RuntimeError that we
            # catch at the outer ``except Exception`` and dump a noisy
            # multi-frame traceback per orphaned client per heartbeat.
            if websocket.client_state != WebSocketState.CONNECTED:
                logger.debug("WebSocket no longer connected; exiting handler")
                return

            try:
                event_dict: dict[str, Any] = await asyncio.wait_for(
                    stream.get(),
                    timeout=5.0,
                )
                await websocket.send_json(event_dict)
            except asyncio.TimeoutError:
                # Send keepalive ping — but catch the disconnect race
                # described above instead of letting it surface as a
                # logger.exception() at the outer level.
                try:
                    await websocket.send_json({"type": "ping"})
                except (WebSocketDisconnect, RuntimeError) as exc:
                    logger.debug("WebSocket client gone during keepalive: %s", exc)
                    return
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception:
        logger.exception("WebSocket error")
    finally:
        stream.close()
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass
