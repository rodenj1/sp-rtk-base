"""Tests for the Bluetooth Verification vocabulary.

`models/bluetooth_models.py` is the single place the Stage names, the
Stage outcomes, and PIN normalisation live — the service, the API, the
Input page, and these tests all read them from there (issue #127 §10).
"""

from __future__ import annotations

from sp_rtk_base.models.bluetooth_models import (
    StageResult,
    StageStatus,
    VerificationStage,
    build_result,
    normalize_pin,
)

ORDER = VerificationStage.ordered()


class TestNormalizePin:
    """A blank PIN field must become the PIN the relay would actually use.

    `_save_input` used to persist `""` for a blank field while the
    relay's `BluetoothConfig.pin` defaults to `"0000"`, so a Green could
    describe a pairing Save would not reproduce (issue #127 §9).
    """

    def test_blank_becomes_the_relay_default(self) -> None:
        assert normalize_pin("") == "0000"

    def test_whitespace_only_becomes_the_relay_default(self) -> None:
        assert normalize_pin("   ") == "0000"

    def test_none_becomes_the_relay_default(self) -> None:
        assert normalize_pin(None) == "0000"

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert normalize_pin(" 1234 ") == "1234"

    def test_a_real_pin_is_left_alone(self) -> None:
        assert normalize_pin("1234") == "1234"


class TestVerificationStage:
    """Five Stages, in the order the connect path walks them.

    `channel` was dropped by issue #129: `discover_rfcomm_channel` is a
    stub `return 1`, and a step whose outcome is structurally fixed
    cannot fail — so it is not a Stage, it teaches operators to stop
    reading them.  The channel number is a detail on `connect`.
    """

    def test_stages_are_in_connect_path_order(self) -> None:
        assert VerificationStage.ordered() == [
            VerificationStage.DISCOVER,
            VerificationStage.PAIR,
            VerificationStage.TRUST,
            VerificationStage.CONNECT,
            VerificationStage.DATA,
        ]

    def test_there_is_no_channel_stage(self) -> None:
        assert "channel" not in {s.value for s in VerificationStage}

    def test_stage_values_are_the_shared_wire_names(self) -> None:
        assert VerificationStage.DISCOVER == "discover"
        assert VerificationStage.DATA == "data"


class TestBuildResult:
    """Assembling a VerificationResult from the Stages that actually ran.

    Two rules live here rather than in the service, because the Input
    page and the tests both depend on them: Stages after a failure are
    reported `skipped` rather than omitted, and a Warning does not void
    a Green.
    """

    def test_all_stages_passing_is_green(self) -> None:
        result = build_result(
            {s: StageResult(stage=s, status=StageStatus.PASSED) for s in ORDER},
            rfcomm_channel=1,
        )
        assert result.verdict == "green"
        assert result.failing_stage is None

    def test_a_data_warning_is_still_green(self) -> None:
        """The mid-survey receiver case — failing on it invents a false Red."""
        recorded = {s: StageResult(stage=s, status=StageStatus.PASSED) for s in ORDER}
        recorded[VerificationStage.DATA] = StageResult(
            stage=VerificationStage.DATA,
            status=StageStatus.WARNING,
            code="no_data",
        )
        result = build_result(recorded, rfcomm_channel=1)
        assert result.verdict == "green"
        assert result.failing_stage is None

    def test_a_failure_is_red_and_names_the_stage(self) -> None:
        result = build_result(
            {
                VerificationStage.DISCOVER: StageResult(
                    stage=VerificationStage.DISCOVER, status=StageStatus.PASSED
                ),
                VerificationStage.PAIR: StageResult(
                    stage=VerificationStage.PAIR,
                    status=StageStatus.FAILED,
                    code="pin_rejected",
                ),
            },
        )
        assert result.verdict == "red"
        assert result.failing_stage == VerificationStage.PAIR

    def test_stages_never_reached_are_reported_skipped_not_omitted(self) -> None:
        """'We never got as far as trying' is information (#127 §4)."""
        result = build_result(
            {
                VerificationStage.DISCOVER: StageResult(
                    stage=VerificationStage.DISCOVER,
                    status=StageStatus.FAILED,
                    code="device_not_found",
                ),
            },
        )
        assert [s.stage for s in result.stages] == ORDER
        assert [s.status for s in result.stages[1:]] == [StageStatus.SKIPPED] * 4

    def test_green_expires_thirty_seconds_after_it_was_taken(self) -> None:
        result = build_result(
            {s: StageResult(stage=s, status=StageStatus.PASSED) for s in ORDER},
        )
        assert (result.expires_at - result.verified_at).total_seconds() == 30.0
