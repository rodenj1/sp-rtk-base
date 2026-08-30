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
    DynModel,
    GnssConfig,
    GnssConstellation,
    GnssSystemConfig,
    PortId,
    PortProtocolConfig,
    RtcmOutputPort,
    RtcmPortConfig,
    RtcmRowId,
    UbxProtocol,
)
from sp_rtk_base.models.device_models import (
    BaseMode as TmodeMode,
)
from sp_rtk_base.models.hardware_identity import (
    HARDWARE_ANY,
    HARDWARE_UNKNOWN,
    HardwareConfidence,
    identity_from_target,
)
from sp_rtk_base.models.profile_models import BaudConfig, PortProtocolSet, Profile
from sp_rtk_base.services.profile_store import ProfileStore
from sp_rtk_base.ui.pages.gps_config import (
    MATRIX_PORTS,
    REQUIRED_RTCM_ROW,
    FormExtras,
    apply_blocked_reason,
    build_apply_config,
    build_picker_entries,
    build_saved_profile,
    format_cell_diff,
    hw_extras_display,
    i2c_spi_advisory_rows,
    infer_data_link_ports,
    is_modified_from_profile,
    matrix_cell_on,
    profile_matrix_to_form_matrix,
    profile_to_form_extras,
    receiver_config_from_profile,
    resolve_gnss_display,
    resolve_identity,
    resolve_ports_display,
    resolve_save_hardware,
    row_slug,
    rtcm_config_to_matrix,
    save_as_enabled,
    suggest_profile_name,
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


def _full_profile(name: str = "full-profile") -> Profile:
    """A profile with every ``ReceiverConfig`` field populated — used to
    exercise pre-fill/comparison helpers that need more than the matrix."""
    return Profile.model_validate(
        {
            "name": name,
            "version": 1,
            "hardware": "ZED-F9P",
            "baud": {"uart1": 57600, "uart2": 115200},
            "meas_period_ms": 500,
            "constellations": ["gps", "glonass"],
            "ports": {
                "UART1": {"in": ["UBX", "NMEA", "RTCM3X"], "out": ["RTCM3X"]},
                "UART2": {"in": ["UBX", "RTCM3X"], "out": ["RTCM3X"]},
            },
            "data_link_port": ["UART1", "UART2"],
            "dyn_model": "stationary",
            "elevation_mask_deg": 15,
            "bds_b2_enabled": False,
            "spi_enabled": True,
            "rtcm_stream": {
                "matrix": {
                    "1005": {"UART1": True, "UART2": True},
                    "1074": {"UART1": True, "UART2": True},
                }
            },
        }
    )


class TestFormExtras:
    """Defaults mirror ``ReceiverConfig``'s own "leave untouched" semantics."""

    def test_defaults_are_all_none_except_meas_period(self) -> None:
        extras = FormExtras()
        assert extras.ports is None
        assert extras.constellations is None
        assert extras.baud is None
        assert extras.meas_period_ms == 1000
        assert extras.dyn_model is None
        assert extras.tmode_mode is None
        assert extras.elevation_mask_deg is None
        assert extras.bds_b2_enabled is None
        assert extras.spi_enabled is None


class TestBuildApplyConfigWithExtras:
    def test_extras_flow_into_the_config(self) -> None:
        matrix = rtcm_config_to_matrix(
            RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"UART1": 1}})
        )
        extras = FormExtras(
            baud=BaudConfig(uart1=57600),
            meas_period_ms=200,
            constellations=[GnssConstellation.GPS],
            dyn_model=DynModel.STATIONARY,
            tmode_mode=TmodeMode.DISABLED,
            elevation_mask_deg=15,
            bds_b2_enabled=False,
            spi_enabled=True,
        )
        config = build_apply_config(matrix, [PortId.UART1], extras)
        assert config.baud is not None and config.baud.uart1 == 57600
        assert config.meas_period_ms == 200
        assert config.constellations == [GnssConstellation.GPS]
        assert config.dyn_model == DynModel.STATIONARY
        assert config.tmode_mode == TmodeMode.DISABLED
        assert config.elevation_mask_deg == 15
        assert config.bds_b2_enabled is False
        assert config.spi_enabled is True


class TestProfileMatrixToFormMatrix:
    def test_covers_every_catalog_row_and_matrix_port(self) -> None:
        matrix = profile_matrix_to_form_matrix(_full_profile())
        assert set(matrix.keys()) == set(RtcmRowId)
        for row in matrix.values():
            assert set(row.keys()) == set(MATRIX_PORTS)

    def test_reflects_the_profiles_sparse_matrix(self) -> None:
        matrix = profile_matrix_to_form_matrix(_full_profile())
        assert matrix[RtcmRowId.RTCM_1005][PortId.UART1] is True
        assert matrix[RtcmRowId.RTCM_1005][PortId.USB] is False
        assert matrix[RtcmRowId.RTCM_1077][PortId.UART1] is False


class TestProfileToFormExtras:
    def test_copies_every_field(self) -> None:
        extras = profile_to_form_extras(_full_profile())
        assert extras.ports is not None
        assert extras.constellations == [
            GnssConstellation.GPS,
            GnssConstellation.GLONASS,
        ]
        assert extras.baud == BaudConfig(uart1=57600, uart2=115200)
        assert extras.meas_period_ms == 500
        assert extras.dyn_model == DynModel.STATIONARY
        assert extras.tmode_mode is None
        assert extras.elevation_mask_deg == 15
        assert extras.bds_b2_enabled is False
        assert extras.spi_enabled is True


class TestReceiverConfigFromProfile:
    def test_strips_identity_fields(self) -> None:
        config = receiver_config_from_profile(_full_profile())
        assert not hasattr(config, "name")
        assert not hasattr(config, "hardware")
        assert not hasattr(config, "forked_from")
        assert config.meas_period_ms == 500

    def test_equals_the_form_built_from_the_same_profile(self) -> None:
        profile = _full_profile()
        matrix = profile_matrix_to_form_matrix(profile)
        extras = profile_to_form_extras(profile)
        form_config = build_apply_config(matrix, profile.data_link_port, extras)
        assert form_config == receiver_config_from_profile(profile)


class TestIsModifiedFromProfile:
    def test_false_when_no_profile_selected(self) -> None:
        config = build_apply_config(
            rtcm_config_to_matrix(
                RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"UART1": 1}})
            ),
            [PortId.UART1],
        )
        assert not is_modified_from_profile(config, None)

    def test_false_when_form_exactly_equals_the_profile(self) -> None:
        profile = _full_profile()
        matrix = profile_matrix_to_form_matrix(profile)
        extras = profile_to_form_extras(profile)
        form_config = build_apply_config(matrix, profile.data_link_port, extras)
        assert not is_modified_from_profile(form_config, profile)

    def test_true_once_the_form_diverges(self) -> None:
        profile = _full_profile()
        matrix = profile_matrix_to_form_matrix(profile)
        matrix[RtcmRowId.RTCM_1074][PortId.UART1] = False
        extras = profile_to_form_extras(profile)
        form_config = build_apply_config(matrix, profile.data_link_port, extras)
        assert is_modified_from_profile(form_config, profile)


class TestSaveAsEnabled:
    def test_disabled_when_form_is_invalid(self) -> None:
        assert not save_as_enabled(None, None)

    def test_enabled_with_no_profile_selected(self) -> None:
        config = build_apply_config(
            rtcm_config_to_matrix(
                RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"UART1": 1}})
            ),
            [PortId.UART1],
        )
        assert save_as_enabled(config, None)

    def test_suppressed_when_selected_profile_exactly_equals_the_form(self) -> None:
        profile = _full_profile()
        matrix = profile_matrix_to_form_matrix(profile)
        extras = profile_to_form_extras(profile)
        form_config = build_apply_config(matrix, profile.data_link_port, extras)
        assert not save_as_enabled(form_config, profile)

    def test_enabled_again_once_the_form_diverges_from_the_selected_profile(
        self,
    ) -> None:
        """The #66 fix: applying edits must not take Save-as away again."""
        profile = _full_profile()
        matrix = profile_matrix_to_form_matrix(profile)
        matrix[RtcmRowId.RTCM_1074][PortId.UART2] = False
        extras = profile_to_form_extras(profile)
        form_config = build_apply_config(matrix, profile.data_link_port, extras)
        assert save_as_enabled(form_config, profile)


class TestSuggestProfileName:
    def test_forked_name_is_slug_safe(self) -> None:
        name = suggest_profile_name(_full_profile("ublox-f9p-base-standard"), "ZED-F9P")
        assert name == "ublox-f9p-base-standard-copy"
        assert " " not in name and "(" not in name

    def test_bare_capture_derives_from_hardware_target(self) -> None:
        assert suggest_profile_name(None, "ZED-F9P") == "zed-f9p-captured"


class TestResolveSaveHardware:
    def test_fork_keeps_the_source_profiles_hardware(self) -> None:
        profile = _full_profile()
        identity = identity_from_target("ZED-F9R", HardwareConfidence.CONFIRMED)
        assert resolve_save_hardware(profile, identity) == "ZED-F9P"

    def test_bare_capture_with_confirmed_identity_uses_the_target(self) -> None:
        identity = identity_from_target("ZED-F9P", HardwareConfidence.CONFIRMED)
        assert resolve_save_hardware(None, identity) == "ZED-F9P"

    def test_bare_capture_with_a_family_target_uses_the_family(self) -> None:
        identity = identity_from_target("gen9", HardwareConfidence.INFERRED)
        assert resolve_save_hardware(None, identity) == "gen9"

    def test_unknown_identity_falls_back_to_any(self) -> None:
        identity = identity_from_target(HARDWARE_UNKNOWN, HardwareConfidence.UNKNOWN)
        assert resolve_save_hardware(None, identity) == HARDWARE_ANY

    def test_inferred_specific_model_falls_back_to_any(self) -> None:
        """An inferred guess of a *specific* model can't be trusted as a save
        tag — only a family token is safe to fall back to."""
        identity = identity_from_target("ZED-F9P", HardwareConfidence.INFERRED)
        assert resolve_save_hardware(None, identity) == HARDWARE_ANY


class TestBuildSavedProfile:
    def test_builds_a_valid_profile_with_identity_fields(self) -> None:
        config = build_apply_config(
            rtcm_config_to_matrix(
                RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"UART1": 1}})
            ),
            [PortId.UART1],
        )
        profile = build_saved_profile("my-base", config, "ZED-F9P", "source-profile")
        assert profile.name == "my-base"
        assert profile.version == 1
        assert profile.hardware == "ZED-F9P"
        assert profile.forked_from == "source-profile"
        assert profile.data_link_port == [PortId.UART1]

    def test_bare_capture_has_no_forked_from(self) -> None:
        config = build_apply_config(
            rtcm_config_to_matrix(
                RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"UART1": 1}})
            ),
            [PortId.UART1],
        )
        profile = build_saved_profile("captured", config, "ZED-F9P", None)
        assert profile.forked_from is None


class TestResolvePortsDisplay:
    def test_falls_back_to_live_when_no_form_ports(self) -> None:
        live = PortProtocolConfig(
            in_protocols={PortId.UART1: [UbxProtocol.UBX]},
            out_protocols={PortId.UART1: [UbxProtocol.RTCM3X]},
        )
        display = resolve_ports_display(live, None)
        assert display[PortId.UART1] == (["UBX"], ["RTCM3X"])
        assert display[PortId.UART2] == ([], [])

    def test_form_ports_take_priority_over_live(self) -> None:
        live = PortProtocolConfig(
            in_protocols={PortId.UART1: [UbxProtocol.UBX]},
        )
        form_ports = {
            PortId.UART1: PortProtocolSet(
                **{"in": [UbxProtocol.UBX, UbxProtocol.NMEA], "out": []}
            ),
        }
        display = resolve_ports_display(live, form_ports)
        assert display[PortId.UART1] == (["UBX", "NMEA"], [])
        # A port omitted from the profile's ports is untouched -> empty.
        assert display[PortId.UART2] == ([], [])


class TestResolveGnssDisplay:
    def test_falls_back_to_live_when_no_form_constellations(self) -> None:
        live = GnssConfig(
            systems=[
                GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=True),
                GnssSystemConfig(constellation=GnssConstellation.SBAS, enabled=False),
            ]
        )
        display = resolve_gnss_display(live, None)
        assert display["gps"] is True
        assert display["sbas"] is False

    def test_form_constellations_take_priority_over_live(self) -> None:
        live = GnssConfig(
            systems=[
                GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=False),
            ]
        )
        display = resolve_gnss_display(live, [GnssConstellation.GLONASS])
        assert display["gps"] is False
        assert display["glonass"] is True


class TestHwExtrasDisplay:
    def test_default_extras_show_unchanged(self) -> None:
        rows = {label: value for _cls, label, value in hw_extras_display(FormExtras())}
        assert rows["Baud"] == "unchanged"
        assert rows["Dynamics Model"] == "unchanged"
        assert rows["BeiDou B2"] == "unchanged"
        assert rows["Measurement Rate"] == "1 Hz"

    def test_populated_extras_render_their_values(self) -> None:
        extras = FormExtras(
            baud=BaudConfig(uart1=57600),
            meas_period_ms=500,
            dyn_model=DynModel.STATIONARY,
            tmode_mode=TmodeMode.FIXED,
            elevation_mask_deg=15,
            bds_b2_enabled=False,
            spi_enabled=True,
        )
        rows = {label: value for _cls, label, value in hw_extras_display(extras)}
        assert rows["Baud"] == "UART1=57600"
        assert rows["Measurement Rate"] == "2 Hz"
        assert rows["Dynamics Model"] == "stationary"
        assert rows["Time Mode"] == "fixed"
        assert rows["Elevation Mask"] == "15°"
        assert rows["BeiDou B2"] == "off"
        assert rows["SPI"] == "on"
