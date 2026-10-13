"""The agent's HTTPS calls to the Portal, each signed with the base key.

Every request carries ``Authorization: Signature <proof>`` (made by the
protocol package) and the protocol version header.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
from sp_rtk_portal_protocol import (
    CHECK_IN_PATH,
    CLAIM_TOKEN_PATH,
    CLAIMS_PATH,
    POLL_PATH,
    PROTOCOL_VERSION,
    VERSION_HEADER,
    BaseKey,
    CheckIn,
    CheckInReply,
    Claimed,
    ClaimStart,
    ClaimStarted,
    ClaimToken,
    ErrorReply,
    Message,
    MessageError,
    Poll,
    PollReply,
    UpgradeRequired,
)

TIMEOUT_S = 10.0


class PortalUnreachableError(Exception):
    """No usable answer: network error, timeout, or an unexpected reply."""


class UpgradeRequiredError(Exception):
    """426: the Portal no longer supports this agent's protocol version."""

    def __init__(self, reply: UpgradeRequired) -> None:
        super().__init__(reply.message or "upgrade required")
        self.reply = reply


class UnclaimedError(Exception):
    """401 ``unclaimed``: the Portal has no base with this key (Un-claimed
    on the Portal)."""


R = TypeVar("R", PollReply, CheckInReply)


class PortalApi:
    def __init__(
        self,
        base_url: str,
        *,
        now: Callable[[], datetime],
        transport: httpx.AsyncBaseTransport | None = None,
        protocol_version: int = PROTOCOL_VERSION,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.protocol_version = protocol_version
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
            VERSION_HEADER: str(self.protocol_version),
            "Content-Type": "application/json",
        }
        try:
            return await self._client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise PortalUnreachableError(str(exc) or type(exc).__name__) from exc

    async def start_claim(self, key: BaseKey, message: ClaimStart) -> ClaimStarted:
        resp = await self._post(key, CLAIMS_PATH, message)
        _raise_for_upgrade(resp)
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

    async def poll(self, key: BaseKey, message: Poll) -> PollReply:
        return await self._exchange(key, POLL_PATH, message, PollReply)

    async def check_in(self, key: BaseKey, message: CheckIn) -> CheckInReply:
        return await self._exchange(key, CHECK_IN_PATH, message, CheckInReply)

    async def _exchange(
        self, key: BaseKey, path: str, message: Message, reply: type[R]
    ) -> R:
        """A Claimed base's request. Raises :class:`UpgradeRequiredError`,
        :class:`UnclaimedError` or :class:`PortalUnreachableError`."""
        resp = await self._post(key, path, message)
        _raise_for_upgrade(resp)
        if resp.status_code == 401 and _error(resp) == ErrorReply.UNCLAIMED:
            raise UnclaimedError("the Portal has no base with this key")
        if resp.status_code != 200:
            raise PortalUnreachableError(f"the Portal answered HTTP {resp.status_code}")
        try:
            return reply.from_json(resp.json())
        except (ValueError, MessageError) as exc:
            raise PortalUnreachableError(
                "the Portal's answer wasn't understood"
            ) from exc


def _error(resp: httpx.Response) -> str | None:
    try:
        return ErrorReply.from_json(resp.json()).error
    except (ValueError, MessageError):
        return None


def _raise_for_upgrade(resp: httpx.Response) -> None:
    if resp.status_code != 426:
        return
    try:
        reply = UpgradeRequired.from_json(resp.json())
    except (ValueError, MessageError):
        reply = UpgradeRequired(min_version=0)
    raise UpgradeRequiredError(reply)
