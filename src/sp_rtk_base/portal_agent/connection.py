"""A Claimed base's contact with the Portal: polls and Check-ins (ADR 0002).

:class:`Connection` runs one loop for as long as the base is Claimed. Each
beat it sends a Check-in when one is due (first thing, then every
``check_in_interval``), then a poll, then sleeps the poll interval (the
metered one when the Operator marked the base metered). There is no
standing connection: each beat is a few small HTTPS requests.

Each reply goes through :meth:`Connection._heard`: last contact, the
metered flag, notices, and the agent config block (applied only when its
revision changes, then persisted). A poll reply also goes through
:meth:`Connection._on_poll_reply`, which is where a Wake (#88) is handled.

When the agent config names a Doorbell host, the :class:`~.doorbell.Doorbell`
sends keepalives; once a Check-in reply says the Portal hears them (wake
mode Doorbell) the beat skips its poll, and a ring Wakes the base instead
(:meth:`Connection._on_wake`). A change of Doorbell health brings the next
Check-in forward to the next beat.

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
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from sp_rtk_portal_protocol import (
    DOORBELL_OK,
    AgentConfig,
    BaseKey,
    CheckIn,
    CheckInReply,
    LanAddress,
    LiveSession,
    Poll,
    PollReply,
)

from sp_rtk_base.portal_agent import agent_config, doorbell
from sp_rtk_base.portal_agent.portal_api import (
    PortalApi,
    PortalUnreachableError,
    RateLimitedError,
    UnclaimedError,
    UpgradeRequiredError,
)
from sp_rtk_base.portal_agent.protocol import RemoteSession
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
"""After a 426, try again once a day (sp-rtk-base may have been upgraded)."""

UPGRADE_MESSAGE = "Upgrade sp-rtk-base to keep using the Portal."


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
        resolve: doorbell.Resolve,
        lan_addresses: Callable[[], list[LanAddress]],
        tunnel_connect: Connect = connect_websocket,
        app_port: int = APP_PORT,
        open_udp: doorbell.OpenUdp = doorbell.open_udp,
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
        self.sessions: list[RemoteSession] = []
        """Live Remote sessions, from the last poll reply."""
        self.unclaimed = False
        """The Portal answered 401 ``unclaimed``: the loop has ended."""
        self._check_in_due = datetime.min.replace(tzinfo=self.last_contact.tzinfo)
        self.tunnel = TunnelClient(
            headers=lambda url, nonce: api.tunnel_headers(key, url, nonce),
            now=now,
            connect=tunnel_connect,
            app_port=app_port,
        )
        self.bell = doorbell.Doorbell(
            key=key,
            now=now,
            sleep=sleep,
            resolve=resolve,
            open_udp=open_udp,
            on_ring=self._on_wake,
            on_change=self._check_in_soon,
        )

    @property
    def doorbell(self) -> str:
        """Doorbell health (the ``DOORBELL_*`` values)."""
        return self.bell.health

    @property
    def wake_mode(self) -> Literal["polling", "doorbell"]:
        return "doorbell" if self.doorbell == DOORBELL_OK else "polling"

    @property
    def poll_interval(self) -> int:
        return self.config.interval(self.metered)

    async def run(self) -> None:
        """Poll and Check in until cancelled or Un-claimed on the Portal
        (either also closes the tunnel)."""
        try:
            await self._beat()
        finally:
            await self.bell.close()
            await self.tunnel.close()

    async def _beat(self) -> None:
        while True:
            try:
                await self.bell.follow(self.config.doorbell)
                if self._now() >= self._check_in_due:
                    await self._check_in_unless_limited()
                backoff = 0.0
                if self.wake_mode == "polling":
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

    async def _poll(self) -> None:
        state = "paused" if self.paused else "connected"
        reply = await self._api.poll(self._key, Poll(state=state))
        self._heard(reply)
        self._on_poll_reply(reply)

    def _on_poll_reply(self, reply: PollReply) -> None:
        """What a poll reply asks beyond the common part: a Wake opens the
        tunnel at the agent's own checked tunnel URL, never anywhere the
        reply names, unless remote access is paused."""
        self.sessions = [] if self.paused else _sessions(reply.sessions)
        if reply.wake is not None:
            self._on_wake(reply.wake)

    def _on_wake(self, nonce: str) -> None:
        """A Wake, from a poll reply or a Doorbell ring; ignored while remote
        access is paused."""
        if self.paused:
            logger.info("Ignoring a Wake: remote access is paused")
            return
        self.tunnel.wake(nonce, self.config.tunnel_url)

    def _check_in_soon(self) -> None:
        self._check_in_due = self._now()

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
            self.sessions = []
            await self.tunnel.close()
        try:
            await self._poll()
        except UpgradeRequiredError as exc:
            self._upgrade_required(exc)
        except (UnclaimedError, PortalUnreachableError, RateLimitedError) as exc:
            logger.info("Couldn't tell the Portal yet, the next poll will: %s", exc)

    async def _check_in(self) -> None:
        reply = await self._api.check_in(
            self._key,
            CheckIn(
                software_version=self._version,
                doorbell=self.doorbell,
                lan_addresses=self._lan_addresses(),
            ),
        )
        self._heard(reply)
        self._check_in_due = self._now() + timedelta(
            seconds=self.config.check_in_interval
        )
        self.bell.portal_says(reply.doorbell_heard, reply.ring_key)
        # Persisted here rather than on every poll, to spare the SD card.
        self.claim = dataclasses.replace(
            self.claim, last_contact=self.last_contact.isoformat()
        )
        save_claim_file(self._state_dir, self.claim)

    def _heard(self, reply: PollReply | CheckInReply) -> None:
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
        self.upgrade_message = exc.reply.message or UPGRADE_MESSAGE
        self._check_in_due = self._now()  # first thing once accepted again


def _sessions(reported: list[LiveSession]) -> list[RemoteSession]:
    """The live Remote sessions a poll reply lists, skipping any malformed."""
    sessions: list[RemoteSession] = []
    for s in reported:
        try:
            sessions.append(RemoteSession.model_validate(s.to_json()))
        except ValidationError:
            logger.warning("Ignoring a malformed Remote session from the Portal")
    return sessions
