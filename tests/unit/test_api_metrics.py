"""Unit tests for the /metrics API endpoint."""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from sp_rtk_base_relay import FrameSubscription
from sp_rtk_base_relay.core.status import (
    DestinationStatus,
    InputStatus,
    RelayStatus,
)

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import AppSettings
from sp_rtk_base.models.device_models import SerialLink
from sp_rtk_base.models.signal_quality_models import Signal
from sp_rtk_base.services import (
    get_config_service,
    get_metrics_service,
    get_relay_service,
    get_signal_quality_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.fake import FakeGpsDriver
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.relay_service import RelayService
from sp_rtk_base.services.signal_quality.service import SignalQualityService
from tests.unit.msm_frames import epoch_frames, sky

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_relay_status() -> RelayStatus:
    return RelayStatus(
        running=True,
        uptime_seconds=60.0,
        input=InputStatus(
            connected=True,
            source_type="TCP",
            bytes_received=5000,
            messages_received=100,
            seconds_since_last_data=0.3,
            reconnect_attempts=1,
            reconnect_successes=1,
            connected_since=1000.0,
        ),
        destinations=[
            DestinationStatus(
                name="test-dest",
                destination_type="tcp_server",
                enabled=True,
                running=True,
                connected=True,
                filter_mode="pass_all",
                bytes_sent=2000,
                messages_sent=50,
                messages_dropped=0,
                messages_filtered=0,
                errors=0,
                last_error=None,
                queue_depth=2,
                connected_since=1000.0,
                uptime_seconds=55.0,
                connection_attempts=1,
                successful_connections=1,
            ),
        ],
        active_destination_count=1,
        total_destination_count=1,
        bytes_received=5000,
        chunks_distributed=100,
        frames_parsed=20,
        no_data_warnings=0,
    )


def _create_client(
    relay_mock: MagicMock,
    metrics_svc: MetricsService,
    config_svc: MagicMock | ConfigService | None = None,
) -> TestClient:
    """Create a TestClient with injected mocks."""
    app = create_api_app()
    app.dependency_overrides[get_relay_service] = lambda: relay_mock
    app.dependency_overrides[get_metrics_service] = lambda: metrics_svc
    if config_svc is not None:
        app.dependency_overrides[get_config_service] = lambda: config_svc
    else:
        # Default: metrics enabled
        mock_cfg = MagicMock(spec=ConfigService)
        mock_cfg.get_settings.return_value = AppSettings(metrics_enabled=True)
        app.dependency_overrides[get_config_service] = lambda: mock_cfg
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests: /metrics endpoint
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    """Tests for GET /metrics."""

    def test_returns_200(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_content_type_prometheus(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        assert "text/plain" in resp.headers["content-type"]

    def test_idle_when_relay_stopped(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        body = resp.text

        assert "sp_rtk_base_relay_running 0.0" in body
        assert "sp_rtk_base_relay_uptime_seconds 0.0" in body
        assert "sp_rtk_base_input_connected 0.0" in body

    def test_metrics_populated_when_running(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = True
        relay.get_status = AsyncMock(return_value=_make_relay_status())
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        body = resp.text

        assert "sp_rtk_base_relay_running 1.0" in body
        assert "sp_rtk_base_relay_uptime_seconds 60.0" in body
        assert "sp_rtk_base_input_connected 1.0" in body
        assert "sp_rtk_base_input_bytes_received 5000.0" in body
        assert "sp_rtk_base_active_destinations 1.0" in body
        assert "sp_rtk_base_chunks_distributed 100.0" in body

    def test_per_destination_labels(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = True
        relay.get_status = AsyncMock(return_value=_make_relay_status())
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        body = resp.text

        assert 'sp_rtk_base_dest_connected{destination="test-dest"} 1.0' in body
        assert 'sp_rtk_base_dest_bytes_sent{destination="test-dest"} 2000.0' in body

    def test_idle_when_status_returns_none(self) -> None:
        """When relay is 'running' but get_status returns None."""
        relay = MagicMock(spec=RelayService)
        relay.is_running = True
        relay.get_status = AsyncMock(return_value=None)
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        body = resp.text

        assert "sp_rtk_base_relay_running 0.0" in body

    def test_contains_help_text(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        body = resp.text

        assert "# HELP sp_rtk_base_relay_running" in body
        assert "# TYPE sp_rtk_base_relay_running gauge" in body


class TestMetricsDisabled:
    """Tests for GET /metrics when metrics_enabled=False."""

    def test_returns_404_when_disabled(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        cfg = MagicMock(spec=ConfigService)
        cfg.get_settings.return_value = AppSettings(metrics_enabled=False)
        client = _create_client(relay, MetricsService(), config_svc=cfg)

        resp = client.get("/metrics")
        assert resp.status_code == 404

    def test_disabled_returns_json_detail(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        cfg = MagicMock(spec=ConfigService)
        cfg.get_settings.return_value = AppSettings(metrics_enabled=False)
        client = _create_client(relay, MetricsService(), config_svc=cfg)

        resp = client.get("/metrics")
        assert resp.json()["detail"] == "Metrics are disabled"

    def test_enabled_returns_200(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        cfg = MagicMock(spec=ConfigService)
        cfg.get_settings.return_value = AppSettings(metrics_enabled=True)
        client = _create_client(relay, MetricsService(), config_svc=cfg)

        resp = client.get("/metrics")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Signal Quality gauges (#171)
# ---------------------------------------------------------------------------


def _scrape_with(signal_quality: SignalQualityService) -> dict[str, float]:
    """Scrape /metrics and return the sp_rtk_base_signal_* samples."""
    relay = MagicMock(spec=RelayService)
    relay.is_running = False
    client = _create_client(relay, MetricsService())
    client.app.dependency_overrides[get_signal_quality_service] = (  # type: ignore[attr-defined]
        lambda: signal_quality
    )
    samples: dict[str, float] = {}
    for family in text_string_to_metric_families(client.get("/metrics").text):
        for sample in family.samples:
            if sample.name.startswith("sp_rtk_base_signal_"):
                band = sample.labels.get("band")
                samples[f"{sample.name}{{{band}}}" if band else sample.name] = (
                    sample.value
                )
    return samples


def _relay_fed(signals: tuple[Signal, ...]) -> SignalQualityService:
    service = SignalQualityService(DeviceService())
    service.relay_started(FrameSubscription())
    for frame in epoch_frames(signals):
        service.take_frame(frame)
    return service


class TestSignalQualityGauges:
    """The four Signal Quality gauges equal what the UI shows."""

    def test_a_verdict_is_exported_as_four_gauges(self) -> None:
        samples = _scrape_with(_relay_fed(sky(20, l1=52.0, l2=50.0)))

        assert samples == {
            "sp_rtk_base_signal_quality": 0.0,  # Good
            "sp_rtk_base_signal_band_strength_dbhz{L1}": 52.0,
            "sp_rtk_base_signal_band_strength_dbhz{L2}": 50.0,
            "sp_rtk_base_signal_usable_satellites": 20.0,
            "sp_rtk_base_signal_available": 1.0,
        }

    def test_the_verdict_level_is_numeric(self) -> None:
        marginal = _scrape_with(_relay_fed(sky(12, l1=50.0, l2=50.0)))
        poor = _scrape_with(_relay_fed(sky(20, l1=50.0, l2=None)))

        assert marginal["sp_rtk_base_signal_quality"] == 1.0
        assert poor["sp_rtk_base_signal_quality"] == 2.0
        assert math.isnan(poor["sp_rtk_base_signal_band_strength_dbhz{L2}"])  # no L2

    def test_no_data_is_available_zero_and_nan_but_the_series_stay(self) -> None:
        samples = _scrape_with(SignalQualityService(DeviceService()))

        assert samples["sp_rtk_base_signal_available"] == 0.0
        for name in (
            "sp_rtk_base_signal_quality",
            "sp_rtk_base_signal_band_strength_dbhz{L1}",
            "sp_rtk_base_signal_band_strength_dbhz{L2}",
            "sp_rtk_base_signal_usable_satellites",
        ):
            assert math.isnan(samples[name]), name

    @pytest.mark.asyncio()
    async def test_survey_in_mode_is_exported_too(self) -> None:
        driver = FakeGpsDriver()  # clear sky: L1 52, L2 51, 28 satellites
        device = DeviceService()
        device.set_driver(driver)
        await device.connect(SerialLink(port="FAKE", baud_rate=115200))
        service = SignalQualityService(device)
        await service.poll_once()

        samples = _scrape_with(service)

        assert samples["sp_rtk_base_signal_available"] == 1.0
        assert samples["sp_rtk_base_signal_quality"] == 0.0
        assert samples["sp_rtk_base_signal_usable_satellites"] == 28.0
