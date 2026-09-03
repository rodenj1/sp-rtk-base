"""Tests for the operator-facing copy of a Verification outcome.

This lives outside `ui/pages/` deliberately: `ui/pages/*` is excluded
from the coverage gate, so the mapping from a result to what an operator
actually reads would be untested by construction if it stayed in the
page. The page is left with rendering; the wording is decided here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sp_rtk_base.models.bluetooth_models import (
    StageResult,
    StageStatus,
    VerificationStage,
    build_result,
)
from sp_rtk_base.services.bluetooth_service import VerificationRefusedError
from sp_rtk_base.ui.bluetooth_status import (
    HeldGreen,
    countdown_label,
    describe_refusal,
    describe_result,
)

ORDER = VerificationStage.ordered()


def _result(**overrides: StageResult) -> object:
    """A Verification result, all Stages passing unless overridden."""
    recorded = {s: StageResult(stage=s, status=StageStatus.PASSED) for s in ORDER}
    for stage_result in overrides.values():
        recorded[stage_result.stage] = stage_result
    return build_result(recorded, rfcomm_channel=1)


def _failed(stage: VerificationStage, code: str) -> object:
    """A result that got as far as *stage* and failed there."""
    recorded: dict[VerificationStage, StageResult] = {}
    for s in ORDER:
        if s is stage:
            recorded[s] = StageResult(stage=s, status=StageStatus.FAILED, code=code)
            break
        recorded[s] = StageResult(stage=s, status=StageStatus.PASSED)
    return build_result(recorded)


class TestGreenCopy:
    def test_a_clean_green_says_save_and_start_will_connect(self) -> None:
        line = describe_result(_result())  # type: ignore[arg-type]
        assert line.tone == "positive"
        assert "Save and Start will connect" in line.text

    def test_a_silent_receiver_is_still_green_but_says_so(self) -> None:
        """The motivating benign case: still surveying in."""
        line = describe_result(  # type: ignore[arg-type]
            _result(
                data=StageResult(
                    stage=VerificationStage.DATA,
                    status=StageStatus.WARNING,
                    code="no_data",
                )
            )
        )
        assert line.tone == "warning"
        assert "surveying" in line.text.lower()

    def test_non_rtcm_data_warns_that_it_may_be_the_wrong_device(self) -> None:
        line = describe_result(  # type: ignore[arg-type]
            _result(
                data=StageResult(
                    stage=VerificationStage.DATA,
                    status=StageStatus.WARNING,
                    code="non_rtcm_data",
                )
            )
        )
        assert line.tone == "warning"
        assert "receiver you meant" in line.text.lower()

    def test_an_unknown_warning_code_still_renders_something(self) -> None:
        """A newly added Warning must not render as a blank Green."""
        line = describe_result(  # type: ignore[arg-type]
            _result(
                data=StageResult(
                    stage=VerificationStage.DATA,
                    status=StageStatus.WARNING,
                    code="something_new",
                    message="The receiver did something unexpected.",
                )
            )
        )
        assert line.tone == "warning"
        assert "something unexpected" in line.text

    def test_the_two_warnings_do_not_share_wording(self) -> None:
        """CONTEXT.md: the weaker evidence must say so in as many words."""
        silent = describe_result(  # type: ignore[arg-type]
            _result(
                data=StageResult(
                    stage=VerificationStage.DATA,
                    status=StageStatus.WARNING,
                    code="no_data",
                )
            )
        )
        noisy = describe_result(  # type: ignore[arg-type]
            _result(
                data=StageResult(
                    stage=VerificationStage.DATA,
                    status=StageStatus.WARNING,
                    code="non_rtcm_data",
                )
            )
        )
        assert silent.text != noisy.text


class TestRedCopy:
    """A Red is always attributed to the Stage that failed, never to a
    raw D-Bus string."""

    def test_a_missing_device_blames_discover_in_words(self) -> None:
        line = describe_result(  # type: ignore[arg-type]
            _failed(VerificationStage.DISCOVER, "device_not_found")
        )
        assert line.tone == "negative"
        assert "discover" in line.text.lower()

    def test_a_rejected_pin_says_the_retry_is_free(self) -> None:
        """Still bonded: correcting the PIN costs the operator nothing."""
        line = describe_result(  # type: ignore[arg-type]
            _failed(VerificationStage.PAIR, "pin_rejected")
        )
        assert "still paired" in line.text.lower()

    def test_a_stranding_says_the_device_is_now_unpaired(self) -> None:
        """Damage the application caused must be named, not left to be
        discovered later from a failing Start."""
        line = describe_result(  # type: ignore[arg-type]
            _failed(VerificationStage.PAIR, "pin_rejected_stranded")
        )
        assert line.tone == "negative"
        assert "now unpaired" in line.text.lower()
        assert "test again" in line.text.lower()

    def test_stranded_and_plain_rejection_do_not_share_wording(self) -> None:
        """The whole reason `pin_rejected_stranded` exists as its own code."""
        stranded = describe_result(  # type: ignore[arg-type]
            _failed(VerificationStage.PAIR, "pin_rejected_stranded")
        )
        plain = describe_result(  # type: ignore[arg-type]
            _failed(VerificationStage.PAIR, "pin_rejected")
        )
        assert stranded.text != plain.text

    def test_a_failed_removal_says_the_pairing_survived(self) -> None:
        line = describe_result(  # type: ignore[arg-type]
            _failed(VerificationStage.PAIR, "bond_removal_failed")
        )
        assert "still paired" in line.text.lower()

    def test_a_refused_socket_blames_connect(self) -> None:
        line = describe_result(  # type: ignore[arg-type]
            _failed(VerificationStage.CONNECT, "socket_refused")
        )
        assert "connect" in line.text.lower()

    def test_an_unknown_code_still_names_the_stage(self) -> None:
        """New codes must degrade to something useful, not to a blank."""
        line = describe_result(  # type: ignore[arg-type]
            _failed(VerificationStage.TRUST, "something_new")
        )
        assert line.tone == "negative"
        assert "trust" in line.text.lower()


class TestRefusalCopy:
    """Each 409 has its own remedy, so each gets its own sentence."""

    def test_relay_running_tells_the_operator_to_stop_it(self) -> None:
        line = describe_refusal(
            VerificationRefusedError(code="relay_running", message="x")
        )
        assert line.tone == "warning"
        assert "stop the relay" in line.text.lower()

    def test_in_progress_tells_them_to_wait(self) -> None:
        line = describe_refusal(
            VerificationRefusedError(code="verification_in_progress", message="x")
        )
        assert "wait" in line.text.lower()

    def test_the_two_refusals_do_not_share_wording(self) -> None:
        a = describe_refusal(
            VerificationRefusedError(code="relay_running", message="x")
        )
        b = describe_refusal(
            VerificationRefusedError(code="verification_in_progress", message="x")
        )
        assert a.text != b.text

    def test_an_unknown_refusal_falls_back_to_the_servers_message(self) -> None:
        line = describe_refusal(
            VerificationRefusedError(code="something_new", message="Server said no")
        )
        assert line.text == "Server said no"


class TestCountdown:
    """The Green is a promise with a short life, and it says how short."""

    def test_it_counts_whole_seconds_remaining(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert countdown_label(now + timedelta(seconds=30), now=now) == "30s"

    def test_it_rounds_up_so_it_never_shows_zero_while_valid(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert countdown_label(now + timedelta(seconds=0.2), now=now) == "1s"

    def test_an_expired_green_has_no_countdown(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert countdown_label(now - timedelta(seconds=1), now=now) is None

    def test_the_exact_moment_of_expiry_has_no_countdown(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert countdown_label(now, now=now) is None


class TestHeldGreen:
    """A Green is void the moment any field it describes is edited.

    Expressed as a predicate over the values currently in the form
    rather than as a flag some edit handler remembers to clear: a flag
    is only as good as the last person who added a field.
    """

    def _held(self) -> HeldGreen:
        return HeldGreen(
            result=_result(),  # type: ignore[arg-type]
            mac_address="AA:BB:CC:DD:EE:FF",
            pin="1234",
        )

    def test_it_stands_for_the_values_it_was_taken_against(self) -> None:
        held = self._held()
        assert held.is_valid_for("AA:BB:CC:DD:EE:FF", "1234")

    def test_editing_the_mac_voids_it(self) -> None:
        held = self._held()
        assert not held.is_valid_for("11:22:33:44:55:66", "1234")

    def test_editing_the_pin_voids_it(self) -> None:
        held = self._held()
        assert not held.is_valid_for("AA:BB:CC:DD:EE:FF", "9999")

    def test_a_cosmetic_pin_edit_does_not_void_it(self) -> None:
        """Normalisation decides, so typing a space is not a PIN change."""
        held = self._held()
        assert held.is_valid_for("AA:BB:CC:DD:EE:FF", " 1234 ")

    def test_clearing_the_pin_voids_it_unless_the_default_was_proven(self) -> None:
        held = self._held()
        assert not held.is_valid_for("AA:BB:CC:DD:EE:FF", "")

        default = HeldGreen(
            result=_result(),  # type: ignore[arg-type]
            mac_address="AA:BB:CC:DD:EE:FF",
            pin="0000",
        )
        assert default.is_valid_for("AA:BB:CC:DD:EE:FF", "")

    def test_it_expires_on_its_own(self) -> None:
        held = self._held()
        expired = held.result.expires_at + timedelta(seconds=1)
        assert not held.is_valid_for("AA:BB:CC:DD:EE:FF", "1234", now=expired)

    def test_it_stands_right_up_to_the_moment_of_expiry(self) -> None:
        held = self._held()
        just_before = held.result.expires_at - timedelta(milliseconds=1)
        assert held.is_valid_for("AA:BB:CC:DD:EE:FF", "1234", now=just_before)

    def test_a_red_is_never_held(self) -> None:
        """Only a Green entitles the operator to 'Save & Start now'."""
        with pytest.raises(ValueError):
            HeldGreen(
                result=_failed(VerificationStage.PAIR, "pin_rejected"),  # type: ignore[arg-type]
                mac_address="AA:BB:CC:DD:EE:FF",
                pin="1234",
            )
