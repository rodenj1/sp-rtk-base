"""Tests for the GPS receiver profile schema (ReceiverConfig + Profile).

Covers only context-free validation — rules that need no live device
state. The UBX-in liveness guard and the tmode_mode=fixed coordinate
guard are service-level and are tested with the apply ticket.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sp_rtk_base.models.device_models import (
    GnssConstellation,
    PortId,
    RtcmRowId,
    UbxProtocol,
)
from sp_rtk_base.models.profile_models import (
    BaudAssertion,
    BaudConfig,
    DynModel,
    PortProtocolSet,
    Profile,
    ReceiverAssertion,
    ReceiverConfig,
    RtcmStreamConfig,
    TmodeMode,
    merge_profile_into_assertion,
)


def _matrix_ok() -> dict[RtcmRowId, dict[PortId, bool]]:
    """A minimal matrix satisfying the data-link validation rules."""
    return {
        RtcmRowId.RTCM_1005: {PortId.UART1: True},
    }


def _base_kwargs() -> dict[str, object]:
    """Minimal kwargs for a valid ReceiverConfig."""
    return {
        "data_link_port": [PortId.UART1],
        "rtcm_stream": {"matrix": _matrix_ok()},
    }


class TestReceiverConfig:
    """ReceiverConfig is what Apply takes — no name field."""

    def test_minimal_valid_config(self) -> None:
        config = ReceiverConfig.model_validate(_base_kwargs())
        assert config.data_link_port == [PortId.UART1]
        assert config.meas_period_ms == 1000

    def test_has_no_name_field(self) -> None:
        assert "name" not in ReceiverConfig.model_fields

    def test_dyn_model_and_tmode_mode_default_to_omitted(self) -> None:
        config = ReceiverConfig.model_validate(_base_kwargs())
        assert config.dyn_model is None
        assert config.tmode_mode is None

    def test_dyn_model_and_tmode_mode_settable(self) -> None:
        kwargs = _base_kwargs()
        kwargs["dyn_model"] = "stationary"
        kwargs["tmode_mode"] = "fixed"
        config = ReceiverConfig.model_validate(kwargs)
        assert config.dyn_model == DynModel.STATIONARY
        assert config.tmode_mode == TmodeMode.FIXED

    def test_missing_data_link_port_rejected(self) -> None:
        kwargs = _base_kwargs()
        del kwargs["data_link_port"]
        with pytest.raises(ValidationError, match="data_link_port"):
            ReceiverConfig.model_validate(kwargs)

    def test_empty_data_link_port_rejected(self) -> None:
        kwargs = _base_kwargs()
        kwargs["data_link_port"] = []
        with pytest.raises(ValidationError, match="data_link_port"):
            ReceiverConfig.model_validate(kwargs)

    def test_data_link_port_usb_rejected(self) -> None:
        kwargs = _base_kwargs()
        kwargs["data_link_port"] = [PortId.USB]
        with pytest.raises(ValidationError):
            ReceiverConfig.model_validate(kwargs)

    def test_1005_not_on_any_data_link_port_rejected(self) -> None:
        kwargs = _base_kwargs()
        kwargs["rtcm_stream"] = {"matrix": {RtcmRowId.RTCM_1074: {PortId.UART1: True}}}
        with pytest.raises(ValidationError, match="1005"):
            ReceiverConfig.model_validate(kwargs)

    def test_1005_on_a_non_data_link_port_is_not_enough(self) -> None:
        kwargs = _base_kwargs()
        kwargs["data_link_port"] = [PortId.UART1, PortId.UART2]
        kwargs["rtcm_stream"] = {
            "matrix": {
                RtcmRowId.RTCM_1005: {PortId.UART2: False, PortId.UART1: False},
                RtcmRowId.RTCM_1074: {PortId.UART1: True, PortId.UART2: True},
            }
        }
        with pytest.raises(ValidationError, match="1005"):
            ReceiverConfig.model_validate(kwargs)

    def test_data_link_port_with_zero_rows_on_rejected(self) -> None:
        kwargs = _base_kwargs()
        kwargs["data_link_port"] = [PortId.UART1, PortId.UART2]
        # UART2 is a data-link port but has nothing routed to it.
        kwargs["rtcm_stream"] = {"matrix": {RtcmRowId.RTCM_1005: {PortId.UART1: True}}}
        with pytest.raises(ValidationError, match="UART2"):
            ReceiverConfig.model_validate(kwargs)

    @pytest.mark.parametrize("meas_period_ms", [99, 60001])
    def test_meas_period_ms_out_of_range_rejected(self, meas_period_ms: int) -> None:
        kwargs = _base_kwargs()
        kwargs["meas_period_ms"] = meas_period_ms
        with pytest.raises(ValidationError):
            ReceiverConfig.model_validate(kwargs)

    @pytest.mark.parametrize("meas_period_ms", [100, 1000, 60000])
    def test_meas_period_ms_in_range_accepted(self, meas_period_ms: int) -> None:
        kwargs = _base_kwargs()
        kwargs["meas_period_ms"] = meas_period_ms
        config = ReceiverConfig.model_validate(kwargs)
        assert config.meas_period_ms == meas_period_ms

    @pytest.mark.parametrize("baud", [9599, 921601])
    def test_baud_out_of_range_rejected(self, baud: int) -> None:
        kwargs = _base_kwargs()
        kwargs["baud"] = {"uart1": baud}
        with pytest.raises(ValidationError):
            ReceiverConfig.model_validate(kwargs)

    def test_baud_in_range_accepted(self) -> None:
        kwargs = _base_kwargs()
        kwargs["baud"] = {"uart1": 57600, "uart2": 115200}
        config = ReceiverConfig.model_validate(kwargs)
        assert config.baud is not None
        assert config.baud.uart1 == 57600
        assert config.baud.uart2 == 115200

    def test_baud_has_no_usb_field(self) -> None:
        assert "usb" not in {f.lower() for f in ReceiverConfig.model_fields}

    def test_baud_usb_key_rejected(self) -> None:
        """USB CDC has no baud rate — ``BaudConfig`` forbids the key
        outright rather than silently ignoring it (issue #62)."""
        kwargs = _base_kwargs()
        kwargs["baud"] = {"uart1": 57600, "usb": 9600}
        with pytest.raises(ValidationError):
            ReceiverConfig.model_validate(kwargs)

    def test_baud_uart2_only_accepted(self) -> None:
        kwargs = _base_kwargs()
        kwargs["baud"] = {"uart2": 38400}
        config = ReceiverConfig.model_validate(kwargs)
        assert config.baud is not None
        assert config.baud.uart1 is None
        assert config.baud.uart2 == 38400

    @pytest.mark.parametrize("elevation_mask_deg", [-1, 91])
    def test_elevation_mask_deg_out_of_range_rejected(
        self, elevation_mask_deg: int
    ) -> None:
        kwargs = _base_kwargs()
        kwargs["elevation_mask_deg"] = elevation_mask_deg
        with pytest.raises(ValidationError):
            ReceiverConfig.model_validate(kwargs)

    @pytest.mark.parametrize("elevation_mask_deg", [0, 15, 90])
    def test_elevation_mask_deg_in_range_accepted(
        self, elevation_mask_deg: int
    ) -> None:
        kwargs = _base_kwargs()
        kwargs["elevation_mask_deg"] = elevation_mask_deg
        config = ReceiverConfig.model_validate(kwargs)
        assert config.elevation_mask_deg == elevation_mask_deg

    def test_unknown_matrix_row_rejected(self) -> None:
        kwargs = _base_kwargs()
        kwargs["rtcm_stream"] = {"matrix": {"9999": {PortId.UART1: True}}}
        with pytest.raises(ValidationError):
            ReceiverConfig.model_validate(kwargs)

    def test_unknown_matrix_column_rejected(self) -> None:
        kwargs = _base_kwargs()
        kwargs["rtcm_stream"] = {"matrix": {RtcmRowId.RTCM_1005: {"I2C": True}}}
        with pytest.raises(ValidationError):
            ReceiverConfig.model_validate(kwargs)

    def test_constellations_settable(self) -> None:
        kwargs = _base_kwargs()
        kwargs["constellations"] = [GnssConstellation.GPS, GnssConstellation.GLONASS]
        config = ReceiverConfig.model_validate(kwargs)
        assert config.constellations == [
            GnssConstellation.GPS,
            GnssConstellation.GLONASS,
        ]

    def test_extra_field_rejected(self) -> None:
        kwargs = _base_kwargs()
        kwargs["bogus_field"] = "nope"
        with pytest.raises(ValidationError):
            ReceiverConfig.model_validate(kwargs)


class TestProfile:
    """Profile = ReceiverConfig + name, version, hardware, forked_from."""

    def _profile_kwargs(self) -> dict[str, object]:
        kwargs = _base_kwargs()
        kwargs["name"] = "custom-test-profile"
        kwargs["version"] = 1
        kwargs["hardware"] = "ZED-F9P"
        return kwargs

    def test_minimal_valid_profile(self) -> None:
        profile = Profile.model_validate(self._profile_kwargs())
        assert profile.name == "custom-test-profile"
        assert profile.version == 1
        assert profile.hardware == "ZED-F9P"
        assert profile.forked_from is None

    def test_missing_name_rejected(self) -> None:
        kwargs = self._profile_kwargs()
        del kwargs["name"]
        with pytest.raises(ValidationError, match="name"):
            Profile.model_validate(kwargs)

    def test_missing_version_rejected(self) -> None:
        kwargs = self._profile_kwargs()
        del kwargs["version"]
        with pytest.raises(ValidationError, match="version"):
            Profile.model_validate(kwargs)

    def test_unknown_version_rejected(self) -> None:
        kwargs = self._profile_kwargs()
        kwargs["version"] = 999
        with pytest.raises(ValidationError, match="version"):
            Profile.model_validate(kwargs)

    @pytest.mark.parametrize(
        "hardware", ["ZED-F9P", "ZED-F9R", "NEO-M9N", "gen9", "any"]
    )
    def test_known_hardware_tokens_accepted(self, hardware: str) -> None:
        # The model/family catalog is owned by the hardware-detection
        # ticket (#60) and re-exported from models.hardware_identity.
        kwargs = self._profile_kwargs()
        kwargs["hardware"] = hardware
        profile = Profile.model_validate(kwargs)
        assert profile.hardware == hardware

    def test_unknown_hardware_token_rejected(self) -> None:
        kwargs = self._profile_kwargs()
        kwargs["hardware"] = "ACME-9000"
        with pytest.raises(ValidationError, match="hardware"):
            Profile.model_validate(kwargs)

    def test_unregistered_family_token_rejected(self) -> None:
        # "gen10" has no shipped receiver or PROTVER range backing it yet
        # (see hardware_identity.KNOWN_FAMILY_TOKENS) — not a valid target.
        kwargs = self._profile_kwargs()
        kwargs["hardware"] = "gen10"
        with pytest.raises(ValidationError, match="hardware"):
            Profile.model_validate(kwargs)

    def test_forked_from_settable(self) -> None:
        kwargs = self._profile_kwargs()
        kwargs["forked_from"] = "ublox-f9p-base-standard"
        profile = Profile.model_validate(kwargs)
        assert profile.forked_from == "ublox-f9p-base-standard"

    def test_display_name_defaults_to_none(self) -> None:
        profile = Profile.model_validate(self._profile_kwargs())
        assert profile.display_name is None

    def test_display_name_settable(self) -> None:
        kwargs = self._profile_kwargs()
        kwargs["display_name"] = "u-blox F9P — Base Station (Standard)"
        profile = Profile.model_validate(kwargs)
        assert profile.display_name == "u-blox F9P — Base Station (Standard)"

    def test_display_name_roundtrips_through_serialization(self) -> None:
        kwargs = self._profile_kwargs()
        kwargs["display_name"] = "My Pretty Name"
        profile = Profile.model_validate(kwargs)
        data = profile.model_dump(mode="json")
        restored = Profile.model_validate(data)
        assert restored.display_name == "My Pretty Name"

    def test_profile_without_display_name_still_loads(self) -> None:
        kwargs = self._profile_kwargs()
        assert "display_name" not in kwargs
        profile = Profile.model_validate(kwargs)
        data = profile.model_dump(mode="json", exclude_none=True)
        assert "display_name" not in data
        restored = Profile.model_validate(data)
        assert restored.display_name is None
        assert restored == profile

    def test_inherits_receiver_config_validation(self) -> None:
        kwargs = self._profile_kwargs()
        kwargs["meas_period_ms"] = 1
        with pytest.raises(ValidationError):
            Profile.model_validate(kwargs)

    def test_serialization_roundtrip(self) -> None:
        profile = Profile.model_validate(self._profile_kwargs())
        data = profile.model_dump(mode="json")
        restored = Profile.model_validate(data)
        assert restored == profile


# ---------------------------------------------------------------------------
# ReceiverAssertion + merge_profile_into_assertion (issue #97)
# ---------------------------------------------------------------------------


def _live() -> ReceiverAssertion:
    """A fully-populated live read-back, distinct from ``_full_profile()``'s
    values so a merge's fallback-to-live is unambiguous in assertions."""
    return ReceiverAssertion(
        baud=BaudAssertion(uart1=9600, uart2=9600),
        meas_period_ms=2000,
        constellations=[GnssConstellation.BEIDOU],
        ports={
            PortId.UART1: PortProtocolSet(**{"in": [], "out": []}),
            PortId.UART2: PortProtocolSet(**{"in": [], "out": []}),
            PortId.USB: PortProtocolSet(
                **{"in": ["UBX", "NMEA"], "out": ["UBX", "NMEA"]}
            ),
        },
        dyn_model=DynModel.PORTABLE,
        tmode_mode=TmodeMode.SURVEY_IN,
        elevation_mask_deg=5,
        bds_b2_enabled=True,
        spi_enabled=False,
        rtcm_stream=RtcmStreamConfig(
            matrix={RtcmRowId.RTCM_1077: {PortId.UART1: True}}
        ),
    )


def _full_profile() -> Profile:
    """A profile with every field populated except ``tmode_mode`` and the
    USB port entry — the two omissions the built-in profile actually
    makes (see profiles/builtin/ublox-f9p-base-standard.yaml)."""
    return Profile.model_validate(
        {
            "name": "full-profile",
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


class TestBaudAssertion:
    def test_both_fields_required(self) -> None:
        with pytest.raises(ValidationError):
            BaudAssertion.model_validate({"uart1": 57600})

    def test_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BaudAssertion(uart1=1, uart2=9600)


class TestReceiverAssertion:
    def test_every_field_required(self) -> None:
        with pytest.raises(ValidationError):
            ReceiverAssertion.model_validate({})

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReceiverAssertion(**_live().model_dump(), bogus_field="nope")

    def test_fully_populated_instance_is_valid(self) -> None:
        live = _live()
        assert live.dyn_model == DynModel.PORTABLE
        assert live.tmode_mode == TmodeMode.SURVEY_IN


class TestMergeProfileIntoAssertion:
    def test_profile_values_win_where_set(self) -> None:
        merged = merge_profile_into_assertion(_full_profile(), _live())
        assert merged.baud.uart1 == 57600
        assert merged.baud.uart2 == 115200
        assert merged.meas_period_ms == 500
        assert merged.constellations == [
            GnssConstellation.GPS,
            GnssConstellation.GLONASS,
        ]
        assert merged.dyn_model == DynModel.STATIONARY
        assert merged.elevation_mask_deg == 15
        assert merged.bds_b2_enabled is False
        assert merged.spi_enabled is True

    def test_omitted_tmode_mode_falls_back_to_live(self) -> None:
        """The built-in profile omits ``tmode_mode`` on purpose."""
        live = _live()
        merged = merge_profile_into_assertion(_full_profile(), live)
        assert merged.tmode_mode == live.tmode_mode

    def test_omitted_usb_port_falls_back_to_live(self) -> None:
        """The built-in profile omits the USB port entry on purpose."""
        live = _live()
        merged = merge_profile_into_assertion(_full_profile(), live)
        assert merged.ports[PortId.USB] == live.ports[PortId.USB]

    def test_ports_the_profile_sets_are_not_overridden_by_live(self) -> None:
        merged = merge_profile_into_assertion(_full_profile(), _live())
        assert merged.ports[PortId.UART1].out == [UbxProtocol.RTCM3X]

    def test_partial_baud_falls_back_to_live_per_field(self) -> None:
        profile = _full_profile().model_copy(update={"baud": BaudConfig(uart1=57600)})
        live = _live()
        merged = merge_profile_into_assertion(profile, live)
        assert merged.baud.uart1 == 57600
        assert merged.baud.uart2 == live.baud.uart2

    def test_matrix_is_taken_from_the_profile_not_live(self) -> None:
        """``rtcm_stream`` is always assertive — never "leave alone"."""
        merged = merge_profile_into_assertion(_full_profile(), _live())
        assert merged.rtcm_stream.matrix[RtcmRowId.RTCM_1005][PortId.UART1] is True
        # Live's matrix (RTCM_1077 on UART1) is not present in the profile
        # and must not leak into the merged result.
        assert merged.rtcm_stream.matrix[RtcmRowId.RTCM_1077][PortId.UART1] is False

    def test_matrix_covers_every_catalog_row_and_matrix_port(self) -> None:
        merged = merge_profile_into_assertion(_full_profile(), _live())
        assert set(merged.rtcm_stream.matrix.keys()) == set(RtcmRowId)
        for row in merged.rtcm_stream.matrix.values():
            assert set(row.keys()) == {PortId.UART1, PortId.UART2, PortId.USB}

    def test_bare_profile_falls_back_to_live_for_every_optional_field(self) -> None:
        """A profile that sets nothing beyond identity + the minimal matrix
        resolves to *live* for every field it left unset."""
        bare = Profile.model_validate(
            {
                "name": "bare",
                "version": 1,
                "hardware": "ZED-F9P",
                "data_link_port": ["UART1"],
                "rtcm_stream": {"matrix": {"1005": {"UART1": True}}},
            }
        )
        live = _live()
        merged = merge_profile_into_assertion(bare, live)
        assert merged.baud == live.baud
        assert merged.constellations == live.constellations
        assert merged.ports == live.ports
        assert merged.dyn_model == live.dyn_model
        assert merged.tmode_mode == live.tmode_mode
        assert merged.elevation_mask_deg == live.elevation_mask_deg
        assert merged.bds_b2_enabled == live.bds_b2_enabled
        assert merged.spi_enabled == live.spi_enabled

    def test_meas_period_ms_never_falls_back_to_live(self) -> None:
        """The one documented exception: ``ReceiverConfig.meas_period_ms``
        is a plain ``int`` defaulting to 1000, not ``int | None`` — a bare
        profile that never mentions it is indistinguishable from one that
        explicitly wants 1000, so it can't fall back to *live* the way
        every other optional field does."""
        bare = Profile.model_validate(
            {
                "name": "bare",
                "version": 1,
                "hardware": "ZED-F9P",
                "data_link_port": ["UART1"],
                "rtcm_stream": {"matrix": {"1005": {"UART1": True}}},
            }
        )
        live = _live()
        assert bare.meas_period_ms != live.meas_period_ms

        merged = merge_profile_into_assertion(bare, live)

        assert merged.meas_period_ms == bare.meas_period_ms
        assert merged.meas_period_ms != live.meas_period_ms
