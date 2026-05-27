"""Unit tests for the in-memory ``FakeGpsDriver``.

The fake driver is used by the Playwright e2e suite to drive UI paths
that are gated on a connected GPS device.  These unit tests give us
≥90 % coverage of the fake driver itself so we can rely on its
behaviour from the e2e harness.

What we assert
--------------
- All 17 abstract methods of :class:`GpsReceiverDriver` are
  implemented (mypy already enforces this; we verify at runtime too).
- ``connect`` / ``disconnect`` flip the ``is_connected`` flag.
- Every method that touches device state raises ``ConnectionError``
  when called before ``connect()`` (mirrors the real ``UbloxDriver``
  contract).
- Configuration writes round-trip through subsequent reads.
- ``get_position`` returns the hard-coded May-26 2026 bug fixture
  values so e2e regression tests can assert on them.
- ``get_survey_in_status`` synthesises a deterministic accuracy
  convergence curve and auto-completes by the fast-complete cap,
  populating the base-config with the fixture position.
- ``list_serial_ports`` surfaces a single ``FAKE`` entry.
- The env-gated registration in ``services.drivers.__init__`` is
  exercised here too: with ``SP_RTK_BASE_FAKE_GPS=1`` set the
  ``"fake"`` key resolves to ``FakeGpsDriver``; without it the key
  is absent.
"""

from __future__ import annotations

import importlib
import os
import time
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from sp_rtk_base.models.device_models import (
    BaseMode,
    DeviceCapability,
    FixedBaseConfig,
    GnssConfig,
    GnssConstellation,
    GnssSystemConfig,
    GpsFixType,
    RtcmMessageConfig,
    RtcmPortConfig,
    SurveyInConfig,
)
from sp_rtk_base.services.drivers.base import GpsReceiverDriver
from sp_rtk_base.services.drivers.fake import (
    FAKE_PORT_LABEL,
    FakeGpsDriver,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def driver() -> FakeGpsDriver:
    """Return a fresh, disconnected fake driver instance."""
    return FakeGpsDriver()


@pytest.fixture()
def connected_driver() -> FakeGpsDriver:
    """Return a fake driver already in the connected state."""
    d = FakeGpsDriver()
    d.connect("FAKE", 115200)
    return d


# ---------------------------------------------------------------------------
# Identity & ABC compliance
# ---------------------------------------------------------------------------


class TestIdentity:
    """``vendor_name`` / ``get_capabilities`` / ABC compliance."""

    def test_is_subclass_of_gps_receiver_driver(self) -> None:
        """FakeGpsDriver must implement the full driver interface."""
        assert issubclass(FakeGpsDriver, GpsReceiverDriver)

    def test_can_be_instantiated_without_arguments(self) -> None:
        """No constructor args — driver registry calls ``cls()``."""
        d = FakeGpsDriver()
        assert isinstance(d, GpsReceiverDriver)

    def test_vendor_name(self, driver: FakeGpsDriver) -> None:
        """vendor_name property returns the fake label."""
        assert driver.vendor_name == "Fake"

    def test_capabilities_cover_all_ui_paths(self, driver: FakeGpsDriver) -> None:
        """Fake driver claims every capability so all UI paths render."""
        caps = driver.get_capabilities()
        assert DeviceCapability.SURVEY_IN in caps
        assert DeviceCapability.FIXED_BASE in caps
        assert DeviceCapability.RTCM_MESSAGE_SELECT in caps
        assert DeviceCapability.SAVE_TO_FLASH in caps
        assert DeviceCapability.POSITION_STREAM in caps
        assert DeviceCapability.SATELLITE_INFO in caps
        assert DeviceCapability.GNSS_SELECT in caps


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class TestConnectionLifecycle:
    """connect / disconnect / is_connected behaviour."""

    def test_starts_disconnected(self, driver: FakeGpsDriver) -> None:
        """Fresh instance has no connection."""
        assert driver.is_connected is False

    def test_connect_flips_state_and_returns_info(self, driver: FakeGpsDriver) -> None:
        """connect() succeeds and returns deterministic identity."""
        info = driver.connect("FAKE", 115200)
        assert driver.is_connected is True
        assert info.vendor == "Fake"
        assert info.model == "FAKE-F9P"
        assert info.firmware_version == "FAKE 1.0"

    def test_connect_accepts_any_port_label(self, driver: FakeGpsDriver) -> None:
        """Fake driver does no I/O — port string is just a label."""
        info = driver.connect("/dev/ttyANY", 9600)
        assert driver.is_connected is True
        assert info.model == "FAKE-F9P"

    def test_disconnect_clears_state(self, connected_driver: FakeGpsDriver) -> None:
        """disconnect() reverts is_connected to False."""
        connected_driver.disconnect()
        assert connected_driver.is_connected is False

    def test_disconnect_is_idempotent(self, driver: FakeGpsDriver) -> None:
        """Calling disconnect when already disconnected is safe."""
        driver.disconnect()  # never connected
        assert driver.is_connected is False

    def test_disconnect_aborts_in_progress_survey(
        self, connected_driver: FakeGpsDriver
    ) -> None:
        """A running survey is cancelled by disconnect."""
        connected_driver.configure_survey_in(
            SurveyInConfig(min_duration_seconds=60, accuracy_limit_mm=50000)
        )
        connected_driver.disconnect()
        # Reconnecting and polling should not see the old survey.
        connected_driver.connect("FAKE", 115200)
        status = connected_driver.get_survey_in_status()
        assert status.active is False
        assert status.valid is False


# ---------------------------------------------------------------------------
# Guard clauses — every state-touching method must raise when disconnected
# ---------------------------------------------------------------------------


class TestDisconnectedGuards:
    """Methods that need a connection raise ``ConnectionError`` cleanly.

    Mirrors the contract that ``UbloxDriver`` enforces — we want
    callers (DeviceService) to see consistent error types regardless
    of which driver is loaded.
    """

    def test_configure_survey_in_requires_connection(
        self, driver: FakeGpsDriver
    ) -> None:
        with pytest.raises(ConnectionError):
            driver.configure_survey_in(SurveyInConfig())

    def test_configure_fixed_base_requires_connection(
        self, driver: FakeGpsDriver
    ) -> None:
        with pytest.raises(ConnectionError):
            driver.configure_fixed_base(
                FixedBaseConfig(latitude=32.0, longitude=-117.0, altitude_m=10.0)
            )

    def test_configure_rtcm_messages_requires_connection(
        self, driver: FakeGpsDriver
    ) -> None:
        with pytest.raises(ConnectionError):
            driver.configure_rtcm_messages(RtcmMessageConfig())

    def test_get_rtcm_config_requires_connection(self, driver: FakeGpsDriver) -> None:
        with pytest.raises(ConnectionError):
            driver.get_rtcm_config()

    def test_get_rtcm_port_config_requires_connection(
        self, driver: FakeGpsDriver
    ) -> None:
        with pytest.raises(ConnectionError):
            driver.get_rtcm_port_config()

    def test_configure_rtcm_ports_requires_connection(
        self, driver: FakeGpsDriver
    ) -> None:
        with pytest.raises(ConnectionError):
            driver.configure_rtcm_ports(RtcmPortConfig())

    def test_save_to_flash_requires_connection(self, driver: FakeGpsDriver) -> None:
        with pytest.raises(ConnectionError):
            driver.save_to_flash()

    def test_get_gnss_config_requires_connection(self, driver: FakeGpsDriver) -> None:
        with pytest.raises(ConnectionError):
            driver.get_gnss_config()

    def test_configure_gnss_requires_connection(self, driver: FakeGpsDriver) -> None:
        with pytest.raises(ConnectionError):
            driver.configure_gnss(GnssConfig())

    def test_get_position_requires_connection(self, driver: FakeGpsDriver) -> None:
        with pytest.raises(ConnectionError):
            driver.get_position()

    def test_get_survey_in_status_requires_connection(
        self, driver: FakeGpsDriver
    ) -> None:
        with pytest.raises(ConnectionError):
            driver.get_survey_in_status()

    def test_get_device_info_requires_connection(self, driver: FakeGpsDriver) -> None:
        with pytest.raises(ConnectionError):
            driver.get_device_info()

    def test_get_base_config_requires_connection(self, driver: FakeGpsDriver) -> None:
        with pytest.raises(ConnectionError):
            driver.get_base_config()


# ---------------------------------------------------------------------------
# Configuration round-trips
# ---------------------------------------------------------------------------


class TestConfigurationRoundTrips:
    """Writes return the same value on a subsequent read."""

    def test_fixed_base_round_trip(self, connected_driver: FakeGpsDriver) -> None:
        """configure_fixed_base → get_base_config returns same coords."""
        cfg = FixedBaseConfig(
            latitude=32.7329015,
            longitude=-117.2362788,
            altitude_m=27.940,
            accuracy_mm=47308,
        )
        connected_driver.configure_fixed_base(cfg)
        current = connected_driver.get_base_config()
        assert current.mode is BaseMode.FIXED
        assert current.latitude == pytest.approx(32.7329015)
        assert current.longitude == pytest.approx(-117.2362788)
        assert current.altitude_m == pytest.approx(27.940)
        assert current.accuracy_mm == 47308

    def test_rtcm_messages_round_trip(self, connected_driver: FakeGpsDriver) -> None:
        cfg = RtcmMessageConfig(message_ids=[1005, 1077], rate_hz=2)
        connected_driver.configure_rtcm_messages(cfg)
        assert connected_driver.get_rtcm_config().message_ids == [1005, 1077]
        assert connected_driver.get_rtcm_config().rate_hz == 2

    def test_rtcm_ports_round_trip(self, connected_driver: FakeGpsDriver) -> None:
        cfg = RtcmPortConfig(
            messages={1005: {"USB": 0, "UART1": 1, "UART2": 0, "I2C": 0, "SPI": 0}}
        )
        connected_driver.configure_rtcm_ports(cfg)
        out = connected_driver.get_rtcm_port_config()
        assert out.messages[1005]["UART1"] == 1
        assert out.messages[1005]["USB"] == 0

    def test_gnss_round_trip(self, connected_driver: FakeGpsDriver) -> None:
        cfg = GnssConfig(
            systems=[
                GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=True),
                GnssSystemConfig(
                    constellation=GnssConstellation.GALILEO, enabled=False
                ),
            ]
        )
        connected_driver.configure_gnss(cfg)
        out = connected_driver.get_gnss_config()
        assert {s.constellation for s in out.systems if s.enabled} == {
            GnssConstellation.GPS
        }

    def test_default_rtcm_port_config_has_six_messages(
        self, connected_driver: FakeGpsDriver
    ) -> None:
        """Sanity check: default port config exposes the six common
        RTCM messages so the GPS-config UI has something to display."""
        cfg = connected_driver.get_rtcm_port_config()
        assert {1005, 1077, 1087, 1097, 1127, 1230} <= cfg.messages.keys()

    def test_default_gnss_has_all_six_constellations(
        self, connected_driver: FakeGpsDriver
    ) -> None:
        """Default GNSS config exposes every constellation in the model."""
        cfg = connected_driver.get_gnss_config()
        assert {s.constellation for s in cfg.systems} == set(GnssConstellation)

    def test_save_to_flash_is_noop_when_connected(
        self, connected_driver: FakeGpsDriver
    ) -> None:
        """save_to_flash returns without raising when connected."""
        connected_driver.save_to_flash()  # should not raise


# ---------------------------------------------------------------------------
# Position fixture
# ---------------------------------------------------------------------------


class TestGetPosition:
    """``get_position`` returns the May-26 bug fixture values."""

    def test_returns_fixture_lat_lon_alt(self, connected_driver: FakeGpsDriver) -> None:
        pos = connected_driver.get_position()
        assert pos.latitude == pytest.approx(32.7329015)
        assert pos.longitude == pytest.approx(-117.2362788)
        assert pos.altitude_m == pytest.approx(27.940)

    def test_returns_rtk_fixed_fix_type(self, connected_driver: FakeGpsDriver) -> None:
        pos = connected_driver.get_position()
        assert pos.fix_type is GpsFixType.FIX_3D
        assert pos.rtk_status == "fixed"

    def test_satellite_count_and_pdop_are_realistic(
        self, connected_driver: FakeGpsDriver
    ) -> None:
        pos = connected_driver.get_position()
        assert pos.num_satellites >= 20
        assert pos.pdop > 0.0
        assert pos.pdop < 2.0

    def test_timestamp_is_populated(self, connected_driver: FakeGpsDriver) -> None:
        pos = connected_driver.get_position()
        assert pos.timestamp is not None


# ---------------------------------------------------------------------------
# Device info & base config
# ---------------------------------------------------------------------------


class TestDeviceInfoAndBaseConfig:
    def test_get_device_info_matches_connect(
        self, connected_driver: FakeGpsDriver
    ) -> None:
        info = connected_driver.get_device_info()
        assert info.model == "FAKE-F9P"
        assert info.firmware_version == "FAKE 1.0"

    def test_base_config_starts_disabled(self, connected_driver: FakeGpsDriver) -> None:
        cfg = connected_driver.get_base_config()
        assert cfg.mode is BaseMode.DISABLED

    def test_configure_survey_in_switches_mode(
        self, connected_driver: FakeGpsDriver
    ) -> None:
        connected_driver.configure_survey_in(
            SurveyInConfig(min_duration_seconds=60, accuracy_limit_mm=50000)
        )
        cfg = connected_driver.get_base_config()
        assert cfg.mode is BaseMode.SURVEY_IN


# ---------------------------------------------------------------------------
# Survey-in clock
# ---------------------------------------------------------------------------


class TestSurveyInClock:
    """The fake survey-in clock auto-completes deterministically."""

    def test_idle_state_when_no_survey_started(
        self, connected_driver: FakeGpsDriver
    ) -> None:
        s = connected_driver.get_survey_in_status()
        assert s.active is False
        assert s.valid is False

    def test_in_progress_immediately_after_start(
        self, connected_driver: FakeGpsDriver
    ) -> None:
        # accuracy_limit_mm is constrained to >=1000 by pydantic; pick a
        # tight value the fast-complete path will surpass before the
        # threshold path does.
        connected_driver.configure_survey_in(
            SurveyInConfig(min_duration_seconds=60, accuracy_limit_mm=1000)
        )
        s = connected_driver.get_survey_in_status()
        assert s.active is True
        assert s.valid is False
        # Position is None during a running survey.
        assert s.latitude is None

    def test_accuracy_decreases_over_time(
        self, connected_driver: FakeGpsDriver
    ) -> None:
        """Polling twice should show a lower modelled accuracy."""
        connected_driver.configure_survey_in(
            SurveyInConfig(min_duration_seconds=60, accuracy_limit_mm=1000)
        )
        first = connected_driver.get_survey_in_status()
        time.sleep(0.05)
        second = connected_driver.get_survey_in_status()
        assert second.mean_accuracy_mm <= first.mean_accuracy_mm

    def test_auto_completes_at_fast_path_cap(
        self, connected_driver: FakeGpsDriver
    ) -> None:
        """If we pretend a long elapsed time has passed, survey completes.

        We patch ``time.monotonic`` rather than really sleeping
        ``_SURVEY_FAST_COMPLETE_SECONDS`` so the test stays fast.
        """
        connected_driver.configure_survey_in(
            SurveyInConfig(min_duration_seconds=60, accuracy_limit_mm=1000)
        )
        # Time-jump 10 s into the future.  Both ``configure_survey_in``
        # and ``get_survey_in_status`` read ``time.monotonic`` — we only
        # need to patch the second call.
        with patch(
            "sp_rtk_base.services.drivers.fake.time.monotonic",
            return_value=time.monotonic() + 10.0,
        ):
            s = connected_driver.get_survey_in_status()
        assert s.valid is True
        assert s.active is False
        assert s.latitude == pytest.approx(32.7329015)
        assert s.longitude == pytest.approx(-117.2362788)
        # Auto-promote also updates the base config.
        bc = connected_driver.get_base_config()
        assert bc.mode is BaseMode.FIXED
        assert bc.latitude == pytest.approx(32.7329015)


# ---------------------------------------------------------------------------
# Serial port discovery
# ---------------------------------------------------------------------------


class TestListSerialPorts:
    def test_returns_single_fake_port(self) -> None:
        ports = FakeGpsDriver.list_serial_ports()
        assert len(ports) == 1
        assert ports[0].port == FAKE_PORT_LABEL
        assert ports[0].is_gps is True
        assert "Fake GPS" in ports[0].description


# ---------------------------------------------------------------------------
# Env-gated registration
# ---------------------------------------------------------------------------


@pytest.fixture()
def reload_driver_registry() -> Iterator[None]:
    """Reload ``services.drivers`` so env-var changes take effect.

    The module performs its env-gated registration at import time, so
    we must reload it after mutating ``os.environ``.  We restore the
    original module state afterwards to keep test order irrelevant.
    """
    import sp_rtk_base.services.drivers as mod

    original_env = os.environ.get("SP_RTK_BASE_FAKE_GPS")
    yield
    if original_env is None:
        os.environ.pop("SP_RTK_BASE_FAKE_GPS", None)
    else:
        os.environ["SP_RTK_BASE_FAKE_GPS"] = original_env
    importlib.reload(mod)


class TestEnvGatedRegistration:
    """The fake driver only appears when ``SP_RTK_BASE_FAKE_GPS=1``."""

    def test_registered_when_env_var_set(self, reload_driver_registry: None) -> None:
        os.environ["SP_RTK_BASE_FAKE_GPS"] = "1"
        import sp_rtk_base.services.drivers as mod

        importlib.reload(mod)
        assert "fake" in mod.list_drivers()
        instance = mod.create_driver("fake")
        assert isinstance(instance, FakeGpsDriver)

    def test_absent_when_env_var_unset(self, reload_driver_registry: None) -> None:
        os.environ.pop("SP_RTK_BASE_FAKE_GPS", None)
        import sp_rtk_base.services.drivers as mod

        importlib.reload(mod)
        assert "fake" not in mod.list_drivers()
        with pytest.raises(ValueError, match="Unknown GPS driver"):
            mod.create_driver("fake")

    def test_absent_when_env_var_is_any_other_value(
        self, reload_driver_registry: None
    ) -> None:
        """Only the exact string ``"1"`` enables the fake driver."""
        os.environ["SP_RTK_BASE_FAKE_GPS"] = "true"
        import sp_rtk_base.services.drivers as mod

        importlib.reload(mod)
        assert "fake" not in mod.list_drivers()
