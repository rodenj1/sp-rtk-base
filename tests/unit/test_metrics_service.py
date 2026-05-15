"""Unit tests for MetricsService."""

from __future__ import annotations

from prometheus_client import CollectorRegistry

from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base_relay.core.status import (
    DestinationStatus,
    InputStatus,
    RelayStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_input_status(
    connected: bool = True,
    bytes_received: int = 1000,
    seconds_since_last_data: float = 0.5,
) -> InputStatus:
    return InputStatus(
        connected=connected,
        source_type="TCP",
        bytes_received=bytes_received,
        messages_received=50,
        seconds_since_last_data=seconds_since_last_data,
        reconnect_attempts=1,
        reconnect_successes=1,
        connected_since=1000.0,
    )


def _make_destination_status(
    name: str = "test-dest",
    connected: bool = True,
    bytes_sent: int = 500,
    messages_sent: int = 25,
    messages_dropped: int = 0,
    errors: int = 0,
    queue_depth: int = 3,
) -> DestinationStatus:
    return DestinationStatus(
        name=name,
        destination_type="tcp_server",
        enabled=True,
        running=True,
        connected=connected,
        filter_mode="pass_all",
        bytes_sent=bytes_sent,
        messages_sent=messages_sent,
        messages_dropped=messages_dropped,
        messages_filtered=0,
        errors=errors,
        last_error=None,
        queue_depth=queue_depth,
        connected_since=1000.0,
        uptime_seconds=60.0,
        connection_attempts=1,
        successful_connections=1,
    )


def _make_relay_status(
    running: bool = True,
    uptime: float = 120.0,
    input_connected: bool = True,
    destinations: list[DestinationStatus] | None = None,
) -> RelayStatus:
    if destinations is None:
        destinations = [_make_destination_status()]
    return RelayStatus(
        running=running,
        uptime_seconds=uptime,
        input=_make_input_status(connected=input_connected),
        destinations=destinations,
        active_destination_count=sum(1 for d in destinations if d.connected),
        total_destination_count=len(destinations),
        bytes_received=1000,
        chunks_distributed=50,
        frames_parsed=10,
        no_data_warnings=0,
    )


def _get_gauge_value(registry: CollectorRegistry, name: str) -> float:
    """Extract a gauge value from the registry by metric name."""
    for metric in registry.collect():
        if metric.name == name:
            for sample in metric.samples:
                return float(sample.value)
    raise ValueError(f"Metric '{name}' not found in registry")


def _get_labelled_gauge(
    registry: CollectorRegistry,
    name: str,
    labels: dict[str, str],
) -> float:
    """Extract a labelled gauge value from the registry."""
    for metric in registry.collect():
        if metric.name == name:
            for sample in metric.samples:
                if all(sample.labels.get(k) == v for k, v in labels.items()):
                    return float(sample.value)
    raise ValueError(f"Metric '{name}' with labels {labels} not found")


# ---------------------------------------------------------------------------
# Tests: Construction
# ---------------------------------------------------------------------------


class TestMetricsServiceConstruction:
    """Tests for MetricsService initialization."""

    def test_creates_with_default_registry(self) -> None:
        svc = MetricsService()
        assert svc.registry is not None

    def test_creates_with_custom_registry(self) -> None:
        reg = CollectorRegistry()
        svc = MetricsService(registry=reg)
        assert svc.registry is reg

    def test_custom_namespace(self) -> None:
        svc = MetricsService(namespace="custom")
        status = _make_relay_status()
        svc.update_from_status(status)
        # Namespace prefixes sp-rtk-base-specific gauges (input/dest/etc.)
        val = _get_gauge_value(svc.registry, "custom_input_connected")
        assert val == 1.0
        # Relay engine gauges use the fixed sp_rtk_base_relay_* prefix,
        # independent of this app's namespace.
        assert (
            _get_gauge_value(svc.registry, "sp_rtk_base_relay_running") == 1.0
        )


# ---------------------------------------------------------------------------
# Tests: update_from_status
# ---------------------------------------------------------------------------


class TestUpdateFromStatus:
    """Tests for MetricsService.update_from_status."""

    def test_global_running(self) -> None:
        svc = MetricsService()
        svc.update_from_status(_make_relay_status(running=True))
        assert _get_gauge_value(svc.registry, "sp_rtk_base_relay_running") == 1.0

    def test_global_not_running(self) -> None:
        svc = MetricsService()
        svc.update_from_status(_make_relay_status(running=False))
        assert _get_gauge_value(svc.registry, "sp_rtk_base_relay_running") == 0.0

    def test_uptime(self) -> None:
        svc = MetricsService()
        svc.update_from_status(_make_relay_status(uptime=99.5))
        assert _get_gauge_value(svc.registry, "sp_rtk_base_relay_uptime_seconds") == 99.5

    def test_input_connected(self) -> None:
        svc = MetricsService()
        svc.update_from_status(_make_relay_status(input_connected=True))
        assert _get_gauge_value(svc.registry, "sp_rtk_base_input_connected") == 1.0

    def test_input_disconnected(self) -> None:
        svc = MetricsService()
        svc.update_from_status(_make_relay_status(input_connected=False))
        assert _get_gauge_value(svc.registry, "sp_rtk_base_input_connected") == 0.0

    def test_input_bytes_received(self) -> None:
        svc = MetricsService()
        svc.update_from_status(_make_relay_status())
        assert _get_gauge_value(svc.registry, "sp_rtk_base_input_bytes_received") == 1000.0

    def test_active_destinations(self) -> None:
        svc = MetricsService()
        dests = [
            _make_destination_status("d1", connected=True),
            _make_destination_status("d2", connected=False),
        ]
        svc.update_from_status(_make_relay_status(destinations=dests))
        assert _get_gauge_value(svc.registry, "sp_rtk_base_active_destinations") == 1.0

    def test_total_destinations(self) -> None:
        svc = MetricsService()
        dests = [
            _make_destination_status("d1"),
            _make_destination_status("d2"),
        ]
        svc.update_from_status(_make_relay_status(destinations=dests))
        assert _get_gauge_value(svc.registry, "sp_rtk_base_total_destinations") == 2.0

    def test_chunks_distributed(self) -> None:
        svc = MetricsService()
        svc.update_from_status(_make_relay_status())
        assert _get_gauge_value(svc.registry, "sp_rtk_base_chunks_distributed") == 50.0

    def test_frames_parsed(self) -> None:
        svc = MetricsService()
        svc.update_from_status(_make_relay_status())
        assert _get_gauge_value(svc.registry, "sp_rtk_base_frames_parsed") == 10.0


# ---------------------------------------------------------------------------
# Tests: per-destination metrics
# ---------------------------------------------------------------------------


class TestPerDestinationMetrics:
    """Tests for per-destination labelled metrics."""

    def test_dest_connected(self) -> None:
        svc = MetricsService()
        svc.update_from_status(_make_relay_status())
        val = _get_labelled_gauge(
            svc.registry, "sp_rtk_base_dest_connected", {"destination": "test-dest"}
        )
        assert val == 1.0

    def test_dest_disconnected(self) -> None:
        svc = MetricsService()
        dests = [_make_destination_status("d1", connected=False)]
        svc.update_from_status(_make_relay_status(destinations=dests))
        val = _get_labelled_gauge(
            svc.registry, "sp_rtk_base_dest_connected", {"destination": "d1"}
        )
        assert val == 0.0

    def test_dest_bytes_sent(self) -> None:
        svc = MetricsService()
        dests = [_make_destination_status("d1", bytes_sent=9999)]
        svc.update_from_status(_make_relay_status(destinations=dests))
        val = _get_labelled_gauge(
            svc.registry, "sp_rtk_base_dest_bytes_sent", {"destination": "d1"}
        )
        assert val == 9999.0

    def test_dest_errors(self) -> None:
        svc = MetricsService()
        dests = [_make_destination_status("d1", errors=5)]
        svc.update_from_status(_make_relay_status(destinations=dests))
        val = _get_labelled_gauge(
            svc.registry, "sp_rtk_base_dest_errors", {"destination": "d1"}
        )
        assert val == 5.0

    def test_dest_queue_depth(self) -> None:
        svc = MetricsService()
        dests = [_make_destination_status("d1", queue_depth=42)]
        svc.update_from_status(_make_relay_status(destinations=dests))
        val = _get_labelled_gauge(
            svc.registry, "sp_rtk_base_dest_queue_depth", {"destination": "d1"}
        )
        assert val == 42.0

    def test_multiple_destinations(self) -> None:
        svc = MetricsService()
        dests = [
            _make_destination_status("alpha", bytes_sent=100),
            _make_destination_status("beta", bytes_sent=200),
        ]
        svc.update_from_status(_make_relay_status(destinations=dests))
        alpha = _get_labelled_gauge(
            svc.registry, "sp_rtk_base_dest_bytes_sent", {"destination": "alpha"}
        )
        beta = _get_labelled_gauge(
            svc.registry, "sp_rtk_base_dest_bytes_sent", {"destination": "beta"}
        )
        assert alpha == 100.0
        assert beta == 200.0


# ---------------------------------------------------------------------------
# Tests: update_idle
# ---------------------------------------------------------------------------


class TestUpdateIdle:
    """Tests for MetricsService.update_idle."""

    def test_idle_resets_running(self) -> None:
        svc = MetricsService()
        # First set running
        svc.update_from_status(_make_relay_status(running=True))
        assert _get_gauge_value(svc.registry, "sp_rtk_base_relay_running") == 1.0
        # Then go idle
        svc.update_idle()
        assert _get_gauge_value(svc.registry, "sp_rtk_base_relay_running") == 0.0

    def test_idle_zeros_uptime(self) -> None:
        svc = MetricsService()
        svc.update_from_status(_make_relay_status(uptime=120.0))
        svc.update_idle()
        assert _get_gauge_value(svc.registry, "sp_rtk_base_relay_uptime_seconds") == 0.0

    def test_idle_sets_no_data(self) -> None:
        svc = MetricsService()
        svc.update_idle()
        assert (
            _get_gauge_value(svc.registry, "sp_rtk_base_input_seconds_since_last_data")
            == -1.0
        )

    def test_idle_zeros_destinations(self) -> None:
        svc = MetricsService()
        svc.update_from_status(_make_relay_status())
        svc.update_idle()
        assert _get_gauge_value(svc.registry, "sp_rtk_base_active_destinations") == 0.0
        assert _get_gauge_value(svc.registry, "sp_rtk_base_total_destinations") == 0.0
