"""The agent's HTTPS calls to the Portal, each signed with the base key.

Every request carries ``Authorization: Signature <proof>`` (made by the
protocol package) and the protocol version header.
"""

from __future__ import annotations

import json
import math
import ssl
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TypeVar, cast
from urllib.parse import urlsplit

import httpcore
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

from sp_rtk_base.portal_agent.doh import Names, SystemNames

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


class _NamedBackend(httpcore.AsyncNetworkBackend):
    """httpcore's network backend, connecting to the address ``names`` gives
    for a host instead of resolving it. Only the TCP connection changes:
    httpcore still does TLS (SNI, the certificate check) and the ``Host``
    header for the URL's own host."""

    def __init__(self, names: Names) -> None:
        self._names = names
        self._inner = cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        address = await self._names.address(host)
        try:
            return await self._inner.connect_tcp(
                address or host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        except (httpcore.ConnectError, httpcore.ConnectTimeout):
            if address is not None:
                self._names.forget(host)
            raise

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:  # pragma: no cover - never used
        return await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:  # pragma: no cover
        await self._inner.sleep(seconds)


def ipv4_transport(
    names: Names | None = None, *, verify: ssl.SSLContext | bool = True
) -> httpx.AsyncHTTPTransport:
    """httpx's transport, on IPv4 only: binding to ``0.0.0.0`` makes it look
    up A records alone. The agent's unit gives it no IPv6, so an AAAA
    attempt could only fail.

    With ``names`` (the agent's :class:`~.doh.DohResolver`), it connects to
    the address they give for the host, still as the host for TLS and
    HTTP."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", verify=verify)
    if names is not None:
        # httpx has no option for a network backend; its pool takes one.
        transport._pool = httpcore.AsyncConnectionPool(  # pyright: ignore[reportPrivateUsage]
            ssl_context=ssl_context(verify),
            local_address="0.0.0.0",
            network_backend=_NamedBackend(names),
        )
    return transport


def ssl_context(verify: ssl.SSLContext | bool) -> ssl.SSLContext:
    if isinstance(verify, ssl.SSLContext):
        return verify
    return httpx.create_ssl_context(verify=verify)


class PortalApi:
    def __init__(
        self,
        base_url: str,
        *,
        now: Callable[[], datetime],
        transport: httpx.AsyncBaseTransport | None = None,
        protocol_version: int = PROTOCOL_VERSION,
        names: Names | None = None,
    ) -> None:
        """``names``: where to connect for the Portal host (the agent passes
        its :class:`~.doh.DohResolver`); by default, by name."""
        self.base_url = base_url.rstrip("/")
        self.names: Names = names if names is not None else SystemNames()
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
                transport=self._transport or ipv4_transport(self.names),
                timeout=TIMEOUT_S,
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
