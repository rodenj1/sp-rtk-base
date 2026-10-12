"""The agent's HTTPS calls to the Portal, each signed with the base key.

Every request carries ``Authorization: Signature <proof>`` (made by the
protocol package) and the protocol version header.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from sp_rtk_portal_protocol import (
    CLAIM_TOKEN_PATH,
    CLAIMS_PATH,
    PROTOCOL_VERSION,
    VERSION_HEADER,
    BaseKey,
    Claimed,
    ClaimStart,
    ClaimStarted,
    ClaimToken,
    ErrorReply,
    Message,
    MessageError,
)

TIMEOUT_S = 10.0


class PortalUnreachableError(Exception):
    """No usable answer: network error, timeout, or an unexpected reply."""


class PortalApi:
    def __init__(
        self,
        base_url: str,
        *,
        now: Callable[[], datetime],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._now = now
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or self.base_url

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, key: BaseKey, path: str, message: Message) -> httpx.Response:
        # Made on first use: an unclaimed, idle agent opens nothing.
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_S
            )
        url = self.base_url + path
        body = json.dumps(message.to_json()).encode()
        headers = {
            "Authorization": key.authorization("POST", url, now=self._now(), body=body),
            VERSION_HEADER: str(PROTOCOL_VERSION),
            "Content-Type": "application/json",
        }
        try:
            return await self._client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise PortalUnreachableError(str(exc) or type(exc).__name__) from exc

    async def start_claim(self, key: BaseKey, message: ClaimStart) -> ClaimStarted:
        resp = await self._post(key, CLAIMS_PATH, message)
        if resp.status_code != 200:
            raise PortalUnreachableError(f"the Portal answered HTTP {resp.status_code}")
        try:
            return ClaimStarted.from_json(resp.json())
        except (ValueError, MessageError) as exc:
            raise PortalUnreachableError(
                "the Portal's answer wasn't understood"
            ) from exc

    async def poll_claim(self, key: BaseKey, device_code: str) -> Claimed | str:
        """The Claim, or the RFC 8628 error code the Portal answered."""
        resp = await self._post(key, CLAIM_TOKEN_PATH, ClaimToken(device_code))
        try:
            if resp.status_code == 200:
                return Claimed.from_json(resp.json())
            if resp.status_code == 400:
                return ErrorReply.from_json(resp.json()).error
        except (ValueError, MessageError) as exc:
            raise PortalUnreachableError(
                "the Portal's answer wasn't understood"
            ) from exc
        raise PortalUnreachableError(f"the Portal answered HTTP {resp.status_code}")
