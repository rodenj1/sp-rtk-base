"""Tests for the shipped built-in GPS receiver profiles."""

from __future__ import annotations

from sp_rtk_base.models.device_models import GnssConstellation, PortId, RtcmRowId
from sp_rtk_base.models.profile_models import DynModel
from sp_rtk_base.profiles import BUILTIN_PROFILES

_ON_ROWS = {
    RtcmRowId.RTCM_1005,
    RtcmRowId.RTCM_4072_0,
    RtcmRowId.RTCM_4072_1,
    RtcmRowId.RTCM_1074,
    RtcmRowId.RTCM_1084,
    RtcmRowId.RTCM_1094,
    RtcmRowId.RTCM_1124,
    RtcmRowId.RTCM_1230,
}
_OFF_ROWS = {
    RtcmRowId.RTCM_1077,
    RtcmRowId.RTCM_1087,
    RtcmRowId.RTCM_1097,
    RtcmRowId.RTCM_1127,
}


class TestBuiltinCatalog:
    def test_imports_cleanly_with_exactly_one_builtin(self) -> None:
        assert list(BUILTIN_PROFILES) == ["ublox-f9p-base-standard"]


class TestUbloxF9pBaseStandard:
    def setup_method(self) -> None:
        self.profile = BUILTIN_PROFILES["ublox-f9p-base-standard"]

    def test_identity(self) -> None:
        assert self.profile.name == "ublox-f9p-base-standard"
        assert self.profile.version == 1
        assert self.profile.hardware == "ZED-F9P"

    def test_display_name(self) -> None:
        assert self.profile.display_name == "u-blox F9P — Base Station (Standard)"

    def test_baud_split(self) -> None:
        assert self.profile.baud is not None
        assert self.profile.baud.uart1 == 57600
        assert self.profile.baud.uart2 == 115200

    def test_port_protocols(self) -> None:
        assert self.profile.ports is not None
        uart1 = self.profile.ports[PortId.UART1]
        assert {p.value for p in uart1.in_} == {"UBX", "NMEA", "RTCM3X"}
        assert {p.value for p in uart1.out} == {"RTCM3X"}

        uart2 = self.profile.ports[PortId.UART2]
        assert {p.value for p in uart2.in_} == {"UBX", "RTCM3X"}
        assert {p.value for p in uart2.out} == {"RTCM3X"}

    def test_usb_omitted_from_ports(self) -> None:
        assert self.profile.ports is not None
        assert PortId.USB not in self.profile.ports

    def test_all_constellations_enabled(self) -> None:
        assert self.profile.constellations is not None
        assert set(self.profile.constellations) == set(GnssConstellation)

    def test_meas_period_ms_is_1000(self) -> None:
        assert self.profile.meas_period_ms == 1000

    def test_role_fields(self) -> None:
        assert self.profile.dyn_model == DynModel.STATIONARY
        assert self.profile.tmode_mode is None

    def test_optimisations(self) -> None:
        assert self.profile.elevation_mask_deg == 15
        assert self.profile.bds_b2_enabled is False
        assert self.profile.spi_enabled is True

    def test_data_link_port(self) -> None:
        assert self.profile.data_link_port == [PortId.UART1, PortId.UART2]

    def test_matrix_on_rows_on_both_uarts_off_usb(self) -> None:
        matrix = self.profile.rtcm_stream.matrix
        for row in _ON_ROWS:
            assert matrix[row][PortId.UART1] is True, row
            assert matrix[row][PortId.UART2] is True, row
            assert matrix[row][PortId.USB] is False, row

    def test_matrix_msm7_off(self) -> None:
        matrix = self.profile.rtcm_stream.matrix
        for row in _OFF_ROWS:
            assert matrix[row][PortId.UART1] is False, row
            assert matrix[row][PortId.UART2] is False, row
            assert matrix[row][PortId.USB] is False, row

    def test_entire_usb_column_off(self) -> None:
        matrix = self.profile.rtcm_stream.matrix
        assert all(not ports.get(PortId.USB, False) for ports in matrix.values())

    def test_matrix_covers_all_twelve_rows(self) -> None:
        assert set(self.profile.rtcm_stream.matrix) == set(RtcmRowId)
