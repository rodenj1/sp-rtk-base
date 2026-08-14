"""The provisioning supervisor loop (issue #9).

Ties the pieces from issues #7/#8 together on a timer: read state via
the nmcli adapter, call the pure :func:`decide`, execute the action it
returns. The one thing this module owns beyond that glue is the
durable-clock bookkeeping described in issue #9 — deriving
``seconds_disconnected``/``seconds_in_ap`` from a persisted store
rather than an in-process counter, so a service restart (crash,
``Restart=on-failure``, package upgrade) doesn't restart the fallback
window or the AP's rescan clock. See
:mod:`sp_rtk_base.services.net_provision.state_store` for why that
matters.

Issue #25 adds a third durable record on top of those two clocks: a
consecutive-connect-failure count keyed to whichever WiFi profile
``NetworkState.saved_wifi_name`` currently names. A failed
``STOP_AP_AND_CONNECT`` is a *handled* outcome here, not a bug — it's
caught, recorded, and the tick completes normally, unlike an unexpected
adapter exception, which still aborts the tick and is left to
:func:`run_forever`'s catch-all.

Issue #33 adds a fourth: a consecutive-uplink-ticks count, so decide()
only tears down an active AP session once ``has_uplink`` has read True
for ``config.uplink_confirm_ticks`` polls in a row, rather than acting
on one possibly-noisy reading.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

from sp_rtk_base.models.net_provision_models import (
    NetProvisionConfig,
    NetworkState,
    ProvisionAction,
)
from sp_rtk_base.services.net_provision.decision import decide
from sp_rtk_base.services.net_provision.nmcli_adapter import WifiConnectError
from sp_rtk_base.services.net_provision.state_store import (
    ProvisioningClockState,
    ProvisioningStateStore,
)

logger = logging.getLogger(__name__)

_UPTIME_PATH = "/proc/uptime"


class ProvisioningAdapter(Protocol):
    """The slice of :class:`NmcliAdapter` the supervisor depends on."""

    def read_state(
        self,
        *,
        seconds_since_boot: float,
        seconds_disconnected: float,
        seconds_in_ap: float,
    ) -> NetworkState: ...

    def execute(self, action: ProvisionAction) -> None: ...


def read_system_uptime_seconds() -> float:
    """Seconds since the machine booted, from ``/proc/uptime``.

    The boot-wait grace period is about the machine booting, not the
    service starting — a supervisor restart mid-boot-wait must not get
    a fresh grace period.
    """
    with open(_UPTIME_PATH) as f:
        return float(f.readline().split()[0])


def _resulting_ap_active(state: NetworkState, action: ProvisionAction) -> bool:
    """Whether the AP is up *after* ``execute(action)`` has run.

    ``state.ap_active`` is what nmcli reported *before* this tick's
    execute() call — decide() saw it and chose ``action`` from it, but a
    caller reacting to the AP's live status (the WiFi-picker portal,
    issue #10) needs to know what's true now, not what was true a
    moment ago. START_AP/STOP_AP_AND_CONNECT change it; IDLE leaves it
    as-is; RESCAN's down/up cycle restores it to active by the time
    execute() returns.
    """
    if action is ProvisionAction.START_AP:
        return True
    if action is ProvisionAction.STOP_AP_AND_CONNECT:
        return False
    if action is ProvisionAction.RESCAN:
        return True
    return state.ap_active


def tick(
    *,
    adapter: ProvisioningAdapter,
    config: NetProvisionConfig,
    state_store: ProvisioningStateStore,
    now_fn: Callable[[], float] = time.time,
    uptime_fn: Callable[[], float] = read_system_uptime_seconds,
    on_ap_active: Callable[[bool], None] | None = None,
) -> ProvisionAction:
    """Run one supervisor iteration and return the action taken.

    Reads the durably-persisted clocks left by the *previous* tick (or
    a previous process, if this one just restarted), uses them to
    compute this tick's elapsed-time inputs, asks the adapter for
    current state, decides, executes, and persists updated clocks for
    the *next* tick to consume. This ordering — always building
    elapsed time from what was last persisted, never from an
    in-process counter — is what makes the clocks restart-proof.

    The clocks persisted here reflect the state read *before*
    ``adapter.execute()`` ran, not the state that results from it — so
    e.g. the tick that issues ``START_AP`` doesn't set
    ``ap_started_at`` until the *following* tick observes the AP
    already active. That's a bounded, one-``poll_interval_seconds``
    undercount of ``seconds_in_ap``/uplink-just-seen timing, not a
    reset: negligible against the much larger rescan/fallback windows
    it's compared to, and avoids a second nmcli read per tick just to
    re-check what ``execute()`` already did.

    A failed ``STOP_AP_AND_CONNECT`` (issue #25) is caught here rather
    than left to propagate: it's a handled outcome — record it and
    finish the tick normally — not the "something unexpected broke"
    case :func:`run_forever`'s catch-all exists for. Letting it escape
    would skip the very clock persistence needed to remember the
    failure happened.

    Args:
        adapter: Reads nmcli state and executes the chosen action.
        config: Threshold knobs, including ``poll_interval_seconds``.
        state_store: Durable clock storage.
        now_fn: Wall-clock source; overridable for tests.
        uptime_fn: System-uptime source; overridable for tests.
        on_ap_active: Called once per tick with whether the AP is up
            *after* ``execute()`` ran (see :func:`_resulting_ap_active`).
            The WiFi-picker portal (issue #10) uses this to start/stop
            itself; its own start()/stop() are idempotent, so calling it
            every tick regardless of change is fine.

    Returns:
        The action :func:`decide` chose (already executed).
    """
    clocks = state_store.load()
    now = now_fn()
    seconds_since_boot = uptime_fn()

    seconds_disconnected = (
        max(0.0, now - clocks.last_uplink_at)
        if clocks.last_uplink_at is not None
        else seconds_since_boot
    )
    seconds_in_ap = (
        max(0.0, now - clocks.ap_started_at)
        if clocks.ap_started_at is not None
        else 0.0
    )

    state = adapter.read_state(
        seconds_since_boot=seconds_since_boot,
        seconds_disconnected=seconds_disconnected,
        seconds_in_ap=seconds_in_ap,
    )

    # A failure count only means something against the profile it was
    # recorded for — if the saved network has changed since (a new
    # portal submission, issue #10), the old count no longer applies.
    # ``state.saved_wifi_name`` reuses the nmcli lookup read_state()
    # already made rather than asking the adapter a second time.
    connect_ssid = state.saved_wifi_name
    failures_apply = (
        connect_ssid is not None and clocks.failed_connect_ssid == connect_ssid
    )
    consecutive_connect_failures = (
        clocks.consecutive_connect_failures if failures_apply else 0
    )
    last_connect_failure_at = clocks.last_connect_failure_at if failures_apply else None
    seconds_since_last_connect_failure = (
        max(0.0, now - last_connect_failure_at)
        if last_connect_failure_at is not None
        else 0.0
    )
    # Issue #33: a single noisy/stale has_uplink=True reading must not
    # be enough for decide() to tear down the AP — count how many polls
    # in a row have seen it, reset the instant a poll doesn't.
    consecutive_uplink_ticks = (
        clocks.consecutive_uplink_ticks + 1 if state.has_uplink else 0
    )
    state = state.model_copy(
        update={
            "consecutive_connect_failures": consecutive_connect_failures,
            "seconds_since_last_connect_failure": seconds_since_last_connect_failure,
            "consecutive_uplink_ticks": consecutive_uplink_ticks,
        }
    )
    action = decide(state, config)

    new_failed_ssid = connect_ssid if consecutive_connect_failures else None
    new_consecutive_failures = consecutive_connect_failures
    new_last_failure_at = last_connect_failure_at
    if action is ProvisionAction.STOP_AP_AND_CONNECT:
        try:
            adapter.execute(action)
        except WifiConnectError:
            logger.warning(
                "Provisioning: connect to saved WiFi %r failed "
                "(consecutive failure #%d)",
                connect_ssid,
                consecutive_connect_failures + 1,
            )
            new_failed_ssid = connect_ssid
            new_consecutive_failures = consecutive_connect_failures + 1
            new_last_failure_at = now
        else:
            new_failed_ssid = None
            new_consecutive_failures = 0
            new_last_failure_at = None
    else:
        adapter.execute(action)

    if on_ap_active is not None:
        on_ap_active(_resulting_ap_active(state, action))

    new_clocks = ProvisioningClockState(
        last_uplink_at=now if state.has_uplink else clocks.last_uplink_at,
        ap_started_at=(
            (clocks.ap_started_at if clocks.ap_started_at is not None else now)
            if state.ap_active
            else None
        ),
        failed_connect_ssid=new_failed_ssid,
        consecutive_connect_failures=new_consecutive_failures,
        last_connect_failure_at=new_last_failure_at,
        consecutive_uplink_ticks=consecutive_uplink_ticks,
    )
    if new_clocks != clocks:
        state_store.save(new_clocks)

    return action


def run_forever(
    *,
    adapter: ProvisioningAdapter,
    config: NetProvisionConfig,
    state_store: ProvisioningStateStore,
    stop_event: threading.Event,
    now_fn: Callable[[], float] = time.time,
    uptime_fn: Callable[[], float] = read_system_uptime_seconds,
    on_ap_active: Callable[[bool], None] | None = None,
) -> None:
    """Tick on ``config.poll_interval_seconds`` until ``stop_event`` fires.

    A failing tick (e.g. an nmcli command failing) is logged and does
    not stop the loop — the durable clocks already make a full process
    restart safe, so there is no need to escalate a single bad tick
    into one. ``stop_event.wait`` doubles as the interruptible sleep so
    a signal handler can end the loop promptly instead of waiting out
    a full poll interval.

    Args:
        on_ap_active: See :func:`tick` — passed straight through, and
            invoked once per tick.
    """
    logger.info(
        "Provisioning supervisor starting (poll interval %.1fs)",
        config.poll_interval_seconds,
    )
    while not stop_event.is_set():
        try:
            action = tick(
                adapter=adapter,
                config=config,
                state_store=state_store,
                now_fn=now_fn,
                uptime_fn=uptime_fn,
                on_ap_active=on_ap_active,
            )
            logger.info("Provisioning tick: action=%s", action.value)
        except Exception:
            logger.exception("Provisioning tick failed; will retry next interval")
        stop_event.wait(config.poll_interval_seconds)
    logger.info("Provisioning supervisor stopped")
