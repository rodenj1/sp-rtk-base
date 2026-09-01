"""Tests for the GPS receiver profile schema (ReceiverConfig + Profile).

Covers only context-free validation — rules that need no live device
state. The UBX-in liveness guard and the tmode_mode=fixed coordinate
guard are service-level and are tested with the apply ticket.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sp_rtk_base.models.device_models import GnssConstellation, PortId, RtcmRowId
from sp_rtk_base.models.profile_models import (
    DynModel,
    Profile,
    ReceiverConfig,
    TmodeMode,
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
