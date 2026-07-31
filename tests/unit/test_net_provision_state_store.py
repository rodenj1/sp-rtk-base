"""Tests for the durable clock state store (issue #9).

The whole point of this module is surviving a restart of the
provisioning service, so the load/save round trip is exercised through
*separate* :class:`ProvisioningStateStore` instances pointed at the
same path — mirroring what actually happens when the process exits
and systemd starts a fresh one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from sp_rtk_base.services.net_provision.state_store import (
    ProvisioningClockState,
    ProvisioningStateStore,
)


class TestFreshState:
    """No file on disk means no history — same as a first-ever boot."""

    def test_missing_file_returns_default_state(self, tmp_path: Path) -> None:
        store = ProvisioningStateStore(tmp_path / "missing" / "state.json")
        state = store.load()
        assert state.last_uplink_at is None
        assert state.ap_started_at is None

    def test_missing_file_does_not_raise(self, tmp_path: Path) -> None:
        """Unlike the config loader, this is not the fail-loudly boundary."""
        store = ProvisioningStateStore(tmp_path / "does-not-exist.json")
        store.load()  # must not raise


class TestCorruptState:
    """A garbled file must not crash the supervisor's startup."""

    def test_invalid_json_returns_default_state(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("{not json")
        store = ProvisioningStateStore(path)
        state = store.load()
        assert state.last_uplink_at is None
        assert state.ap_started_at is None

    def test_wrong_shape_returns_default_state(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"last_uplink_at": "not-a-number"}))
        store = ProvisioningStateStore(path)
        state = store.load()
        assert state.last_uplink_at is None

    def test_corrupt_file_logs_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "state.json"
        path.write_text("{not json")
        store = ProvisioningStateStore(path)
        with caplog.at_level(logging.WARNING):
            store.load()
        assert any("provisioning state" in record.message for record in caplog.records)


class TestRoundTrip:
    """Save then load returns the same values."""

    def test_round_trip_preserves_both_timestamps(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        store = ProvisioningStateStore(path)
        store.save(ProvisioningClockState(last_uplink_at=111.0, ap_started_at=222.0))

        loaded = store.load()
        assert loaded.last_uplink_at == 111.0
        assert loaded.ap_started_at == 222.0

    def test_round_trip_preserves_none_values(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        store = ProvisioningStateStore(path)
        store.save(ProvisioningClockState())

        loaded = store.load()
        assert loaded.last_uplink_at is None
        assert loaded.ap_started_at is None

    def test_save_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "state.json"
        store = ProvisioningStateStore(path)
        store.save(ProvisioningClockState(last_uplink_at=1.0))
        assert path.exists()

    def test_save_leaves_no_temp_file_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        store = ProvisioningStateStore(path)
        store.save(ProvisioningClockState(last_uplink_at=1.0))
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []


class TestSurvivesRestart:
    """The scenario the ticket calls out explicitly: process restart.

    A fresh :class:`ProvisioningStateStore` object (as a restarted
    process would construct) must recover exactly what the previous
    process instance persisted, with no in-memory state carried over.
    """

    def test_a_new_store_instance_recovers_the_prior_processs_clocks(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "state.json"

        first_process_store = ProvisioningStateStore(path)
        first_process_store.save(
            ProvisioningClockState(last_uplink_at=1_000.0, ap_started_at=1_500.0)
        )
        del first_process_store  # simulate the process exiting

        second_process_store = ProvisioningStateStore(path)
        recovered = second_process_store.load()

        assert recovered.last_uplink_at == 1_000.0
        assert recovered.ap_started_at == 1_500.0
