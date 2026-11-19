"""A Claimed base's contact with the Portal: its held tunnel, polls and
Check-ins (ADRs 0002, 0006).

:class:`Connection` runs for as long as the base is Claimed. Its
:attr:`~Connection.mode` says how the Portal reaches the base:

- **holding**: the tunnel is open and held (protocol version 2);
- **reconnecting**: a version 2 agent whose tunnel isn't open (yet, or any
  more);
- **polling**: reached only through Wakes: a version 1 agent, or a version
  2 agent that fell back to polling.

It runs one loop. Each beat a version 2 agent first makes sure its tunnel
is held (so it opens at once on Claim and on start), except when refused as
too old, and waits for an open to succeed or fail. Holding is its own
task: after a drop or a failed open it tries again after a random, growing
delay (:class:`~.reconnect.Reconnect`, :attr:`Connection.reconnect`), not
on the beat.

While it holds its tunnel, the tunnel carries everything (ticket #205):
no polls or Check-ins go over HTTPS. A Check-in goes on the tunnel when
it opens, every ``check_in_interval`` and whenever its contents change
(noticed within a beat); the Portal sends the agent config, metered flag,
notices and live Remote sessions on connect and on every change, and each
Remote session's end (:meth:`Connection._on_control`).

**Fallback** (ticket #209). A network that won't let a WebSocket through
(a proxy refusing upgrades) mustn't leave the base unreachable. After
``fallback_failures`` (3) tunnel opens failed in a row (not counting a
Portal's 503, nor any while HTTPS failed too:
:attr:`~.reconnect.Reconnect.blocked`), the agent sends a
Check-in over HTTPS saying ``polling``: if it gets through, the agent stops
its reconnect backoff and polls like a version 1 agent, Wakes included, and
tries its tunnel again every ``fallback_retry_interval`` (15 min), holding it
from the first try that opens. If HTTPS fails too, nothing says the network
rather than the Portal is at fault: it keeps backing off on the tunnel.

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

Check-ins carry the pause too, as polls do. A paused agent keeps its
tunnel (and so its online state): it only refuses the streams in it.

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
from sp_rtk_base.portal_agent.reconnect import Reconnect
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
        """Remote access paused on site: polls say so, Wakes are ignored and
        the tunnel refuses every stream."""
        self.record = RemoteRecord(claim.base_id, now)
        """Live Remote sessions, and the Wakes, starts and ends the app logs."""
        self.unclaimed = False
        """The Portal answered 401 ``unclaimed``: the loop has ended."""
        self._check_in_due = datetime.min.replace(tzinfo=self.last_contact.tzinfo)
        self._sent: tuple[datetime | None, CheckIn] | None = None
        """The tunnel (by when it opened) and Check-in last sent on it."""
        self.holds = api.protocol_version >= HOLDING_VERSION
        """This agent holds its tunnel open (else it only answers Wakes)."""
        self.reconnect = Reconnect(cap=lambda: self.config.reconnect_backoff_cap)
        """The backoff between tries to open the held tunnel again after a
        drop; its ``failures`` counts the tries that failed in a row."""
        self._fell_back = False
        """The tunnel wouldn't open while HTTPS got through: polling instead
        (until a try opens it, see :meth:`_falls_back`)."""
        self._retry_at = self._now()
        """When a fallen-back agent next tries its tunnel."""
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
            reconnect=self.reconnect,
            refusing=lambda: self.paused,
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
        if self.holds and self.tunnel.state == "open" and self.tunnel.held:
            return "holding"
        if not self.holds or self._falls_back():
            return "polling"
        return "reconnecting"

    def _falls_back(self) -> bool:
        """Fallen back to polling, and no tunnel opened since (one that
        does resets :attr:`reconnect`'s failures, and holding takes over at
        once)."""
        return self._fell_back and self.reconnect.failures > 0

    async def run(self) -> None:
        """Hold the tunnel, poll and Check in until cancelled or Un-claimed
        on the Portal (either also closes the tunnel)."""
        try:
            await self._beat()
        finally:
            await self.tunnel.close()

    def _may_hold(self) -> bool:
        """Hold the tunnel, unless the Portal refuses this agent's protocol
        version."""
        return self.holds and not self.needs_upgrade

    def _keeps_holding(self) -> bool:
        """Try the tunnel again after a drop or failed open (with backoff),
        unless it may not be held now or the agent fell back to polling."""
        return self._may_hold() and not self._falls_back()

    async def _hold(self) -> None:
        """Hold the tunnel open, unless it's held already (open, or waiting
        to reconnect), a tunnel a Wake opened is still running its course,
        or it may not be held now; then wait until an open in progress
        succeeds or fails. A paused agent holds it too, refusing its
        streams. Fallen back to polling, it only tries once its retry is
        due."""
        if not self._may_hold():
            return
        if self._falls_back():
            if self.tunnel.state != "closed" or self._now() < self._retry_at:
                return
            self._retry_at = self._now() + timedelta(
                seconds=self.config.fallback_retry_interval
            )
            logger.info("Trying the tunnel again")
        self.tunnel.hold(lambda: self.config.tunnel_url, keep=self._keeps_holding)
        await self.tunnel.opened()
        if self._fell_back and self.reconnect.failures == 0:
            logger.info("The tunnel opened again: holding it, no more polling")
            self._fell_back = False

    async def _fall_back_if_due(self) -> None:
        """After ``fallback_failures`` failed tunnel opens in a row, fall
        back to polling if an ordinary signed HTTPS request (a Check-in
        saying ``polling``) gets through. If it doesn't, the error goes on
        (and the tunnel keeps backing off)."""
        if (
            self._fell_back
            or not self._may_hold()
            or self.reconnect.blocked < self.config.fallback_failures
        ):
            return
        self._fell_back = True
        try:
            await self._check_in_unless_limited()
        except BaseException:
            self._fell_back = False
            raise
        logger.warning(
            "The tunnel failed to open %s times in a row but HTTPS gets "
            "through: polling instead, trying the tunnel every %s s",
            self.reconnect.blocked,
            self.config.fallback_retry_interval,
        )
        await self.tunnel.close()  # ends the backoff, so Wakes are answered
        self._retry_at = self._now() + timedelta(
            seconds=self.config.fallback_retry_interval
        )

    async def _beat(self) -> None:
        while True:
            await self._hold()
            if self.mode == "holding":
                await self._sleep(await self._check_in_on_tunnel())
                continue
            try:
                await self._fall_back_if_due()
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
                self.reconnect.unreachable()
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

        Pausing ends every open stream and refuses new ones, so live Remote
        sessions are cut here whatever the Portal does; the tunnel stays
        open. The Portal ends the sessions and revokes their tokens when it
        hears ``paused``: from a Check-in on the held tunnel, else a poll.
        The pause is kept on disk.
        """
        self.paused = paused
        save_paused(self._state_dir, paused)
        logger.info("Remote access %s", "paused" if paused else "resumed")
        if paused:
            self.record.forget_live()
            await self.tunnel.end_streams()
        if self.mode == "holding":
            await self._check_in_on_tunnel()  # it says paused (or not)
            return
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
