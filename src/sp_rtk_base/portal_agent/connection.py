"""A Claimed base's contact with the Portal: its held tunnel, polls and
Check-ins (ADRs 0002, 0006).

:class:`Connection` runs for as long as the base is Claimed. Its
:attr:`~Connection.mode` says how the Portal reaches the base:

- **holding**: the tunnel is open and held (protocol version 2);
- **reconnecting**: a version 2 agent whose tunnel isn't open (yet, or any
  more);
- **polling**: a version 1 agent, reached only through Wakes.

It runs one loop. Each beat a version 2 agent first opens its tunnel to
hold it if it isn't open (so at once on Claim and on start, and again on
the beat after a drop), except while paused or refused as too old, and
waits for it to open or fail.

While it holds its tunnel, the tunnel carries everything (ticket #205):
no polls or Check-ins go over HTTPS. A Check-in goes on the tunnel when
it opens, every ``check_in_interval`` and whenever its contents change
(noticed within a beat); the Portal sends the agent config, metered flag,
notices and live Remote sessions on connect and on every change, and each
Remote session's end (:meth:`Connection._on_control`).

Otherwise it sends a Check-in over HTTPS when one is due (first thing,
then every ``check_in_interval``), then a poll, then sleeps the poll
interval (the metered one when the Operator marked the base metered).

Each reply (or update on the tunnel) goes through
:meth:`Connection._heard`: last contact, the metered flag, notices, and
the agent config block (applied only when its revision changes, then
persisted). A poll reply also goes through
:meth:`Connection._on_poll_reply`, which is where a Wake (#88) is handled
and the Remote sessions it lists go to the base's own record
(:class:`~.remote_record.RemoteRecord`).

Check-ins carry the pause too, as polls do.

A 401 ``unclaimed`` means the base was Un-claimed on the Portal: the loop
ends with :attr:`Connection.unclaimed` set, and the agent wipes its key.

A 429 means the Portal finds the agent too fast (ticket #98): a refused
poll waits its ``Retry-After``, or twice the poll interval when it gives
none, whichever is longer; a refused Check-in is tried again after its
``Retry-After`` (else half the Check-in interval) while polls go on.

A 426 means this agent's protocol version is no longer supported: it keeps
its key and Claim, stops polling and tries again once a day; the status says
``needs_upgrade``.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

from sp_rtk_portal_protocol import (
    HOLDING_VERSION,
    AgentConfig,
    BaseKey,
    CheckIn,
    CheckInReply,
    EndedSession,
    FrameError,
    LanAddress,
    MessageError,
    Poll,
    PollReply,
    TunnelClosed,
    TunnelUpdate,
    control_message,
    read_control,
)

from sp_rtk_base.portal_agent import agent_config
from sp_rtk_base.portal_agent.portal_api import (
    PortalApi,
    PortalUnreachableError,
    RateLimitedError,
    UnclaimedError,
    UpgradeRequiredError,
)
from sp_rtk_base.portal_agent.protocol import (
    UPDATE_TO_KEEP_USING,
    ConnectionMode,
    RemoteSession,
)
from sp_rtk_base.portal_agent.remote_record import RemoteRecord
from sp_rtk_base.portal_agent.state import (
    Claim,
    load_agent_config,
    save_agent_config,
    save_claim_file,
    save_paused,
)
from sp_rtk_base.portal_agent.tunnel import (
    APP_PORT,
    Connect,
    TunnelClient,
    connect_websocket,
)

logger = logging.getLogger(__name__)

UPGRADE_RETRY_S = 24 * 3600
"""After a 426, try again once a day (sp-rtk-base may have been Updated)."""


class Connection:
    def __init__(
        self,
        *,
        state_dir: Path,
        key: BaseKey,
        claim: Claim,
        api: PortalApi,
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]],
        software_version: str,
        lan_addresses: Callable[[], list[LanAddress]],
        tunnel_connect: Connect | None = None,
        app_port: int = APP_PORT,
        paused: bool = False,
    ) -> None:
        self._state_dir = state_dir
        self._key = key
        self.claim = claim
        self._api = api
        self._now = now
        self._sleep = sleep
        self._version = software_version
        self._lan_addresses = lan_addresses
        self._block: AgentConfig | None = load_agent_config(state_dir)
        self.config = agent_config.effective(self._block, api.base_url)
        self.last_contact = datetime.fromisoformat(claim.last_contact)
        self.metered = False
        self.notices: list[str] = []
        self.needs_upgrade = False
        self.upgrade_message: str | None = None
        self.paused = paused
        """Remote access paused on site: polls say so and Wakes are ignored."""
        self.record = RemoteRecord(claim.base_id, now)
        """Live Remote sessions, and the Wakes, starts and ends the app logs."""
        self.unclaimed = False
        """The Portal answered 401 ``unclaimed``: the loop has ended."""
        self._check_in_due = datetime.min.replace(tzinfo=self.last_contact.tzinfo)
        self._sent: tuple[datetime | None, CheckIn] | None = None
        """The tunnel (by when it opened) and Check-in last sent on it."""
        self.holds = api.protocol_version >= HOLDING_VERSION
        """This agent holds its tunnel open (else it only answers Wakes)."""
        self.tunnel = TunnelClient(
            headers=lambda url, nonce: api.tunnel_headers(key, url, nonce),
            now=now,
            sleep=sleep,
            connect=(
                tunnel_connect
                if tunnel_connect is not None
                else functools.partial(connect_websocket, names=api.names)
            ),
            app_port=app_port,
            ping_interval=lambda: self.config.ping_interval,
            on_control=self._on_control,
        )

    @property
    def sessions(self) -> list[RemoteSession]:
        """Live Remote sessions, from the last poll reply."""
        return self.record.live

    @property
    def poll_interval(self) -> int:
        return self.config.interval(self.metered)

    @property
    def mode(self) -> ConnectionMode:
        if not self.holds:
            return "polling"
        if self.tunnel.state == "open" and self.tunnel.held:
            return "holding"
        return "reconnecting"

    async def run(self) -> None:
        """Hold the tunnel, poll and Check in until cancelled or Un-claimed
        on the Portal (either also closes the tunnel)."""
        try:
            await self._beat()
        finally:
            await self.tunnel.close()

    async def _hold(self) -> None:
        """Open the tunnel to hold it, unless it's open (or opening: a
        tunnel a Wake opened is left to run its course), remote access is
        paused, or the Portal refuses this agent's protocol version; then
        wait until it is open or failed to open."""
        if self.holds and not self.paused and not self.needs_upgrade:
            self.tunnel.hold(self.config.tunnel_url)
            await self.tunnel.opened()

    async def _beat(self) -> None:
        while True:
            await self._hold()
            if self.mode == "holding":
                await self._sleep(await self._check_in_on_tunnel())
                continue
            try:
                if self._now() >= self._check_in_due:
                    await self._check_in_unless_limited()
                backoff = 0.0
                try:
                    await self._poll()
                except RateLimitedError as exc:
                    backoff = max(exc.retry_after or 0, 2 * self.poll_interval)
                    logger.info("The Portal asked for fewer polls: %ss", backoff)
                delay = max(self.poll_interval, backoff)
            except UpgradeRequiredError as exc:
                self._upgrade_required(exc)
                delay = UPGRADE_RETRY_S
            except UnclaimedError as exc:
                logger.warning("Un-claimed on the Portal: %s", exc)
                self.unclaimed = True
                return
            except PortalUnreachableError as exc:
                logger.info("Couldn't reach the Portal, will retry: %s", exc)
                delay = self.poll_interval
            await self._sleep(delay)

    def _check_in_message(self) -> CheckIn:
        return CheckIn(
            software_version=self._version,
            lan_addresses=self._lan_addresses(),
            paused=self.paused,
            mode=self.mode,
        )

    async def _check_in_on_tunnel(self) -> float:
        """Send a Check-in on the held tunnel if this tunnel hasn't had one,
        one is due, or it changed. Returns how long until the next beat."""
        message = self._check_in_message()
        sent = (self.tunnel.since, message)
        if sent != self._sent or self._now() >= self._check_in_due:
            try:
                await self.tunnel.send_control(control_message(message))
            except TunnelClosed:
                return 0  # gone meanwhile: reconnect at once
            self._sent = sent
            self.last_contact = self._now()
            self._checked_in()
        due = (self._check_in_due - self._now()).total_seconds()
        return max(0.0, min(self.poll_interval, due))

    async def _on_control(self, payload: bytes) -> None:
        """A control message from the Portal on the held tunnel: the agent
        config, notices and live Remote sessions, or a session's end. A
        malformed one closes the tunnel, as a bad frame does."""
        try:
            message = read_control(payload)
        except MessageError as exc:
            raise FrameError(str(exc)) from None
        if isinstance(message, TunnelUpdate):
            self._heard(message)
            self.record.heard(message.sessions, [])
            if self.paused:
                self.record.forget_live()
        elif isinstance(message, EndedSession):
            self.record.ended(message)

    async def _poll(self) -> None:
        state = "paused" if self.paused else "connected"
        reply = await self._api.poll(self._key, Poll(state=state))
        self._heard(reply)
        self._on_poll_reply(reply)

    def _on_poll_reply(self, reply: PollReply) -> None:
        """What a poll reply asks beyond the common part: a Wake opens the
        tunnel at the agent's own checked tunnel URL, never anywhere the
        reply names, unless remote access is paused."""
        self.record.heard(reply.sessions, reply.ended)
        if self.paused:
            self.record.forget_live()
        if reply.wake is not None:
            self._on_wake(reply.wake)

    def _on_wake(self, nonce: str) -> None:
        """A Wake from a poll reply; ignored while remote access is paused."""
        if self.paused:
            logger.info("Ignoring a Wake: remote access is paused")
            return
        self.record.wake(nonce)
        self.tunnel.wake(nonce, self.config.tunnel_url)

    async def _check_in_unless_limited(self) -> None:
        try:
            await self._check_in()
        except RateLimitedError as exc:
            wait = exc.retry_after
            if wait is None:
                wait = self.config.check_in_interval / 2
            logger.info("The Portal asked for fewer Check-ins; next in %ss", wait)
            self._check_in_due = self._now() + timedelta(seconds=wait)

    async def set_paused(self, paused: bool) -> None:
        """Pause (or resume) remote access, and tell the Portal at once.

        Pausing closes the tunnel, so live Remote sessions are cut here
        whatever the Portal does; the Portal ends them and revokes their
        tokens when it hears ``paused``. The pause is kept on disk.
        """
        self.paused = paused
        save_paused(self._state_dir, paused)
        logger.info("Remote access %s", "paused" if paused else "resumed")
        if paused:
            self.record.forget_live()
            await self.tunnel.close()
        try:
            await self._poll()
        except UpgradeRequiredError as exc:
            self._upgrade_required(exc)
        except (UnclaimedError, PortalUnreachableError, RateLimitedError) as exc:
            logger.info("Couldn't tell the Portal yet, the next poll will: %s", exc)

    async def _check_in(self) -> None:
        reply = await self._api.check_in(self._key, self._check_in_message())
        self._heard(reply)
        self._checked_in()

    def _checked_in(self) -> None:
        self._check_in_due = self._now() + timedelta(
            seconds=self.config.check_in_interval
        )
        # Persisted here rather than on every poll, to spare the SD card.
        self.claim = dataclasses.replace(
            self.claim, last_contact=self.last_contact.isoformat()
        )
        save_claim_file(self._state_dir, self.claim)

    def _heard(self, reply: PollReply | CheckInReply | TunnelUpdate) -> None:
        self.last_contact = self._now()
        if self.needs_upgrade:
            logger.info("The Portal accepts this agent again")
        self.needs_upgrade, self.upgrade_message = False, None
        self.metered = reply.metered
        self.notices = [n.text for n in reply.notices]
        if self._block is None or reply.config.revision != self._block.revision:
            self._apply(reply.config)

    def _apply(self, block: AgentConfig) -> None:
        logger.info("Applying agent config revision %s", block.revision)
        self._block = block
        self.config = agent_config.effective(block, self._api.base_url)
        save_agent_config(self._state_dir, block)

    def _upgrade_required(self, exc: UpgradeRequiredError) -> None:
        if not self.needs_upgrade:
            logger.warning(
                "The Portal needs protocol version %s or later: %s",
                exc.reply.min_version,
                exc.reply.message,
            )
        self.needs_upgrade = True
        self.upgrade_message = exc.reply.message or UPDATE_TO_KEEP_USING
        self._check_in_due = self._now()  # first thing once accepted again
