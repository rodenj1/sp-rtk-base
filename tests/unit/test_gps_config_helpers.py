"""Tests for the pure helpers used by the Advanced GPS Config page (issue #64).

The page-rendering closure itself drives NiceGUI elements and can't be
meaningfully unit-tested without a full browser harness (that's what
tests/e2e covers). These helpers are pure functions extracted from
``ui/pages/gps_config.py`` to make the profile-picker tagging and
RTCM-matrix/advisory logic verifiable in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sp_rtk_base.models.device_models import (
    ApplyConfigCellDiff,
    PortId,
    RtcmOutputPort,
    RtcmPortConfig,
    RtcmRowId,
)
from sp_rtk_base.models.hardware_identity import (
    HARDWARE_ANY,
    HARDWARE_UNKNOWN,
    HardwareConfidence,
    identity_from_target,
)
from sp_rtk_base.models.profile_models import Profile
from sp_rtk_base.services.profile_store import ProfileStore
from sp_rtk_base.ui.pages.gps_config import (
    MATRIX_PORTS,
    REQUIRED_RTCM_ROW,
    apply_blocked_reason,
    build_apply_config,
    build_picker_entries,
    format_cell_diff,
    i2c_spi_advisory_rows,
    infer_data_link_ports,
    matrix_cell_on,
    resolve_identity,
    row_slug,
    rtcm_config_to_matrix,
)


def _matrix_ok() -> dict[RtcmRowId, dict[PortId, bool]]:
    return {RtcmRowId.RTCM_1005: {PortId.UART1: True}}


def _profile(name: str, hardware: str) -> Profile:
    return Profile.model_validate(
        {
            "name": name,
            "version": 1,
            "hardware": hardware,
            "data_link_port": [PortId.UART1],
            "rtcm_stream": {"matrix": _matrix_ok()},
        }
    )


class TestResolveIdentity:
    def test_none_fields_resolve_to_unknown(self) -> None:
        identity = resolve_identity(None, None)
        assert identity.target == HARDWARE_UNKNOWN
        assert identity.confidence == HardwareConfidence.UNKNOWN

    def test_confirmed_target_passes_through(self) -> None:
        identity = resolve_identity("ZED-F9P", HardwareConfidence.CONFIRMED)
        assert identity.target == "ZED-F9P"
        assert identity.confidence == HardwareConfidence.CONFIRMED
        assert identity.is_specific_model


class TestBuildPickerEntries:
    """The one-time-required 1005 row is REQUIRED_RTCM_ROW — sanity check."""

    def test_required_row_is_1005(self) -> None:
        assert REQUIRED_RTCM_ROW == RtcmRowId.RTCM_1005

    def test_builtin_before_custom(self, tmp_path: Path) -> None:
        store = ProfileStore(profiles_dir=tmp_path)
        custom = _profile("zzz-custom", HARDWARE_ANY)
        store.create_profile(custom)
        profiles = store.list_profiles()  # built-in first, alphabetical

        identity = identity_from_target(HARDWARE_UNKNOWN, HardwareConfidence.UNKNOWN)
        entries = build_picker_entries(profiles, store, identity)

        assert [e.profile.name for e in entries] == [p.name for p in profiles]
        assert entries[0].is_builtin
        assert not entries[-1].is_builtin

    def test_confirmed_matching_device_is_compatible_and_default(
        self, tmp_path: Path
    ) -> None:
        store = ProfileStore(profiles_dir=tmp_path)
        profiles = store.list_profiles()
        identity = identity_from_target("ZED-F9P", HardwareConfidence.CONFIRMED)

        entries = build_picker_entries(profiles, store, identity)
        builtin_entry = next(
            e for e in entries if e.profile.name == "ublox-f9p-base-standard"
        )
        assert builtin_entry.compatible
        assert builtin_entry.incompatible_reason is None
        assert builtin_entry.is_default

    def test_mismatched_specific_model_is_incompatible_with_reason(
        self, tmp_path: Path
    ) -> None:
        store = ProfileStore(profiles_dir=tmp_path)
        custom = _profile("f9r-only", "ZED-F9R")
        store.create_profile(custom)
        profiles = store.list_profiles()
        identity = identity_from_target("ZED-F9P", HardwareConfidence.CONFIRMED)

        entries = build_picker_entries(profiles, store, identity)
        f9r_entry = next(e for e in entries if e.profile.name == "f9r-only")

        assert not f9r_entry.compatible
        assert f9r_entry.incompatible_reason == "not for this hardware (ZED-F9P)"
        assert not f9r_entry.is_default

    def test_unconfirmed_identity_has_no_default(self, tmp_path: Path) -> None:
        store = ProfileStore(profiles_dir=tmp_path)
        profiles = store.list_profiles()
        identity = identity_from_target("ZED-F9P", HardwareConfidence.INFERRED)

        entries = build_picker_entries(profiles, store, identity)

        assert not any(e.is_default for e in entries)
        builtin_entry = next(
            e for e in entries if e.profile.name == "ublox-f9p-base-standard"
        )
        assert not builtin_entry.compatible
        assert builtin_entry.incompatible_reason == (
            "receiver hardware is unconfirmed — only family- or any-tagged "
            "profiles are enabled"
        )


class TestMatrixCellOn:
    def test_on_when_rate_positive(self) -> None:
        rtcm = RtcmPortConfig(
            messages={RtcmRowId.RTCM_1005: {"UART1": 1, "UART2": 0, "USB": 0}}
        )
        assert matrix_cell_on(rtcm, RtcmRowId.RTCM_1005, PortId.UART1)
        assert not matrix_cell_on(rtcm, RtcmRowId.RTCM_1005, PortId.UART2)

    def test_missing_row_is_off(self) -> None:
        rtcm = RtcmPortConfig(messages={})
        assert not matrix_cell_on(rtcm, RtcmRowId.RTCM_1077, PortId.USB)


class TestRowSlug:
    def test_plain_row_id_is_unchanged(self) -> None:
        assert row_slug(RtcmRowId.RTCM_1005) == "1005"

    def test_dotted_row_id_becomes_css_safe(self) -> None:
        assert row_slug(RtcmRowId.RTCM_4072_0) == "4072_0"
        assert row_slug(RtcmRowId.RTCM_4072_1) == "4072_1"


class TestI2cSpiAdvisoryRows:
    def test_empty_when_only_matrix_ports_enabled(self) -> None:
        rtcm = RtcmPortConfig(
            messages={
                RtcmRowId.RTCM_1005: {
                    "UART1": 1,
                    "UART2": 0,
                    "USB": 0,
                    "I2C": 0,
                    "SPI": 0,
                }
            }
        )
        assert i2c_spi_advisory_rows(rtcm) == []

    @pytest.mark.parametrize("advisory_port", [RtcmOutputPort.I2C, RtcmOutputPort.SPI])
    def test_nonzero_cell_on_advisory_port_is_flagged(
        self, advisory_port: RtcmOutputPort
    ) -> None:
        rtcm = RtcmPortConfig(
            messages={RtcmRowId.RTCM_1077: {"UART1": 0, advisory_port.value: 1}}
        )
        assert i2c_spi_advisory_rows(rtcm) == [RtcmRowId.RTCM_1077]

    def test_multiple_flagged_rows_preserve_catalog_order(self) -> None:
        rtcm = RtcmPortConfig(
            messages={
                RtcmRowId.RTCM_1097: {"I2C": 1},
                RtcmRowId.RTCM_1005: {"SPI": 1},
            }
        )
        assert i2c_spi_advisory_rows(rtcm) == [RtcmRowId.RTCM_1005, RtcmRowId.RTCM_1097]


class TestRtcmConfigToMatrix:
    """Seeds the editable boolean matrix from a live ``RtcmPortConfig`` read-back."""

    def test_covers_every_catalog_row_and_matrix_port(self) -> None:
        matrix = rtcm_config_to_matrix(RtcmPortConfig(messages={}))
        assert set(matrix.keys()) == {
            RtcmRowId.RTCM_1005,
            RtcmRowId.RTCM_1077,
            RtcmRowId.RTCM_1087,
            RtcmRowId.RTCM_1097,
            RtcmRowId.RTCM_1127,
            RtcmRowId.RTCM_1230,
            RtcmRowId.RTCM_1074,
            RtcmRowId.RTCM_1084,
            RtcmRowId.RTCM_1094,
            RtcmRowId.RTCM_1124,
            RtcmRowId.RTCM_4072_0,
            RtcmRowId.RTCM_4072_1,
        }
        for row in matrix.values():
            assert set(row.keys()) == set(MATRIX_PORTS)

    def test_reflects_live_enabled_cells(self) -> None:
        rtcm = RtcmPortConfig(
            messages={RtcmRowId.RTCM_1005: {"UART1": 1, "UART2": 0, "USB": 1}}
        )
        matrix = rtcm_config_to_matrix(rtcm)
        assert matrix[RtcmRowId.RTCM_1005][PortId.UART1] is True
        assert matrix[RtcmRowId.RTCM_1005][PortId.UART2] is False
        assert matrix[RtcmRowId.RTCM_1005][PortId.USB] is True
        assert matrix[RtcmRowId.RTCM_1077][PortId.UART1] is False


class TestInferDataLinkPorts:
    """The spec-mandated inference: a UART carrying any RTCM row qualifies."""

    def test_empty_matrix_infers_nothing(self) -> None:
        matrix = rtcm_config_to_matrix(RtcmPortConfig(messages={}))
        assert infer_data_link_ports(matrix) == []

    def test_uart_with_a_row_on_is_inferred(self) -> None:
        matrix = rtcm_config_to_matrix(
            RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"UART1": 1}})
        )
        assert infer_data_link_ports(matrix) == [PortId.UART1]

    def test_usb_only_traffic_infers_nothing(self) -> None:
        """USB can never be a data-link port — it's excluded even if it carries RTCM."""
        matrix = rtcm_config_to_matrix(
            RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"USB": 1}})
        )
        assert infer_data_link_ports(matrix) == []

    def test_both_uarts_inferred_in_enum_order(self) -> None:
        matrix = rtcm_config_to_matrix(
            RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"UART1": 1, "UART2": 1}})
        )
        assert infer_data_link_ports(matrix) == [PortId.UART1, PortId.UART2]


class TestApplyBlockedReason:
    def test_no_data_link_port_blocks_with_a_reason(self) -> None:
        reason = apply_blocked_reason([])
        assert reason is not None
        assert "data-link port" in reason

    def test_a_chosen_data_link_port_unblocks(self) -> None:
        assert apply_blocked_reason([PortId.UART1]) is None


class TestBuildApplyConfig:
    def test_builds_a_valid_receiver_config(self) -> None:
        matrix = rtcm_config_to_matrix(
            RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"UART1": 1}})
        )
        config = build_apply_config(matrix, [PortId.UART1])
        assert config.data_link_port == [PortId.UART1]
        assert config.rtcm_stream.matrix[RtcmRowId.RTCM_1005][PortId.UART1] is True
        # Fields this page doesn't make editable are left untouched.
        assert config.ports is None
        assert config.constellations is None
        assert config.dyn_model is None
        assert config.tmode_mode is None
        assert config.baud is None

    def test_raises_when_1005_missing_from_every_data_link_port(self) -> None:
        matrix = rtcm_config_to_matrix(RtcmPortConfig(messages={}))
        with pytest.raises(ValueError, match="1005"):
            build_apply_config(matrix, [PortId.UART1])


class TestFormatCellDiff:
    def test_names_row_port_and_the_mismatch(self) -> None:
        diff = ApplyConfigCellDiff(
            row_id=RtcmRowId.RTCM_1005,
            port=PortId.UART1,
            expected=True,
            actual=False,
        )
        text = format_cell_diff(diff)
        assert "1005" in text
        assert "UART1" in text
        assert "on" in text
        assert "off" in text
