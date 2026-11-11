"""API tests for the info-style and process metrics on /metrics.

Input kind, build version and the standard process metrics, read from a
real scrape.
"""

from __future__ import annotations

import importlib.metadata
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import AppSettings, InputProfile, InputSource
from sp_rtk_base.services import (
    get_config_service,
    get_metrics_service,
    get_relay_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.relay_service import RelayService


def _scrape(input_profile: InputProfile | None = None) -> str:
    relay = MagicMock(spec=RelayService)
    relay.is_running = False
    config = MagicMock(spec=ConfigService)
    config.get_settings.return_value = AppSettings(metrics_enabled=True)
    config.get_input_config.return_value = input_profile
    app = create_api_app()
    app.dependency_overrides[get_relay_service] = lambda: relay
    metrics = MetricsService()
    app.dependency_overrides[get_metrics_service] = lambda: metrics
    app.dependency_overrides[get_config_service] = lambda: config
    resp = TestClient(app).get("/metrics")
    assert resp.status_code == 200
    return resp.text


def _samples(body: str, name: str) -> list[tuple[dict[str, str], float]]:
    return [
        (dict(s.labels), s.value)
        for family in text_string_to_metric_families(body)
        for s in family.samples
        if s.name == name
    ]


class TestBuildInfo:
    def test_build_info_is_one_with_the_installed_package_version(self) -> None:
        samples = _samples(_scrape(), "sp_rtk_base_build_info")

        assert samples == [
            ({"version": importlib.metadata.version("sp-rtk-base")}, 1.0)
        ]


class TestInputInfo:
    @pytest.mark.parametrize("kind", ["bluetooth", "serial", "tcp"])
    def test_input_info_is_one_labelled_with_the_input_profile_type(
        self, kind: InputSource
    ) -> None:
        body = _scrape(InputProfile(source=kind))

        assert _samples(body, "sp_rtk_base_input_info") == [({"kind": kind}, 1.0)]

    def test_a_saved_usb_serial_profile_is_exported_as_serial(self) -> None:
        # Not an InputSource: only a saved profile's JSON can still say it.
        body = _scrape(InputProfile.model_validate({"source": "usb_serial"}))

        assert _samples(body, "sp_rtk_base_input_info") == [({"kind": "serial"}, 1.0)]

    def test_input_info_is_absent_without_an_input_profile(self) -> None:
        assert _samples(_scrape(None), "sp_rtk_base_input_info") == []

    def test_changing_the_input_profile_replaces_the_old_kind(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        config = MagicMock(spec=ConfigService)
        config.get_settings.return_value = AppSettings(metrics_enabled=True)
        metrics = MetricsService()
        app = create_api_app()
        app.dependency_overrides[get_relay_service] = lambda: relay
        app.dependency_overrides[get_metrics_service] = lambda: metrics
        app.dependency_overrides[get_config_service] = lambda: config
        client = TestClient(app)

        config.get_input_config.return_value = InputProfile(source="tcp")
        client.get("/metrics")
        config.get_input_config.return_value = InputProfile(source="bluetooth")
        body = client.get("/metrics").text

        assert _samples(body, "sp_rtk_base_input_info") == [
            ({"kind": "bluetooth"}, 1.0)
        ]
        config.get_input_config.return_value = None
        body = client.get("/metrics").text
        assert _samples(body, "sp_rtk_base_input_info") == []


class TestProcessMetrics:
    @pytest.mark.parametrize(
        ("name", "metric_type"),
        [
            ("process_cpu_seconds_total", "counter"),
            ("process_resident_memory_bytes", "gauge"),
            ("process_virtual_memory_bytes", "gauge"),
            ("process_start_time_seconds", "gauge"),
            ("process_open_fds", "gauge"),
            ("process_max_fds", "gauge"),
        ],
    )
    def test_the_standard_process_metrics_are_exported(
        self, name: str, metric_type: str
    ) -> None:
        family_name = name.removesuffix("_total")
        families = {f.name: f for f in text_string_to_metric_families(_scrape())}

        assert families[family_name].type == metric_type
        [sample] = _samples(_scrape(), name)
        assert sample[0] == {}
        assert sample[1] > 0
