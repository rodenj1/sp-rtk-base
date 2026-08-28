"""Tests for hardware identity resolution and the compatibility filter.

Covers the tier ladder (models.hardware_identity.resolve_hardware_identity),
the one-line compatibility rule, the incompatible-reason messaging, and
deterministic default selection.
"""

from __future__ import annotations

from sp_rtk_base.models.hardware_identity import (
    HARDWARE_ANY,
    HARDWARE_UNKNOWN,
    HardwareConfidence,
    HardwareIdentity,
    default_selection,
    identity_from_target,
    incompatible_reason,
    is_compatible,
    resolve_hardware_identity,
)


class TestResolveHardwareIdentityTiers:
    """The five-tier resolution ladder, each attached to a confidence."""

    def test_tier1_mod_extension_is_confirmed(self) -> None:
        identity = resolve_hardware_identity(mod="ZED-F9P")
        assert identity.target == "ZED-F9P"
        assert identity.confidence == HardwareConfidence.CONFIRMED
        assert identity.families == frozenset({"gen9"})

    def test_tier2_explicit_model_string_is_confirmed(self) -> None:
        identity = resolve_hardware_identity(explicit_model="NEO-M9N")
        assert identity.target == "NEO-M9N"
        assert identity.confidence == HardwareConfidence.CONFIRMED

    def test_tier1_takes_priority_over_tier2(self) -> None:
        identity = resolve_hardware_identity(mod="ZED-F9P", explicit_model="NEO-M9N")
        assert identity.target == "ZED-F9P"

    def test_tier3_hw_version_lookup_is_confirmed(self) -> None:
        identity = resolve_hardware_identity(hw_version="00190000")
        assert identity.target == "ZED-F9P"
        assert identity.confidence == HardwareConfidence.CONFIRMED

    def test_tier3_unrecognised_hw_version_falls_through(self) -> None:
        identity = resolve_hardware_identity(hw_version="deadbeef")
        assert identity.confidence == HardwareConfidence.UNKNOWN

    def test_tier4_firmware_family_heuristic_is_inferred(self) -> None:
        identity = resolve_hardware_identity(firmware="EXT CORE HPG 1.32")
        assert identity.target == "ZED-F9P"
        assert identity.confidence == HardwareConfidence.INFERRED
        assert identity.families == frozenset({"gen9"})

    def test_tier4_adr_firmware_maps_to_f9r(self) -> None:
        identity = resolve_hardware_identity(firmware="EXT CORE ADR 1.32")
        assert identity.target == "ZED-F9R"
        assert identity.confidence == HardwareConfidence.INFERRED

    def test_tier5_protver_family_only_is_inferred(self) -> None:
        identity = resolve_hardware_identity(protocol_version="27.31")
        assert identity.target == "gen9"
        assert identity.confidence == HardwareConfidence.INFERRED
        assert identity.families == frozenset({"gen9"})
        assert identity.is_specific_model is False

    def test_nothing_matches_is_unknown(self) -> None:
        identity = resolve_hardware_identity()
        assert identity.target == HARDWARE_UNKNOWN
        assert identity.confidence == HardwareConfidence.UNKNOWN
        assert identity.families == frozenset()

    def test_unrecognised_protver_falls_through_to_unknown(self) -> None:
        identity = resolve_hardware_identity(protocol_version="99.0")
        assert identity.confidence == HardwareConfidence.UNKNOWN

    def test_never_raises_on_garbage_input(self) -> None:
        identity = resolve_hardware_identity(
            mod="",
            explicit_model="",
            hw_version="???",
            firmware="",
            protocol_version="",
        )
        assert identity.confidence == HardwareConfidence.UNKNOWN

    def test_unrecognised_mod_string_passes_through_verbatim_confirmed(self) -> None:
        # A new receiver (e.g. ZED-X20P) is supported by adding a profile
        # tagged with its MOD= string, not by editing this module — MOD=
        # is trusted verbatim regardless of whether it's in any catalog.
        identity = resolve_hardware_identity(mod="ZED-X20P")
        assert identity.target == "ZED-X20P"
        assert identity.confidence == HardwareConfidence.CONFIRMED
        assert identity.families == frozenset()


class TestIsCompatible:
    """The one-line rule: specific-model, family, any, and the non-matches."""

    def test_confirmed_specific_model_match(self) -> None:
        device = identity_from_target("ZED-F9P", HardwareConfidence.CONFIRMED)
        assert is_compatible(device, "ZED-F9P") is True

    def test_inferred_specific_model_never_matches(self) -> None:
        # A guessed model can never unlock a specific-model profile.
        device = identity_from_target("ZED-F9P", HardwareConfidence.INFERRED)
        assert is_compatible(device, "ZED-F9P") is False

    def test_family_match_regardless_of_confidence(self) -> None:
        device = identity_from_target("gen9", HardwareConfidence.INFERRED)
        assert is_compatible(device, "gen9") is True

    def test_any_always_matches(self) -> None:
        device = identity_from_target(HARDWARE_UNKNOWN, HardwareConfidence.UNKNOWN)
        assert is_compatible(device, HARDWARE_ANY) is True

    def test_non_match(self) -> None:
        device = identity_from_target("ZED-F9P", HardwareConfidence.CONFIRMED)
        assert is_compatible(device, "ZED-F9R") is False

    def test_confirmed_model_family_tagged_profile_matches_via_family(self) -> None:
        device = identity_from_target("ZED-F9P", HardwareConfidence.CONFIRMED)
        assert is_compatible(device, "gen9") is True


class TestIncompatibleReason:
    def test_none_when_compatible(self) -> None:
        device = identity_from_target("ZED-F9P", HardwareConfidence.CONFIRMED)
        assert incompatible_reason(device, "ZED-F9P") is None

    def test_names_the_hardware_when_confirmed_but_non_matching(self) -> None:
        device = identity_from_target("ZED-F9P", HardwareConfidence.CONFIRMED)
        reason = incompatible_reason(device, "ZED-F9R")
        assert reason is not None
        assert "ZED-F9P" in reason

    def test_unconfirmed_reason_does_not_leak_a_specific_guess(self) -> None:
        device = identity_from_target("ZED-F9P", HardwareConfidence.INFERRED)
        reason = incompatible_reason(device, "ZED-F9P")
        assert reason is not None
        assert "unconfirmed" in reason
        assert "ZED-F9P" not in reason


class TestDefaultSelection:
    _ordered = [("ublox-f9p-base-standard", "ZED-F9P"), ("zzz-custom", "any")]

    def test_picks_first_compatible_in_order(self) -> None:
        device = identity_from_target("ZED-F9P", HardwareConfidence.CONFIRMED)
        assert default_selection(device, self._ordered) == "ublox-f9p-base-standard"

    def test_null_when_no_compatible_profile(self) -> None:
        device = identity_from_target("ZED-F9R", HardwareConfidence.CONFIRMED)
        assert default_selection(device, [("f9p-only", "ZED-F9P")]) is None

    def test_null_when_confidence_is_inferred(self) -> None:
        device = identity_from_target("ZED-F9P", HardwareConfidence.INFERRED)
        assert default_selection(device, self._ordered) is None

    def test_null_when_confidence_is_unknown(self) -> None:
        device = identity_from_target(HARDWARE_UNKNOWN, HardwareConfidence.UNKNOWN)
        assert default_selection(device, self._ordered) is None


class TestHardwareIdentityIsSpecificModel:
    def test_specific_model_is_specific(self) -> None:
        assert HardwareIdentity(
            target="ZED-F9P", confidence=HardwareConfidence.CONFIRMED
        ).is_specific_model

    def test_family_token_is_not_specific(self) -> None:
        assert not HardwareIdentity(
            target="gen9", confidence=HardwareConfidence.INFERRED
        ).is_specific_model

    def test_unknown_is_not_specific(self) -> None:
        assert not HardwareIdentity(
            target=HARDWARE_UNKNOWN, confidence=HardwareConfidence.UNKNOWN
        ).is_specific_model
