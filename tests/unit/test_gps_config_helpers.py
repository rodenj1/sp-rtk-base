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
    BaseMode as TmodeMode,
)
from sp_rtk_base.models.device_models import (
    CurrentBaseConfig,
    DynModel,
    GnssConstellation,
    PortId,
    RtcmOutputPort,
    RtcmPortConfig,
    RtcmRowId,
    SurveyInProgress,
)
from sp_rtk_base.models.hardware_identity import (
    HARDWARE_ANY,
    HARDWARE_UNKNOWN,
    HardwareConfidence,
    identity_from_target,
)
from sp_rtk_base.models.profile_models import (
    ApplyDiffEntry,
    BaudAssertion,
    PortProtocolSet,
    Profile,
    ReceiverApplyRequest,
    ReceiverAssertion,
    RtcmStreamConfig,
    merge_profile_into_assertion,
)
from sp_rtk_base.services.profile_store import ProfileStore
from sp_rtk_base.ui.pages.gps_config import (
    MATRIX_PORTS,
    REQUIRED_RTCM_ROW,
    apply_blocked_reason,
    build_apply_request,
    build_picker_entries,
    build_saved_profile,
    display_label,
    fixed_position_step_state,
    format_leaf_diff,
    hw_extras_display,
    i2c_spi_advisory_rows,
    infer_data_link_ports,
    is_modified_from_profile,
    matrix_cell_on,
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


def _minimal_form() -> ReceiverAssertion:
    """A minimal, fully-populated form — used wherever a test only cares
    about the matrix + data-link ports and doesn't need the rest of the
    hardware section populated meaningfully (issue #98: ``build_apply_request``
    always takes the whole form, unlike the old matrix-only default)."""
    return ReceiverAssertion(
        baud=BaudAssertion(uart1=57600, uart2=115200),
        meas_period_ms=1000,
        constellations=[],
        ports={},
        dyn_model=DynModel.PORTABLE,
        tmode_mode=TmodeMode.DISABLED,
        elevation_mask_deg=0,
        bds_b2_enabled=False,
        spi_enabled=False,
        rtcm_stream=RtcmStreamConfig(
            matrix=rtcm_config_to_matrix(
                RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"UART1": 1}})
            )
        ),
    )


class TestBuildApplyRequest:
    def test_builds_a_valid_request(self) -> None:
        form = _minimal_form()
        request = build_apply_request(form, [PortId.UART1])
        assert request.data_link_port == [PortId.UART1]
        assert request.assertion == form

    def test_raises_when_1005_missing_from_every_data_link_port(self) -> None:
        form = _minimal_form().model_copy(
            update={
                "rtcm_stream": RtcmStreamConfig(
                    matrix=rtcm_config_to_matrix(RtcmPortConfig(messages={}))
                )
            }
        )
        with pytest.raises(ValueError, match="1005"):
            build_apply_request(form, [PortId.UART1])


class TestFormatLeafDiff:
    def test_names_the_path_and_the_mismatch(self) -> None:
        diff = ApplyDiffEntry(path="rtcm.1005.UART1", expected=True, actual=False)
        text = format_leaf_diff(diff)
        assert "rtcm.1005.UART1" in text
        assert "on" in text
        assert "off" in text

    def test_non_bool_values_render_as_themselves(self) -> None:
        diff = ApplyDiffEntry(path="meas_period_ms", expected=1000, actual=333)
        text = format_leaf_diff(diff)
        assert "1000" in text
        assert "333" in text


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


def _live_assertion() -> ReceiverAssertion:
    """A live-seeded assertion with values distinct from ``_full_profile()``'s
    — used to prove a merge/comparison falls back to *live* wherever the
    profile omits a field, rather than to a schema default."""
    return ReceiverAssertion(
        baud=BaudAssertion(uart1=9600, uart2=9600),
        meas_period_ms=2000,
        constellations=[GnssConstellation.BEIDOU],
        ports={
            PortId.UART1: PortProtocolSet(**{"in": ["UBX"], "out": []}),
            PortId.UART2: PortProtocolSet(**{"in": ["UBX"], "out": []}),
            PortId.USB: PortProtocolSet(
                **{"in": ["UBX", "NMEA"], "out": ["UBX", "NMEA"]}
            ),
        },
        dyn_model=DynModel.PORTABLE,
        tmode_mode=TmodeMode.SURVEY_IN,
        elevation_mask_deg=5,
        bds_b2_enabled=True,
        spi_enabled=False,
        rtcm_stream=RtcmStreamConfig(matrix={}),
    )


class TestReceiverConfigFromProfile:
    def test_returns_the_apply_envelope(self) -> None:
        request = receiver_config_from_profile(_full_profile(), _live_assertion())
        assert isinstance(request, ReceiverApplyRequest)
        assert request.assertion.meas_period_ms == 500
        assert request.data_link_port == [PortId.UART1, PortId.UART2]

    def test_falls_back_to_live_for_fields_the_profile_omits(self) -> None:
        """``_full_profile()`` omits ``tmode_mode`` and USB ports on purpose."""
        live = _live_assertion()
        request = receiver_config_from_profile(_full_profile(), live)
        assert request.assertion.tmode_mode == live.tmode_mode
        assert request.assertion.ports[PortId.USB] == live.ports[PortId.USB]

    def test_equals_the_form_built_from_the_same_profile(self) -> None:
        profile = _full_profile()
        live = _live_assertion()
        merged = merge_profile_into_assertion(profile, live)
        form_request = build_apply_request(merged, list(profile.data_link_port))
        assert form_request == receiver_config_from_profile(profile, live)


class TestIsModifiedFromProfile:
    def test_false_when_no_profile_selected(self) -> None:
        request = build_apply_request(_minimal_form(), [PortId.UART1])
        assert not is_modified_from_profile(request, None, _live_assertion())

    def test_false_when_form_exactly_equals_the_profile(self) -> None:
        profile = _full_profile()
        live = _live_assertion()
        form_request = receiver_config_from_profile(profile, live)
        assert not is_modified_from_profile(form_request, profile, live)

    def test_true_once_the_form_diverges(self) -> None:
        profile = _full_profile()
        live = _live_assertion()
        form_request = receiver_config_from_profile(profile, live)
        diverged = form_request.model_copy(
            update={
                "assertion": form_request.assertion.model_copy(
                    update={"meas_period_ms": 999}
                )
            }
        )
        assert is_modified_from_profile(diverged, profile, live)


class TestSaveAsEnabled:
    def test_disabled_when_form_is_invalid(self) -> None:
        assert not save_as_enabled(None, None, _live_assertion())

    def test_enabled_with_no_profile_selected(self) -> None:
        request = build_apply_request(_minimal_form(), [PortId.UART1])
        assert save_as_enabled(request, None, _live_assertion())

    def test_suppressed_when_selected_profile_exactly_equals_the_form(self) -> None:
        profile = _full_profile()
        live = _live_assertion()
        form_request = receiver_config_from_profile(profile, live)
        assert not save_as_enabled(form_request, profile, live)

    def test_enabled_again_once_the_form_diverges_from_the_selected_profile(
        self,
    ) -> None:
        """The #66 fix: applying edits must not take Save-as away again."""
        profile = _full_profile()
        live = _live_assertion()
        form_request = receiver_config_from_profile(profile, live)
        diverged = form_request.model_copy(
            update={
                "assertion": form_request.assertion.model_copy(
                    update={"meas_period_ms": 999}
                )
            }
        )
        assert save_as_enabled(diverged, profile, live)


class TestDisplayLabel:
    def test_renders_display_name_when_set(self) -> None:
        profile = _full_profile("some-slug").model_copy(
            update={"display_name": "Pretty Name"}
        )
        assert display_label(profile) == "Pretty Name"

    def test_falls_back_to_slug_when_absent(self) -> None:
        profile = _full_profile("some-slug")
        assert profile.display_name is None
        assert display_label(profile) == "some-slug"


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
        request = build_apply_request(_minimal_form(), [PortId.UART1])
        profile = build_saved_profile("my-base", request, "ZED-F9P", "source-profile")
        assert profile.name == "my-base"
        assert profile.version == 1
        assert profile.hardware == "ZED-F9P"
        assert profile.forked_from == "source-profile"
        assert profile.data_link_port == [PortId.UART1]

    def test_bare_capture_has_no_forked_from(self) -> None:
        request = build_apply_request(_minimal_form(), [PortId.UART1])
        profile = build_saved_profile("captured", request, "ZED-F9P", None)
        assert profile.forked_from is None


class TestResolvePortsDisplay:
    def test_renders_the_forms_ports(self) -> None:
        ports = {
            PortId.UART1: PortProtocolSet(**{"in": ["UBX"], "out": ["RTCM3X"]}),
        }
        display = resolve_ports_display(ports)
        assert display[PortId.UART1] == (["UBX"], ["RTCM3X"])
        # A port absent from the map renders empty.
        assert display[PortId.UART2] == ([], [])


class TestResolveGnssDisplay:
    def test_renders_the_forms_constellations(self) -> None:
        display = resolve_gnss_display([GnssConstellation.GPS])
        assert display["gps"] is True
        assert display["sbas"] is False

    def test_empty_constellations_shows_nothing_enabled(self) -> None:
        display = resolve_gnss_display([])
        assert not any(display.values())


class TestHwExtrasDisplay:
    def test_renders_every_field_concretely(self) -> None:
        extras = ReceiverAssertion(
            baud=BaudAssertion(uart1=57600, uart2=115200),
            meas_period_ms=500,
            constellations=[],
            ports={},
            dyn_model=DynModel.STATIONARY,
            tmode_mode=TmodeMode.FIXED,
            elevation_mask_deg=15,
            bds_b2_enabled=False,
            spi_enabled=True,
            rtcm_stream=RtcmStreamConfig(matrix={}),
        )
        rows = {label: value for _cls, label, value in hw_extras_display(extras)}
        assert rows["Baud"] == "UART1=57600, UART2=115200"
        assert rows["Measurement Rate"] == "2 Hz"
        assert rows["Dynamics Model"] == "stationary"
        assert rows["Time Mode"] == "fixed"
        assert rows["Elevation Mask"] == "15°"
        assert rows["BeiDou B2"] == "off"
        assert rows["SPI"] == "on"


class TestFixedPositionStepState:
    """Step derivation for the Fixed Position three-step card (issue #96)."""

    def test_disabled_mode_and_no_survey_is_step_1(self) -> None:
        state = fixed_position_step_state(
            CurrentBaseConfig(mode=TmodeMode.DISABLED),
            SurveyInProgress(active=False, valid=False),
        )
        assert state.current_step == 1
        assert state.survey_state_text == "— not started"
        assert state.fixed_pos_text == "— none"

    def test_survey_in_mode_is_step_2(self) -> None:
        state = fixed_position_step_state(
            CurrentBaseConfig(mode=TmodeMode.SURVEY_IN),
            SurveyInProgress(
                active=True,
                valid=False,
                duration_seconds=42,
                mean_accuracy_mm=1234.5,
                observations=168,
            ),
        )
        assert state.current_step == 2
        assert "42s" in state.survey_state_text
        assert "1234 mm" in state.survey_state_text
        assert "168 obs" in state.survey_state_text
        assert state.fixed_pos_text == "— none"

    def test_active_survey_poll_is_step_2_even_if_mode_lags(self) -> None:
        """``survey.active`` alone is enough — covers a receiver mid-survey
        whose ``CurrentBaseConfig`` read-back hasn't caught up yet."""
        state = fixed_position_step_state(
            CurrentBaseConfig(mode=TmodeMode.DISABLED),
            SurveyInProgress(active=True, valid=False),
        )
        assert state.current_step == 2

    def test_fixed_mode_is_step_3(self) -> None:
        state = fixed_position_step_state(
            CurrentBaseConfig(
                mode=TmodeMode.FIXED,
                latitude=32.7329015,
                longitude=-117.2362788,
                altitude_m=27.94,
                accuracy_mm=47308,
            ),
            SurveyInProgress(active=False, valid=True),
        )
        assert state.current_step == 3
        assert "32.7329015" in state.fixed_pos_text
        assert "-117.2362788" in state.fixed_pos_text
        assert "47308 mm" in state.fixed_pos_text

    def test_fixed_mode_wins_over_stale_active_survey_flag(self) -> None:
        """Fixed mode is terminal — a stale ``active=True`` poll can't
        regress the card back to step 2."""
        state = fixed_position_step_state(
            CurrentBaseConfig(mode=TmodeMode.FIXED, latitude=1.0, longitude=2.0),
            SurveyInProgress(active=True, valid=False),
        )
        assert state.current_step == 3
