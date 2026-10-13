"""The agent's HTTPS calls to the Portal, each signed with the base key.

Every request carries ``Authorization: Signature <proof>`` (made by the
protocol package) and the protocol version header.
"""

from __future__ import annotations

import json
import math
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
    RELEASE_PATH,
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
    Release,
    UpgradeRequired,
)
from sp_rtk_portal_protocol.tunnel import signed_url

TIMEOUT_S = 10.0
RELEASE_TIMEOUT_S = 5.0
"""A release is best effort: the Operator waits for it on the local page."""


class PortalUnreachableError(Exception):
    """No usable answer: network error, timeout, or an unexpected reply."""


class UpgradeRequiredError(Exception):
    """426: the Portal no longer supports this agent's protocol version."""

    def __init__(self, reply: UpgradeRequired) -> None:
        super().__init__(reply.message or "upgrade required")
        self.reply = reply


class RateLimitedError(Exception):
    """429: the Portal says this agent asks too often. Wait ``retry_after``
    seconds (``None``: it didn't say) before asking again."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("the Portal asked the agent to slow down")
        self.retry_after = retry_after


MAX_RETRY_AFTER_S = 24 * 3600
"""A longer ``Retry-After`` is read as this: the agent always comes back."""


class UnclaimedError(Exception):
    """401 ``unclaimed``: the Portal has no base with this key (Un-claimed
    on the Portal)."""


R = TypeVar("R", PollReply, CheckInReply)


def ipv4_transport() -> httpx.AsyncHTTPTransport:
    """httpx's transport, on IPv4 only: binding to ``0.0.0.0`` makes it look
    up A records alone. The agent's unit gives it no IPv6, so an AAAA
    attempt could only fail."""
    return httpx.AsyncHTTPTransport(local_address="0.0.0.0")


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

    def tunnel_headers(self, key: BaseKey, url: str, nonce: str) -> dict[str, str]:
        """Headers of the tunnel open: a GET signed for ``url`` whose proof
        echoes the Wake ``nonce``, and the protocol version."""
        return {
            "Authorization": key.authorization(
                "GET", signed_url(url), now=self._now(), nonce=nonce
            ),
            VERSION_HEADER: str(self.protocol_version),
        }

    async def _post(
        self,
        key: BaseKey,
        path: str,
        message: Message,
        timeout: float = TIMEOUT_S,
    ) -> httpx.Response:
        # Made on first use: an unclaimed, idle agent opens nothing.
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport or ipv4_transport(), timeout=TIMEOUT_S
            )
        url = self.base_url + path
        body = json.dumps(message.to_json()).encode()
        headers = {
            "Authorization": key.authorization("POST", url, now=self._now(), body=body),
            VERSION_HEADER: str(self.protocol_version),
            "Content-Type": "application/json",
        }
        try:
            return await self._client.post(
                url, content=body, headers=headers, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise PortalUnreachableError(str(exc) or type(exc).__name__) from exc

    async def start_claim(self, key: BaseKey, message: ClaimStart) -> ClaimStarted:
        resp = await self._post(key, CLAIMS_PATH, message)
        _raise_for_upgrade(resp)
        _raise_for_rate_limit(resp)
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

    async def release(self, key: BaseKey) -> None:
        """A local Un-claim: ask the Portal to forget the base with this key.
        Raises :class:`UnclaimedError` (it already had),
        :class:`UpgradeRequiredError` or :class:`PortalUnreachableError`."""
        resp = await self._post(key, RELEASE_PATH, Release(), RELEASE_TIMEOUT_S)
        _raise_for_upgrade(resp)
        _raise_for_unclaimed(resp)
        if resp.status_code != 200:
            raise PortalUnreachableError(f"the Portal answered HTTP {resp.status_code}")

    async def _exchange(
        self, key: BaseKey, path: str, message: Message, reply: type[R]
    ) -> R:
        """A Claimed base's request. Raises :class:`UpgradeRequiredError`,
        :class:`UnclaimedError` or :class:`PortalUnreachableError`."""
        resp = await self._post(key, path, message)
        _raise_for_upgrade(resp)
        _raise_for_unclaimed(resp)
        _raise_for_rate_limit(resp)
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


def _raise_for_unclaimed(resp: httpx.Response) -> None:
    if resp.status_code == 401 and _error(resp) == ErrorReply.UNCLAIMED:
        raise UnclaimedError("the Portal has no base with this key")


def _raise_for_rate_limit(resp: httpx.Response) -> None:
    if resp.status_code != 429:
        return
    try:
        seconds = float(resp.headers.get("retry-after", ""))
    except ValueError:
        seconds = math.nan  # absent, or an HTTP date: back off our own way
    retry_after = min(seconds, MAX_RETRY_AFTER_S) if seconds >= 0 else None
    raise RateLimitedError(retry_after)
