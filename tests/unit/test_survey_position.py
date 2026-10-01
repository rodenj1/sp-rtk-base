"""Tests for the u-blox driver's high-precision survey position read.

Seam under test: ``UbloxDriver.get_survey_position()``, with its serial port
and UBXReader mocked as in ``test_ublox_driver.py``. The fake driver's read is
tested in ``test_fake_driver.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sp_rtk_base.services.drivers.ublox import UbloxDriver
from sp_rtk_base.services.geodesy import ecef_to_llh, llh_to_ecef

# ---------------------------------------------------------------------------
# u-blox driver
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_fcntl() -> object:  # type: ignore[misc]
    with patch("sp_rtk_base.services.drivers.ublox.fcntl.flock"):
        yield


def _hpposecef(
    x_cm: float, y_cm: float, z_cm: float, p_acc_mm: float, invalid: int = 0
) -> SimpleNamespace:
    """NAV-HPPOSECEF as pyubx2 decodes it: cm with the 0.1 mm HP folded in."""
    return SimpleNamespace(
        identity="NAV-HPPOSECEF",
        ecefX=x_cm,
        ecefY=y_cm,
        ecefZ=z_cm,
        invalidEcef=invalid,
        pAcc=p_acc_mm,
    )


def _pvt(
    fix_type: int = 3,
    gnss_fix_ok: int = 1,
    carr_soln: int = 0,
    diff_soln: int = 0,
    last_correction_age: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        identity="NAV-PVT",
        fixType=fix_type,
        gnssFixOk=gnss_fix_ok,
        carrSoln=carr_soln,
        diffSoln=diff_soln,
        lastCorrectionAge=last_correction_age,
    )


def _driver_answering(*replies: SimpleNamespace) -> UbloxDriver:
    """A connected UbloxDriver whose reader returns ``replies`` in order."""
    driver = UbloxDriver()
    reader = MagicMock()
    reader.read = MagicMock(side_effect=[(b"", reply) for reply in replies])
    serial = MagicMock()
    serial.is_open = True
    driver._serial = serial  # pyright: ignore[reportPrivateUsage]
    driver._reader = reader  # pyright: ignore[reportPrivateUsage]
    return driver


class TestUbloxSurveyPosition:
    def test_reads_the_high_precision_ecef_position_at_0_1_mm(self) -> None:
        driver = _driver_answering(
            _hpposecef(-245000011.93, -466500034.03, 343200056.05, 123.4),
            _pvt(),
        )

        position = driver.get_survey_position()

        assert position.ecef_x_m == pytest.approx(-2450000.1193, abs=1e-6)
        assert position.ecef_y_m == pytest.approx(-4665000.3403, abs=1e-6)
        assert position.ecef_z_m == pytest.approx(3432000.5605, abs=1e-6)
        assert position.accuracy_3d_m == pytest.approx(0.1234)

    @pytest.mark.parametrize(
        ("carr_soln", "rtk_status"), [(0, "none"), (1, "float"), (2, "fixed")]
    )
    def test_reports_the_rtk_status(self, carr_soln: int, rtk_status: str) -> None:
        driver = _driver_answering(
            _hpposecef(1.0, 2.0, 3.0, 10.0),
            _pvt(carr_soln=carr_soln, diff_soln=1, last_correction_age=1),
        )

        assert driver.get_survey_position().rtk_status == rtk_status

    @pytest.mark.parametrize(
        ("pvt", "fix_ok"),
        [
            (_pvt(fix_type=3, gnss_fix_ok=1), True),  # 3D
            (_pvt(fix_type=4, gnss_fix_ok=1), True),  # GNSS + dead reckoning
            (_pvt(fix_type=2, gnss_fix_ok=1), False),  # 2D only
            (_pvt(fix_type=3, gnss_fix_ok=0), False),  # fix not valid
        ],
    )
    def test_fix_ok_means_a_valid_3d_fix(
        self, pvt: SimpleNamespace, fix_ok: bool
    ) -> None:
        driver = _driver_answering(_hpposecef(1.0, 2.0, 3.0, 10.0), pvt)

        assert driver.get_survey_position().fix_ok is fix_ok

    def test_an_invalid_ecef_position_is_not_a_valid_fix(self) -> None:
        driver = _driver_answering(_hpposecef(1.0, 2.0, 3.0, 10.0, invalid=1), _pvt())

        assert driver.get_survey_position().fix_ok is False

    @pytest.mark.parametrize(
        ("diff_soln", "age_code", "age_s"),
        [
            (0, 0, None),  # no corrections in use
            (1, 0, None),  # in use, but the receiver gives no age
            (1, 1, 1.0),  # 0-1 s
            (1, 3, 5.0),  # 2-5 s
            (1, 8, 45.0),  # 30-45 s
            (1, 12, 120.0),  # >= 120 s
        ],
    )
    def test_reports_the_correction_age(
        self, diff_soln: int, age_code: int, age_s: float | None
    ) -> None:
        driver = _driver_answering(
            _hpposecef(1.0, 2.0, 3.0, 10.0),
            _pvt(diff_soln=diff_soln, last_correction_age=age_code),
        )

        assert driver.get_survey_position().correction_age_s == age_s


class TestGeodesy:
    def test_llh_and_ecef_round_trip_to_well_under_a_millimetre(self) -> None:
        llh = (32.7329015, -117.2362788, 27.94)

        lat, lon, alt = ecef_to_llh(*llh_to_ecef(*llh))

        assert lat == pytest.approx(llh[0], abs=1e-9)  # ~0.1 mm
        assert lon == pytest.approx(llh[1], abs=1e-9)
        assert alt == pytest.approx(llh[2], abs=1e-4)
