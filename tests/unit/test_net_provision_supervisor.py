"""Tests for the supervisor loop (issue #9): decide()/adapter glue plus
the durable-clock bookkeeping that makes seconds_disconnected and
seconds_in_ap survive a restart.

Fakes stand in for the nmcli adapter (matching the pattern in
``test_net_provision_nmcli_adapter``) so nothing here touches a real
subprocess, clock, or filesystem beyond the state store's tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sp_rtk_base.models.net_provision_models import (
    Connectivity,
    NetProvisionConfig,
    NetworkState,
    ProvisionAction,
)
from sp_rtk_base.services.net_provision.state_store import (
    ProvisioningClockState,
    ProvisioningStateStore,
)
from sp_rtk_base.services.net_provision.supervisor import run_forever, tick

_PASSWORD = "sticker-secret"

_CONNECTED_STATE = NetworkState(
    uplink_connectivity=Connectivity.FULL,
    seconds_since_boot=999.0,
    seconds_disconnected=0.0,
    ap_active=False,
)
_AP_ACTIVE_STATE = NetworkState(
    uplink_connectivity=Connectivity.NONE,
    seconds_since_boot=999.0,
    seconds_disconnected=999.0,
    ap_active=True,
    seconds_in_ap=0.0,
)
_DISCONNECTED_CLIENT_STATE = NetworkState(
    uplink_connectivity=Connectivity.NONE,
    seconds_since_boot=999.0,
    seconds_disconnected=999.0,
    ap_active=False,
)


class FakeAdapter:
    """Records read_state() inputs, returns a canned state, records execute()."""

    def __init__(self, state_to_return: NetworkState) -> None:
        self.state_to_return = state_to_return
        self.read_state_calls: list[dict[str, float]] = []
        self.executed_actions: list[ProvisionAction] = []
        self.raise_on_execute: Exception | None = None

    def read_state(
        self,
        *,
        seconds_since_boot: float,
        seconds_disconnected: float,
        seconds_in_ap: float,
    ) -> NetworkState:
        self.read_state_calls.append(
            {
                "seconds_since_boot": seconds_since_boot,
                "seconds_disconnected": seconds_disconnected,
                "seconds_in_ap": seconds_in_ap,
            }
        )
        return self.state_to_return

    def execute(self, action: ProvisionAction) -> None:
        self.executed_actions.append(action)
        if self.raise_on_execute is not None:
            raise self.raise_on_execute


class FakeStopEvent:
    """A threading.Event stand-in that stops the loop after N waits."""

    def __init__(self, stop_after: int) -> None:
        self._stop_after = stop_after
        self._waits = 0
        self.wait_calls: list[float] = []

    def is_set(self) -> bool:
        return self._waits >= self._stop_after

    def wait(self, timeout: float) -> bool:
        self.wait_calls.append(timeout)
        self._waits += 1
        return self.is_set()


@pytest.fixture
def config() -> NetProvisionConfig:
    return NetProvisionConfig(ap_password=_PASSWORD)


@pytest.fixture
def state_store(tmp_path: Path) -> ProvisioningStateStore:
    return ProvisioningStateStore(tmp_path / "state.json")


class TestElapsedTimeComputation:
    """tick() must derive seconds_disconnected/seconds_in_ap from disk."""

    def test_never_connected_uses_uptime_as_disconnected_duration(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        adapter = FakeAdapter(_DISCONNECTED_CLIENT_STATE)
        tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 42.0,
        )
        call = adapter.read_state_calls[0]
        assert call["seconds_since_boot"] == 42.0
        assert call["seconds_disconnected"] == 42.0

    def test_prior_uplink_timestamp_yields_elapsed_disconnect_time(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        state_store.save(ProvisioningClockState(last_uplink_at=950.0))
        adapter = FakeAdapter(_DISCONNECTED_CLIENT_STATE)
        tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )
        assert adapter.read_state_calls[0]["seconds_disconnected"] == 50.0

    def test_ap_never_started_yields_zero_seconds_in_ap(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        adapter = FakeAdapter(_AP_ACTIVE_STATE)
        tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )
        assert adapter.read_state_calls[0]["seconds_in_ap"] == 0.0

    def test_prior_ap_start_timestamp_yields_elapsed_ap_session_time(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        state_store.save(ProvisioningClockState(ap_started_at=900.0))
        adapter = FakeAdapter(_AP_ACTIVE_STATE)
        tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )
        assert adapter.read_state_calls[0]["seconds_in_ap"] == 100.0


class TestClockPersistence:
    """The core issue-#9 behavior: clocks must survive a restart."""

    def test_uplink_resets_the_disconnect_clock(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        state_store.save(ProvisioningClockState(last_uplink_at=1.0))
        adapter = FakeAdapter(_CONNECTED_STATE)
        tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )
        assert state_store.load().last_uplink_at == 1_000.0

    def test_disconnected_leaves_the_last_uplink_timestamp_untouched(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        """A restart mid-outage must not restart the fallback window."""
        state_store.save(ProvisioningClockState(last_uplink_at=700.0))
        adapter = FakeAdapter(_DISCONNECTED_CLIENT_STATE)
        tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )
        assert state_store.load().last_uplink_at == 700.0

    def test_ap_becoming_active_records_a_start_time(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        adapter = FakeAdapter(_AP_ACTIVE_STATE)
        tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )
        assert state_store.load().ap_started_at == 1_000.0

    def test_ap_staying_active_does_not_reset_its_start_time(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        """The exact bug the ticket calls out: a restart resetting

        seconds_in_ap means a service that restarts more often than
        rescan_interval_seconds never rescans, and the AP never comes
        back down.
        """
        state_store.save(ProvisioningClockState(ap_started_at=100.0))
        adapter = FakeAdapter(_AP_ACTIVE_STATE)
        tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 999_999.0,  # a "restart" long after AP started
            uptime_fn=lambda: 5.0,  # uptime resets — this is a fresh process
        )
        assert state_store.load().ap_started_at == 100.0

    def test_ap_stopping_clears_its_start_time(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        state_store.save(ProvisioningClockState(ap_started_at=100.0))
        adapter = FakeAdapter(_DISCONNECTED_CLIENT_STATE)  # ap_active=False
        tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )
        assert state_store.load().ap_started_at is None

    def test_a_fresh_store_instance_after_tick_sees_the_persisted_clocks(
        self, config: NetProvisionConfig, tmp_path: Path
    ) -> None:
        """Simulates a process restart between two ticks."""
        path = tmp_path / "state.json"
        first_process_store = ProvisioningStateStore(path)
        tick(
            adapter=FakeAdapter(_AP_ACTIVE_STATE),
            config=config,
            state_store=first_process_store,
            now_fn=lambda: 100.0,
            uptime_fn=lambda: 5.0,
        )
        del first_process_store

        second_process_store = ProvisioningStateStore(path)
        adapter = FakeAdapter(_AP_ACTIVE_STATE)
        tick(
            adapter=adapter,
            config=config,
            state_store=second_process_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 5.0,  # process restarted -> uptime reset
        )
        # seconds_in_ap must reflect the full 900s across the "restart",
        # not 0 / uptime-since-restart.
        assert adapter.read_state_calls[0]["seconds_in_ap"] == 900.0


class TestDecideAndExecute:
    """tick() must call the real decide() and execute whatever it returns."""

    def test_connected_with_no_ap_is_idle(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        adapter = FakeAdapter(_CONNECTED_STATE)
        action = tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )
        assert action is ProvisionAction.IDLE
        assert adapter.executed_actions == [ProvisionAction.IDLE]

    def test_ap_active_with_uplink_stops_the_ap(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        state = NetworkState(
            uplink_connectivity=Connectivity.FULL,
            seconds_since_boot=999.0,
            seconds_disconnected=0.0,
            ap_active=True,
            seconds_in_ap=10.0,
        )
        adapter = FakeAdapter(state)
        action = tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )
        assert action is ProvisionAction.STOP_AP_AND_CONNECT
        assert adapter.executed_actions == [ProvisionAction.STOP_AP_AND_CONNECT]

    def test_boot_wait_not_elapsed_is_idle_even_without_uplink(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        # decide() reads seconds_since_boot off the NetworkState the
        # adapter returns, not tick()'s uptime_fn input directly — this
        # canned state is what actually drives decide()'s branch here.
        just_booted = NetworkState(
            uplink_connectivity=Connectivity.NONE,
            seconds_since_boot=1.0,
            seconds_disconnected=1.0,
            ap_active=False,
        )
        adapter = FakeAdapter(just_booted)
        action = tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 1.0,
        )
        assert action is ProvisionAction.IDLE


class TestOnApActiveCallback:
    """tick()/run_forever() report the resulting AP state so a caller

    (the WiFi-picker portal's start()/stop(), issue #10) can follow it
    without polling nmcli itself.
    """

    def test_defaults_to_none_and_does_not_raise(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        tick(
            adapter=FakeAdapter(_CONNECTED_STATE),
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )

    def test_start_ap_reports_true_even_though_the_pre_execute_state_was_false(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        """The state read at the top of the tick reflects nmcli *before*
        execute() ran — decide() saw ap_active=False and chose START_AP.
        The callback must reflect what's true *after* execute(), not the
        stale pre-execute reading, or the portal would only start a full
        poll interval late."""
        just_booted_no_ap = NetworkState(
            uplink_connectivity=Connectivity.NONE,
            seconds_since_boot=9_999.0,
            seconds_disconnected=9_999.0,
            ap_active=False,
        )
        adapter = FakeAdapter(just_booted_no_ap)
        calls: list[bool] = []
        action = tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 9_999.0,
            on_ap_active=calls.append,
        )
        assert action is ProvisionAction.START_AP
        assert calls == [True]

    def test_stop_ap_and_connect_reports_false(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        state = NetworkState(
            uplink_connectivity=Connectivity.FULL,
            seconds_since_boot=999.0,
            seconds_disconnected=0.0,
            ap_active=True,
            seconds_in_ap=10.0,
        )
        adapter = FakeAdapter(state)
        calls: list[bool] = []
        tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
            on_ap_active=calls.append,
        )
        assert calls == [False]

    def test_idle_reports_the_unchanged_current_ap_state(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        adapter = FakeAdapter(_CONNECTED_STATE)  # ap_active=False, decide()->IDLE
        calls: list[bool] = []
        tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
            on_ap_active=calls.append,
        )
        assert calls == [False]

    def test_rescan_reports_true_since_the_ap_resumes(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        state = NetworkState(
            uplink_connectivity=Connectivity.NONE,
            seconds_since_boot=999.0,
            seconds_disconnected=999.0,
            ap_active=True,
            seconds_in_ap=999.0,  # past rescan_interval_seconds
        )
        adapter = FakeAdapter(state)
        calls: list[bool] = []
        action = tick(
            adapter=adapter,
            config=config,
            state_store=state_store,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
            on_ap_active=calls.append,
        )
        assert action is ProvisionAction.RESCAN
        assert calls == [True]

    def test_run_forever_invokes_the_callback_once_per_tick(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        adapter = FakeAdapter(_CONNECTED_STATE)
        stop_event = FakeStopEvent(stop_after=3)
        calls: list[bool] = []
        run_forever(
            adapter=adapter,
            config=config,
            state_store=state_store,
            stop_event=stop_event,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
            on_ap_active=calls.append,
        )
        assert calls == [False, False, False]


class TestRunForever:
    """The loop wrapper: tick on an interval until told to stop."""

    def test_ticks_repeatedly_until_the_stop_event_fires(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        adapter = FakeAdapter(_CONNECTED_STATE)
        stop_event = FakeStopEvent(stop_after=3)
        run_forever(
            adapter=adapter,
            config=config,
            state_store=state_store,
            stop_event=stop_event,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )
        assert len(adapter.executed_actions) == 3

    def test_waits_for_the_configured_poll_interval_between_ticks(
        self, state_store: ProvisioningStateStore
    ) -> None:
        config = NetProvisionConfig(ap_password=_PASSWORD, poll_interval_seconds=7.5)
        adapter = FakeAdapter(_CONNECTED_STATE)
        stop_event = FakeStopEvent(stop_after=2)
        run_forever(
            adapter=adapter,
            config=config,
            state_store=state_store,
            stop_event=stop_event,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )
        assert stop_event.wait_calls == [7.5, 7.5]

    def test_a_failing_tick_does_not_stop_the_loop(
        self, config: NetProvisionConfig, state_store: ProvisioningStateStore
    ) -> None:
        """The durable clocks already make a full restart safe, so a

        single bad tick (e.g. a failed nmcli call) should not escalate
        into stopping the whole supervisor.
        """
        adapter = FakeAdapter(_CONNECTED_STATE)
        adapter.raise_on_execute = RuntimeError("nmcli boom")
        stop_event = FakeStopEvent(stop_after=3)

        run_forever(
            adapter=adapter,
            config=config,
            state_store=state_store,
            stop_event=stop_event,
            now_fn=lambda: 1_000.0,
            uptime_fn=lambda: 500.0,
        )

        assert len(adapter.executed_actions) == 3
