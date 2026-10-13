"""The agent's control API on a Unix socket.

Permissioned to the sp-rtk-base service user twice over: the socket file
is ``0600`` (owned by the user the agent runs as, the service user), and
every connection's peer uid (``SO_PEERCRED``) must be an allowed one,
by default the agent's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import socket
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import httpx
from sp_rtk_portal_protocol import (
    PROTOCOL_VERSION,
    BaseKey,
    Claimed,
    ClaimStart,
    ClaimStarted,
    ErrorReply,
    LanAddress,
)

from sp_rtk_base import __version__
from sp_rtk_base.portal_agent import doorbell, lan
from sp_rtk_base.portal_agent.connection import UPGRADE_MESSAGE, Connection
from sp_rtk_base.portal_agent.portal_api import (
    PortalApi,
    PortalUnreachableError,
    RateLimitedError,
    UnclaimedError,
    UpgradeRequiredError,
)
from sp_rtk_base.portal_agent.protocol import (
    AgentState,
    AgentStatus,
    state_path,
)
from sp_rtk_base.portal_agent.protocol import portal_url as default_portal_url
from sp_rtk_base.portal_agent.state import (
    Claim,
    load_claim,
    load_paused,
    save_claim,
    save_paused,
    wipe_claim,
)
from sp_rtk_base.portal_agent.tunnel import APP_PORT, Connect, connect_websocket

logger = logging.getLogger(__name__)

_MAX_REQUEST = 64 * 1024


class ClaimStartError(Exception):
    """A Claim couldn't start. The message is for the Operator."""


def _too_many_claims(retry_after: float | None) -> str:
    """What the Operator reads when the Portal refuses a Claim start 429."""
    text = "Too many Claims were started from this network."
    if retry_after is None:
        return f"{text} Try again later."
    minutes = max(1, math.ceil(retry_after / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return f"{text} Try again in {minutes} {unit}."


class NotClaimedError(Exception):
    """Pause and resume need a Claimed base."""


@dataclass
class _Pairing:
    key: BaseKey
    started: ClaimStarted
    expires_at: datetime
    task: asyncio.Task[None] | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


EXPIRED = "The code expired before it was approved. Connect again for a new one."
REFUSED = "The Portal refused the Claim. Connect again to retry."
UNCLAIMED = "Un-claimed."
UNCLAIMED_UNTOLD = (
    "Un-claimed here, but the Portal couldn't be told: Un-claim the base on "
    "the Portal too."
)
UNCLAIMED_ON_PORTAL = "This base was Un-claimed on the Portal."
SLOW_DOWN_S = 5
"""RFC 8628 §3.5: a ``slow_down`` adds this to the poll interval, for good."""


class PortalAgent:
    """The agent's state: not connected, pairing or connected (Claimed).

    Unclaimed it is idle and contacts nothing. :meth:`unclaim` (the local
    page's Un-claim) and a 401 ``unclaimed`` from the Portal both wipe the
    key and return it there. "Connect to Portal" calls
    :meth:`start_claim`: it makes a base key, asks the Portal for a pairing
    code and polls (RFC 8628) until the Operator approves it, the code
    expires or the Portal refuses. Once Claimed, the key and Claim are on
    disk (:mod:`.state`) and survive restarts and reinstalls, and a
    :class:`~.connection.Connection` polls and Checks in (started by
    :meth:`start`, which :func:`serve` calls).
    """

    def __init__(
        self,
        state_dir: Path | None = None,
        *,
        portal_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        name: str | None = None,
        software_version: str | None = None,
        resolve: doorbell.Resolve = doorbell.system_resolve,
        lan_addresses: Callable[[], list[LanAddress]] = lan.lan_addresses,
        protocol_version: int = PROTOCOL_VERSION,
        tunnel_connect: Connect = connect_websocket,
        app_port: int = APP_PORT,
        open_udp: doorbell.OpenUdp = doorbell.open_udp,
    ) -> None:
        """``tunnel_connect``, ``app_port`` and ``open_udp`` exist for tests (a
        websocket connector reaching a test Portal, the stub UI's port, and
        UDP reaching a stub Doorbell server). Nothing the Portal sends can
        change any of them."""
        self._state_dir = state_dir if state_dir is not None else state_path()
        self._api = PortalApi(
            portal_url if portal_url is not None else default_portal_url(),
            now=now,
            transport=transport,
            protocol_version=protocol_version,
        )
        self._resolve = resolve
        self._lan_addresses = lan_addresses
        self._tunnel_connect = tunnel_connect
        self._app_port = app_port
        self._open_udp = open_udp
        self._now = now
        self._sleep = sleep
        self._name = name if name is not None else socket.gethostname()
        self._version = (
            software_version if software_version is not None else __version__
        )
        loaded = load_claim(self._state_dir)
        self._key: BaseKey | None = loaded[0] if loaded else None
        self._claim: Claim | None = loaded[1] if loaded else None
        self._connection: Connection | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._pairing: _Pairing | None = None
        self._message: str | None = None
        self._paused = load_paused(self._state_dir)
        self._lock = asyncio.Lock()

    def status(self) -> AgentStatus:
        if self._claim is not None:
            c = self._claim
            conn = self._connection
            state: AgentState = "paused" if self._paused else "connected"
            if conn is None:
                return AgentStatus(
                    state=state,
                    base_id=c.base_id,
                    account=c.account,
                    portal=c.portal,
                    last_contact=datetime.fromisoformat(c.last_contact),
                )
            return AgentStatus(
                state=state,
                base_id=c.base_id,
                account=c.account,
                portal=c.portal,
                last_contact=conn.last_contact,
                message=conn.upgrade_message,
                wake_mode=conn.wake_mode,
                poll_interval=conn.poll_interval,
                metered=conn.metered,
                doorbell=conn.doorbell,
                config_revision=conn.config.revision,
                tunnel_url=conn.config.tunnel_url,
                notices=conn.notices,
                needs_upgrade=conn.needs_upgrade,
                tunnel=conn.tunnel.state,
                tunnel_since=conn.tunnel.since,
                tunnel_streams=conn.tunnel.streams,
                sessions=conn.sessions,
                tunnel_events=list(conn.tunnel.events),
            )
        if self._pairing is not None:
            p = self._pairing
            return AgentStatus(
                state="pairing",
                user_code=p.started.user_code,
                approve_url=p.started.verification_uri_complete,
                expires_at=p.expires_at,
            )
        return AgentStatus(state="not_connected", message=self._message)

    async def start_claim(self) -> AgentStatus:
        """Ask the Portal for a pairing code. Raises :class:`ClaimStartError`."""
        async with self._lock:
            if self._claim is not None:
                raise ClaimStartError("This base is already Claimed.")
            if self._pairing is not None:
                return self.status()
            key = BaseKey.generate()
            try:
                started = await self._api.start_claim(
                    key, ClaimStart(name=self._name, software_version=self._version)
                )
            except UpgradeRequiredError as exc:
                logger.warning("Couldn't start a Claim: %s", exc)
                raise ClaimStartError(UPGRADE_MESSAGE) from exc
            except RateLimitedError as exc:
                logger.warning("Couldn't start a Claim: too many from this network")
                raise ClaimStartError(_too_many_claims(exc.retry_after)) from exc
            except PortalUnreachableError as exc:
                logger.warning("Couldn't start a Claim: %s", exc)
                raise ClaimStartError(f"Couldn't reach the Portal ({exc}).") from exc
            pairing = _Pairing(
                key, started, self._now() + timedelta(seconds=started.expires_in)
            )
            self._pairing, self._message = pairing, None
            pairing.task = asyncio.create_task(self._poll(pairing))
            logger.info("Pairing started; code %s", started.user_code)
            return self.status()

    async def _poll(self, pairing: _Pairing) -> None:
        interval = max(pairing.started.interval, 1)
        while True:
            await self._sleep(interval)
            if self._now() >= pairing.expires_at:
                self._end_pairing(EXPIRED)
                return
            try:
                result = await self._api.poll_claim(
                    pairing.key, pairing.started.device_code
                )
            except PortalUnreachableError as exc:
                logger.info("Claim poll failed, will retry: %s", exc)
                continue
            if isinstance(result, Claimed):
                self._claimed(pairing, result)
                return
            if result == ErrorReply.AUTHORIZATION_PENDING:
                continue
            if result == ErrorReply.SLOW_DOWN:
                interval += SLOW_DOWN_S
                continue
            self._end_pairing(
                EXPIRED if result == ErrorReply.EXPIRED_TOKEN else REFUSED
            )
            return

    def _claimed(self, pairing: _Pairing, claimed: Claimed) -> None:
        now = self._now().isoformat()
        claim = Claim(
            base_id=claimed.base_id,
            account=claimed.account,
            portal=self._api.host,
            claimed_at=now,
            last_contact=now,
        )
        save_claim(self._state_dir, pairing.key, claim)
        self._key, self._claim, self._pairing = pairing.key, claim, None
        logger.info("Claimed by %s as %s", claimed.account, claimed.base_id)
        self.start()

    def start(self) -> None:
        """Start polling if Claimed (and not already). Needs a running loop."""
        if self._claim is None or self._key is None or self._connection_task:
            return
        self._connection = Connection(
            state_dir=self._state_dir,
            key=self._key,
            claim=self._claim,
            api=self._api,
            now=self._now,
            sleep=self._sleep,
            software_version=self._version,
            resolve=self._resolve,
            lan_addresses=self._lan_addresses,
            tunnel_connect=self._tunnel_connect,
            app_port=self._app_port,
            paused=self._paused,
            open_udp=self._open_udp,
        )
        self._connection_task = asyncio.create_task(
            self._run_connection(self._connection)
        )

    async def _run_connection(self, connection: Connection) -> None:
        await connection.run()
        if connection.unclaimed:
            async with self._lock:
                if self._connection is connection:
                    self._forget(UNCLAIMED_ON_PORTAL)

    async def unclaim(self) -> AgentStatus:
        """Un-claim: stop polling (closing the tunnel), tell the Portal with
        a signed release (best effort), wipe the key and go idle. A no-op
        when not Claimed."""
        async with self._lock:
            if self._claim is None or self._key is None:
                return self.status()
            task, self._connection_task = self._connection_task, None
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            message = UNCLAIMED
            try:
                await self._api.release(self._key)
            except UnclaimedError:
                pass  # the Portal had already forgotten it
            except (PortalUnreachableError, UpgradeRequiredError) as exc:
                logger.warning("Couldn't tell the Portal about the Un-claim: %s", exc)
                message = UNCLAIMED_UNTOLD
            self._forget(message)
            return self.status()

    def _forget(self, message: str) -> None:
        wipe_claim(self._state_dir)
        logger.info(
            "Un-claimed %s: %s", self._claim.base_id if self._claim else "?", message
        )
        self._key = self._claim = self._connection = None
        self._connection_task = None
        self._message = message
        self._paused = False  # a new Claim starts with remote access on

    async def set_paused(self, paused: bool) -> AgentStatus:
        """Pause or resume remote access. Raises :class:`NotClaimedError`."""
        if self._claim is None:
            raise NotClaimedError
        self._paused = paused
        if self._connection is not None:
            await self._connection.set_paused(paused)
        else:
            save_paused(self._state_dir, paused)
        return self.status()

    def _end_pairing(self, message: str) -> None:
        logger.info("Pairing ended: %s", message)
        self._pairing, self._message = None, message

    async def close(self) -> None:
        """Stop any pairing or polling and close connections."""
        pairing = self._pairing
        tasks = [self._connection_task]
        if pairing is not None:
            tasks.append(pairing.task)
        for task in tasks:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._connection_task = None
        await self._api.aclose()


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock: socket.socket | None = writer.get_extra_info("socket")
    if sock is None:  # pragma: no cover - asyncio always provides it
        return None
    creds = sock.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


def _encode(reply: dict[str, Any]) -> bytes:
    return (json.dumps(reply) + "\n").encode()


async def _answer(agent: PortalAgent, line: bytes) -> dict[str, Any]:
    try:
        request = json.loads(line)
    except ValueError:
        return {"ok": False, "error": "bad request"}
    if not isinstance(request, dict):
        return {"ok": False, "error": "bad request"}
    op: Any = cast(dict[str, Any], request).get("op")
    if op == "status":
        return {"ok": True, "status": agent.status().model_dump(mode="json")}
    if op == "start_claim":
        try:
            status = await agent.start_claim()
        except ClaimStartError as exc:
            return {"ok": False, "error": "claim failed", "message": str(exc)}
        return {"ok": True, "status": status.model_dump(mode="json")}
    if op in ("pause", "resume"):
        try:
            status = await agent.set_paused(op == "pause")
        except NotClaimedError:
            return {"ok": False, "error": "not claimed"}
        return {"ok": True, "status": status.model_dump(mode="json")}
    if op == "unclaim":
        status = await agent.unclaim()
        return {"ok": True, "status": status.model_dump(mode="json")}
    return {"ok": False, "error": "unknown op"}


async def serve(
    path: Path,
    agent: PortalAgent | None = None,
    *,
    allowed_uids: frozenset[int] | None = None,
) -> asyncio.Server:
    """Listen on ``path`` (replacing a stale file) and answer requests.

    ``allowed_uids`` defaults to the agent's own uid.
    """
    the_agent = agent if agent is not None else PortalAgent()
    the_agent.start()
    uids = allowed_uids if allowed_uids is not None else frozenset({os.getuid()})

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            uid = _peer_uid(writer)
            if uid not in uids:
                logger.warning("Refused a control connection from uid %s", uid)
                writer.write(_encode({"ok": False, "error": "refused"}))
                await writer.drain()
                return
            while True:
                line = await reader.readline()
                if not line:
                    return
                writer.write(_encode(await _answer(the_agent, line)))
                await writer.drain()
        except (ConnectionError, asyncio.LimitOverrunError, ValueError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(handle, str(path), limit=_MAX_REQUEST)
    finally:
        os.umask(old_umask)
    path.chmod(0o600)
    return server
