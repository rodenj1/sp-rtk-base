"""Tests for the operator-facing copy of a Detection (issue #143).

``ui/pages/*`` is excluded from the coverage gate, so the mapping from a
result to the sentence an operator actually reads lives in
``ui/detection_status.py`` and is tested here. The page is left with
rendering; the wording is decided in a covered module.

Two rules are enforced here rather than by good intentions. The four
outcomes must not share wording — a port that ignores baud, a port
nothing answered on, and a port that could not be opened are three
different next actions, and collapsing them is exactly the failure this
feature exists to prevent. And the copy must stay free of the project's
own vocabulary: an operator holding a GPS receiver has never heard of a
Candidate.
"""

from __future__ import annotations

import pytest

from sp_rtk_base.models.device_models import (
    Candidate,
    CandidateVerdict,
    DetectionOutcome,
    DetectionResult,
    DeviceInfo,
)
from sp_rtk_base.services.device_service import DetectionRefusedError
from sp_rtk_base.services.drivers.base import BAUD_MISMATCH_HINT
from sp_rtk_base.ui.detection_status import (
    describe_connect_failure,
    describe_detection,
    describe_detection_refusal,
    describe_port_failure,
    detected_rate_to_apply,
)

F9P = DeviceInfo(vendor="u-blox", model="ZED-F9P", firmware_version="HPG 1.32")


def _found(rate: int = 57600, device: DeviceInfo | None = F9P) -> DetectionResult:
    return DetectionResult(
        outcome=DetectionOutcome.FOUND,
        baud_rate=rate,
        device=device,
        candidates=[Candidate(baud_rate=rate, verdict=CandidateVerdict.ANSWERED)],
    )


def _not_found(*candidates: Candidate) -> DetectionResult:
    return DetectionResult(
        outcome=DetectionOutcome.NOT_FOUND, candidates=list(candidates)
    )


class TestFound:
    def test_names_the_rate_and_the_receiver(self) -> None:
        line = describe_detection(_found())

        assert "57600" in line.text
        assert "ZED-F9P" in line.text
        assert line.tone == "positive"

    def test_includes_the_firmware_when_there_is_one(self) -> None:
        """Identity is free — MON-VER answering is what 'found' means."""
        assert "HPG 1.32" in describe_detection(_found()).text

    def test_an_unidentified_receiver_still_reports_its_rate(self) -> None:
        anon = DeviceInfo(vendor="u-blox", model="Unknown")
        line = describe_detection(_found(device=anon))

        assert "57600" in line.text
        assert "Unknown" not in line.text

    def test_survives_a_result_with_no_identity_at_all(self) -> None:
        line = describe_detection(_found(device=None))

        assert "57600" in line.text
        assert line.tone == "positive"


class TestRateIndifferent:
    def _result(self) -> DetectionResult:
        return DetectionResult(
            outcome=DetectionOutcome.RATE_INDIFFERENT,
            baud_rate=115200,
            device=F9P,
            candidates=[],
        )

    def test_says_the_rate_is_not_the_problem(self) -> None:
        """The most useful thing this feature can say to someone
        debugging a failed connect on a USB-connected receiver."""
        text = describe_detection(self._result()).text.lower()

        assert "ignores" in text
        assert "not the baud rate" in text

    def test_is_a_warning_not_a_success(self) -> None:
        """Nothing was found; a property of the port was observed."""
        assert describe_detection(self._result()).tone == "warning"

    def test_does_not_lead_with_a_rate(self) -> None:
        """Reporting 115200 here would be reporting the first thing
        tried, dressed up as a discovery."""
        assert not describe_detection(self._result()).text.startswith("Found")


class TestNotFound:
    def test_silence_everywhere_says_check_the_cable_and_port(self) -> None:
        line = describe_detection(
            _not_found(
                Candidate(baud_rate=115200, verdict=CandidateVerdict.SILENT),
                Candidate(baud_rate=57600, verdict=CandidateVerdict.SILENT),
            )
        )

        assert line.tone == "negative"
        assert "cable" in line.text.lower()

    def test_traffic_at_exactly_one_rate_is_reported_as_a_diagnosis(self) -> None:
        """The link is at that rate; the receiver is not taking commands."""
        line = describe_detection(
            _not_found(
                Candidate(baud_rate=115200, verdict=CandidateVerdict.SILENT),
                Candidate(baud_rate=57600, verdict=CandidateVerdict.BYTES_NO_ANSWER),
            )
        )

        assert "57600" in line.text
        assert line.tone == "warning"

    def test_the_diagnosis_does_not_share_wording_with_plain_silence(self) -> None:
        silent = describe_detection(
            _not_found(Candidate(baud_rate=115200, verdict=CandidateVerdict.SILENT))
        )
        diagnosed = describe_detection(
            _not_found(
                Candidate(baud_rate=57600, verdict=CandidateVerdict.BYTES_NO_ANSWER)
            )
        )

        assert silent.text != diagnosed.text
        assert silent.tone != diagnosed.tone

    def test_traffic_at_several_rates_is_not_a_diagnosis(self) -> None:
        """Two rates carrying frames is noise, not a located link."""
        line = describe_detection(
            _not_found(
                Candidate(baud_rate=115200, verdict=CandidateVerdict.BYTES_NO_ANSWER),
                Candidate(baud_rate=57600, verdict=CandidateVerdict.BYTES_NO_ANSWER),
            )
        )

        assert line.tone == "negative"


class TestWhichRateGetsApplied:
    def test_a_found_rate_fills_the_dropdown(self) -> None:
        assert detected_rate_to_apply(_found()) == 57600

    def test_a_rate_indifferent_result_changes_nothing(self) -> None:
        """Every rate works, so 'the' rate is just whatever was tried
        first — writing it back would dress up a non-discovery."""
        result = DetectionResult(
            outcome=DetectionOutcome.RATE_INDIFFERENT, baud_rate=115200
        )

        assert detected_rate_to_apply(result) is None

    def test_nothing_found_changes_nothing(self) -> None:
        assert detected_rate_to_apply(_not_found()) is None


class TestRefusals:
    def test_relay_running_says_why_and_what_to_do(self) -> None:
        line = describe_detection_refusal(
            DetectionRefusedError("relay_running", "raw service message")
        )

        assert "relay" in line.text.lower()
        assert line.tone == "warning"

    def test_already_connected_explains_there_is_nothing_to_detect(self) -> None:
        line = describe_detection_refusal(
            DetectionRefusedError("device_connected", "raw service message")
        )

        assert "disconnect" in line.text.lower()

    def test_the_two_refusals_do_not_share_wording(self) -> None:
        """They share a status code and have unrelated remedies."""
        a = describe_detection_refusal(DetectionRefusedError("relay_running", "x"))
        b = describe_detection_refusal(DetectionRefusedError("device_connected", "x"))

        assert a.text != b.text

    def test_an_unknown_code_still_says_something(self) -> None:
        line = describe_detection_refusal(
            DetectionRefusedError("something_new", "the service explained itself")
        )

        assert "the service explained itself" in line.text


class TestPortFailure:
    """The sweep's own message already names the port and the reason;
    this layer only adds the remedy."""

    SWEEP_MSG = "Could not open /dev/ttyUSB0 at any rate: "

    def test_keeps_the_port_and_the_real_reason(self) -> None:
        line = describe_port_failure(self.SWEEP_MSG + "permission denied")

        assert "/dev/ttyUSB0" in line.text
        assert "permission denied" in line.text
        assert line.tone == "negative"

    def test_a_permission_error_names_the_group_fix(self) -> None:
        """On a Pi this is nearly always the dialout/plugdev group."""
        line = describe_port_failure(self.SWEEP_MSG + "[Errno 13] permission denied")

        assert "dialout" in line.text

    def test_a_non_permission_error_does_not_guess_at_groups(self) -> None:
        line = describe_port_failure(self.SWEEP_MSG + "no such file or directory")

        assert "dialout" not in line.text

    def test_does_not_name_the_port_twice(self) -> None:
        line = describe_port_failure(self.SWEEP_MSG + "permission denied")

        assert line.text.count("/dev/ttyUSB0") == 1


class TestConnectFailureCopy:
    def test_a_baud_mismatch_points_at_the_detect_button(self) -> None:
        """The whole mitigation for Detect being a button someone has to
        find: a fresh install still fails its first Connect."""
        line = describe_connect_failure(
            f"Connection failed: No response from device within 10s — "
            f"{BAUD_MISMATCH_HINT}"
        )

        assert "Detect" in line.text
        assert line.tone == "negative"

    def test_the_stale_advice_is_replaced_not_appended(self) -> None:
        line = describe_connect_failure(
            f"No response from device within 10s — {BAUD_MISMATCH_HINT}"
        )

        assert BAUD_MISMATCH_HINT not in line.text

    def test_a_relay_conflict_says_stop_the_relay(self) -> None:
        line = describe_connect_failure(
            "Cannot connect to device while relay is running — stop relay first"
        )

        assert "Dashboard" in line.text
        assert line.tone == "warning"
        assert "Detect" not in line.text

    def test_an_unrelated_failure_keeps_the_layer_belows_words(self) -> None:
        """Never bury the layer below's error under a guess."""
        line = describe_connect_failure("Serial port /dev/ttyUSB0 is locked")

        assert "Serial port /dev/ttyUSB0 is locked" in line.text
        assert "Detect" not in line.text

    def test_an_unwrapped_failure_still_reads_as_a_connect_failure(self) -> None:
        line = describe_connect_failure("Serial port /dev/ttyUSB0 is locked")

        assert line.text.startswith("Connection failed:")

    def test_an_already_wrapped_failure_is_not_wrapped_twice(self) -> None:
        line = describe_connect_failure("Connection failed: port is locked")

        assert line.text.count("Connection failed:") == 1


class TestCopyStaysFreeOfProjectJargon:
    """``CONTEXT.md``'s vocabulary governs our names, not operator copy."""

    @pytest.mark.parametrize(
        "line",
        [
            describe_detection(_found()),
            describe_detection(
                DetectionResult(
                    outcome=DetectionOutcome.RATE_INDIFFERENT, baud_rate=115200
                )
            ),
            describe_detection(_not_found()),
            describe_detection(
                _not_found(
                    Candidate(baud_rate=57600, verdict=CandidateVerdict.BYTES_NO_ANSWER)
                )
            ),
            describe_port_failure(
                "Could not open /dev/ttyUSB0 at any rate: permission denied"
            ),
        ],
    )
    def test_no_internal_term_reaches_the_operator(self, line: object) -> None:
        text = getattr(line, "text", "").lower()

        for jargon in ("candidate", "rate-indifferent", "verdict", "sweep"):
            assert jargon not in text
