"""While an Update runs, nothing new starts on code about to be replaced
(sp-rtk-base#240).

Start, Survey-in, Console connect and Check now are refused through the
REST API. The Update runs as far as the app can tell: ``status.json`` in
the update directory says ``installing``, as the updater would write it.
Boot auto-start is the exception: it is how the Relay resumes after the
restart.
"""

from __future__ import annotations

from collections.abc import Iterator
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import DestinationProfile, InputProfile
from sp_rtk_base.services import (
    get_config_service,
    get_device_service,
    get_relay_service,
    get_survey_service,
    get_update_check_service,
    get_update_service,
    wire_update_guard,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.relay_service import RelayService
from sp_rtk_base.services.survey_service import SurveyService
from sp_rtk_base.services.update_check import UpdateCheckService
from sp_rtk_base.services.update_service import UpdateService
from sp_rtk_base.update.state import (
    UPDATING_MESSAGE,
    UpdateFiles,
    UpdateStatus,
    Versions,
)

relay_mod = import_module("sp_rtk_base.services.relay_service")


@pytest.fixture()
def config(tmp_path: Path) -> ConfigService:
    cfg = ConfigService(config_path=tmp_path / "config.yaml")
    cfg.save_input_config(
        InputProfile(source="tcp", config={"host": "10.0.0.1", "port": 5000})
    )
    cfg.save_destination(
        DestinationProfile(name="out", type="tcp_server", config={"port": 9000})
    )
    return cfg


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    stub = MagicMock()
    stub.is_running = False

    def _start(destinations: Any = None) -> None:
        stub.is_running = True

    stub.start = _start
    monkeypatch.setattr(relay_mod, "RelayEngine", lambda cfg: stub)  # pyright: ignore[reportUnknownLambdaType]
    return stub


@pytest.fixture()
def files(tmp_path: Path) -> UpdateFiles:
    return UpdateFiles(tmp_path / "update")


@pytest.fixture()
def relay(config: ConfigService, engine: MagicMock) -> RelayService:
    return RelayService(config.get_config)


@pytest.fixture()
def client(
    config: ConfigService, relay: RelayService, files: UpdateFiles
) -> Iterator[TestClient]:
    device = DeviceService()
    survey = SurveyService(device)
    update = UpdateService(files, running=Versions(app="0.9.0", relay="4.1.0"))
    checker = UpdateCheckService(
        MagicMock(), running_app="0.9.0", running_relay="4.1.0"
    )
    wire_update_guard(update, relay, device, survey)
    app = create_api_app()
    app.dependency_overrides[get_config_service] = lambda: config
    app.dependency_overrides[get_relay_service] = lambda: relay
    app.dependency_overrides[get_device_service] = lambda: device
    app.dependency_overrides[get_survey_service] = lambda: survey
    app.dependency_overrides[get_update_service] = lambda: update
    app.dependency_overrides[get_update_check_service] = lambda: checker
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def updating(files: UpdateFiles) -> None:
    files.write_status(
        UpdateStatus(
            phase="installing",
            from_=Versions(app="0.9.0", relay="4.1.0"),
            to=Versions(app="0.10.1", relay="4.2.0"),
        )
    )


@pytest.mark.usefixtures("updating")
class TestWhileUpdating:
    def test_start_is_refused(self, client: TestClient, engine: MagicMock) -> None:
        response = client.post("/api/relay/start")

        assert response.status_code == 409
        assert response.json()["code"] == "updating"
        assert response.json()["message"] == UPDATING_MESSAGE
        assert engine.is_running is False

    def test_survey_in_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/api/device/configure/survey-in",
            json={"min_duration_seconds": 60, "accuracy_limit_mm": 2000},
        )

        assert response.status_code == 409
        assert response.json()["detail"] == UPDATING_MESSAGE

    def test_console_connect_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/api/device/connect",
            json={"vendor": "ublox", "port": "/dev/sim", "baud_rate": 38400},
        )

        assert response.status_code == 409
        assert response.json()["detail"] == UPDATING_MESSAGE

    def test_check_now_is_refused(self, client: TestClient) -> None:
        response = client.post("/api/update/check")

        assert response.status_code == 409
        assert response.json()["code"] == "updating"

    @pytest.mark.asyncio
    async def test_boot_auto_start_still_resumes_the_relay(
        self, relay: RelayService, engine: MagicMock
    ) -> None:
        await relay.start_saved(
            trigger="auto-start",
            refuse_while_console_connected=False,
            refuse_while_updating=False,
        )

        assert engine.is_running is True


def test_start_works_once_the_update_has_finished(
    client: TestClient, files: UpdateFiles, engine: MagicMock
) -> None:
    files.write_status(UpdateStatus(phase="done"))

    response = client.post("/api/relay/start")

    assert response.status_code == 200, response.text
    assert engine.is_running is True
