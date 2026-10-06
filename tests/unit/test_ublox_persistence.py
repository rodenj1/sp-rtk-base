"""What the receiver comes back with after a reset (sp-rtk-base#221).

Driven through ``UbloxDriver``'s public methods against a simulated
receiver that models the RAM/BBR/Flash/Default configuration layers, so
each test states the outcome an operator sees: after a reset or power
cycle, is the base still the fixed base they committed?
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from sp_rtk_base.models.device_models import FixedBaseConfig, SurveyInConfig
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.ublox import UbloxDriver
from tests.fixtures.simulated_ublox import (
    BBR,
    FLASH,
    RAM,
    SimulatedSerial,
    SimulatedUblox,
)

FIXED = 2
SURVEY_IN = 1

HOME = FixedBaseConfig(
    latitude=32.7328957,
    longitude=-117.2362787,
    altitude_m=29.091,
    accuracy_mm=17,
)


SURVEY = SurveyInConfig(min_duration_seconds=120, accuracy_limit_mm=2000)


@pytest.fixture()
def receiver() -> SimulatedUblox:
    return SimulatedUblox()


@pytest.fixture()
def unconnected(receiver: SimulatedUblox) -> Iterator[UbloxDriver]:
    """A driver whose serial port opens onto ``receiver``."""
    with (
        patch(
            "sp_rtk_base.services.drivers.ublox.serial.Serial",
            side_effect=lambda **kw: SimulatedSerial(receiver, **kw),
        ),
        patch("sp_rtk_base.services.drivers.ublox.fcntl.flock"),
        patch("sp_rtk_base.services.drivers.ublox.time.sleep"),
    ):
        drv = UbloxDriver()
        yield drv
        drv.disconnect()


@pytest.fixture()
def driver(unconnected: UbloxDriver) -> UbloxDriver:
    unconnected.connect("/dev/sim")
    return unconnected


class TestFixedBaseSurvivesReset:
    def test_a_fixed_base_that_flash_did_not_keep_is_reported_not_saved(
        self, driver: UbloxDriver, receiver: SimulatedUblox
    ) -> None:
        receiver.flash_ignores_writes = True

        with pytest.raises(RuntimeError, match="not saved"):
            driver.configure_fixed_base(HOME)

    def test_fixed_base_is_still_fixed_after_a_battery_backed_power_cycle(
        self, driver: UbloxDriver, receiver: SimulatedUblox
    ) -> None:
        driver.configure_fixed_base(HOME)

        receiver.power_cycle(backup_battery=True)

        assert receiver.value("CFG_TMODE_MODE") == FIXED
        assert receiver.value("CFG_TMODE_LAT") == 327328957
        assert receiver.value("CFG_TMODE_LON") == -1172362787


class TestInterruptedSurveyInIsAbandoned:
    """An unfinished Survey-in never persists: the base falls back."""

    def test_reset_mid_survey_returns_to_the_previous_fixed_base(
        self, driver: UbloxDriver, receiver: SimulatedUblox
    ) -> None:
        driver.configure_fixed_base(HOME)
        saved_flash = dict(receiver.layers[FLASH])
        driver.configure_survey_in(SURVEY)
        assert receiver.value("CFG_TMODE_MODE") == SURVEY_IN

        receiver.power_cycle(backup_battery=True)

        assert receiver.value("CFG_TMODE_MODE") == FIXED
        assert receiver.value("CFG_TMODE_LAT") == 327328957
        assert receiver.layers[FLASH] == saved_flash
        assert receiver.layers[BBR] == {}

    def test_cancels_hardware_reset_returns_to_the_previous_fixed_base(
        self, driver: UbloxDriver, receiver: SimulatedUblox
    ) -> None:
        driver.configure_fixed_base(HOME)
        saved_flash = dict(receiver.layers[FLASH])
        driver.configure_survey_in(SURVEY)

        driver.reset_and_reconnect()

        assert receiver.layers[FLASH] == saved_flash
        assert receiver.layers[BBR] == {}

        assert receiver.value("CFG_TMODE_MODE") == FIXED
        assert receiver.value("CFG_TMODE_LAT") == 327328957
        assert receiver.svin_dur == 0

    def test_failed_survey_in_start_rolls_back_to_the_previous_fixed_base(
        self, driver: UbloxDriver, receiver: SimulatedUblox
    ) -> None:
        driver.configure_fixed_base(HOME)
        receiver.survey_engine_stalls = True

        with pytest.raises(RuntimeError, match="did not engage"):
            driver.configure_survey_in(SURVEY)

        assert receiver.value("CFG_TMODE_MODE") == FIXED
        assert receiver.value("CFG_TMODE_LAT") == 327328957
        assert receiver.value("CFG_TMODE_MODE", FLASH) == FIXED
        assert receiver.value("CFG_TMODE_MODE", BBR) is None


class TestConnectRepairsBbrResidue:
    """Receivers damaged before the fix are repaired on Connect."""

    @pytest.mark.asyncio()
    async def test_connect_deletes_tmode_keys_left_in_bbr_and_leaves_ram_alone(
        self, unconnected: UbloxDriver, receiver: SimulatedUblox
    ) -> None:
        # test-base on 2026-10-06: Flash and RAM fixed, BBR "base mode off".
        saved = {
            "CFG_TMODE_MODE": FIXED,
            "CFG_TMODE_POS_TYPE": 1,
            "CFG_TMODE_LAT": 327328957,
            "CFG_TMODE_LON": -1172362787,
        }
        receiver.store(FLASH, **saved)
        receiver.store(BBR, CFG_TMODE_MODE=0)
        receiver.store(RAM, **saved)

        service = DeviceService()
        service.set_driver(unconnected)
        await service.connect("/dev/sim")

        assert receiver.value("CFG_TMODE_MODE", BBR) is None
        assert receiver.value("CFG_TMODE_MODE") == FIXED
        assert receiver.resets == []
        receiver.power_cycle(backup_battery=True)
        assert receiver.value("CFG_TMODE_MODE") == FIXED


class TestSaveToFlash:
    def test_saves_ram_to_flash_and_never_to_bbr(
        self, driver: UbloxDriver, receiver: SimulatedUblox
    ) -> None:
        receiver.store(RAM, CFG_RATE_MEAS=200)

        driver.save_to_flash()

        assert receiver.value("CFG_RATE_MEAS", FLASH) == 200
        assert receiver.value("CFG_RATE_MEAS", BBR) is None


class TestFixedBaseFullPrecision:
    def test_commit_writes_the_high_precision_llh_parts(
        self, driver: UbloxDriver, receiver: SimulatedUblox
    ) -> None:
        # A stale HP part from an earlier base must not survive the commit.
        receiver.store(FLASH, CFG_TMODE_LAT_HP=55, CFG_TMODE_HEIGHT_HP=-40)
        receiver.store(RAM, CFG_TMODE_LAT_HP=55, CFG_TMODE_HEIGHT_HP=-40)
        surveyed = FixedBaseConfig(
            latitude=32.732895709,
            longitude=-117.236278832,
            altitude_m=29.1172,
            accuracy_mm=17,
        )

        driver.configure_fixed_base(surveyed)
        receiver.power_cycle(backup_battery=True)

        # 1e-7 deg + 1e-9 deg HP; cm + 0.1 mm HP; HP shares the sign.
        assert receiver.value("CFG_TMODE_LAT") == 327328957
        assert receiver.value("CFG_TMODE_LAT_HP") == 9
        assert receiver.value("CFG_TMODE_LON") == -1172362788
        assert receiver.value("CFG_TMODE_LON_HP") == -32
        assert receiver.value("CFG_TMODE_HEIGHT") == 2911
        assert receiver.value("CFG_TMODE_HEIGHT_HP") == 72


class TestRestoreMatchesAReset:
    def test_restoring_the_saved_base_honours_bbr_over_flash(
        self, driver: UbloxDriver, receiver: SimulatedUblox
    ) -> None:
        # Unrepaired: Flash says fixed, BBR still says base mode off.
        receiver.store(FLASH, CFG_TMODE_MODE=FIXED, CFG_TMODE_LAT=327328957)
        receiver.store(BBR, CFG_TMODE_MODE=0)
        receiver.store(RAM, CFG_TMODE_MODE=SURVEY_IN)

        driver.restore_saved_base_mode()
        restored = receiver.value("CFG_TMODE_MODE")
        receiver.power_cycle(backup_battery=True)

        assert restored == receiver.value("CFG_TMODE_MODE") == 0
