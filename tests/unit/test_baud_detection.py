"""Tests for Detection — the baud-rate sweep (issue #142).

The sweep itself is vendor-neutral and lives on
:class:`~sp_rtk_base.services.drivers.base.GpsReceiverDriver`; only the
single-Candidate trial is vendor-specific.  These tests drive the sweep
through a scripted driver that implements nothing but the trial, so what
is under test is the ordering, the confirmation rule, cancellation and
the outcome classification — never MON-VER.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import serial  # type: ignore[import-untyped]

from sp_rtk_base.models.device_models import (
    BAUD_RATES,
    DEFAULT_BAUD,
    CandidateVerdict,
    DetectionOutcome,
    DeviceInfo,
)
from sp_rtk_base.services.device_service import (
    DetectionRefusedError,
    DeviceService,
)
from sp_rtk_base.services.drivers import create_driver
from sp_rtk_base.services.drivers.base import BAUD_MISMATCH_HINT, DETECTION_CANDIDATES
from sp_rtk_base.services.drivers.fake import (
    FAKE_DETECTED_BAUD,
    FAKE_RATE_INDIFFERENT_PORT,
    FakeGpsDriver,
)
from sp_rtk_base.services.drivers.ublox import UbloxDriver

# ---------------------------------------------------------------------------
# Scripted driver — implements the trial and nothing else
# ---------------------------------------------------------------------------

STUB_DEVICE = DeviceInfo(vendor="u-blox", model="ZED-F9P", firmware_version="HPG 1.32")


def _mon_ver() -> SimpleNamespace:
    """A parsed MON-VER reply, shaped as pyubx2 hands it over."""
    return SimpleNamespace(
        identity="MON-VER",
        swVersion="EXT CORE 1.00 (f4c834)",
        hwVersion="00190000",
        extension_00="FWVER=HPG 1.32",
        extension_01="PROTVER=27.31",
        extension_02="MOD=ZED-F9P",
        extension_03=None,
    )


def _open_port() -> MagicMock:
    ser = MagicMock()
    ser.is_open = True
    return ser


class ScriptedDriver(FakeGpsDriver):
    """Answers each Candidate from a script, and records the order tried.

    Subclasses the fake rather than the ABC because the ABC has thirty-odd
    abstract methods and none of them are what this exercises.
    """

    def __init__(self, verdicts: dict[int, CandidateVerdict]) -> None:
        super().__init__()
        self._verdicts = verdicts
        self.tried: list[int] = []
        self.cancel_after: int | None = None
        self._detection_cancelled_flag = False

    def try_baud_candidate(
        self, port: str, baud_rate: int, budget_s: float
    ) -> tuple[CandidateVerdict, DeviceInfo | None]:
        self.tried.append(baud_rate)
        if self.cancel_after is not None and len(self.tried) >= self.cancel_after:
            self._detection_cancelled_flag = True
        verdict = self._verdicts.get(baud_rate, CandidateVerdict.SILENT)
        info = STUB_DEVICE if verdict is CandidateVerdict.ANSWERED else None
        return verdict, info

    def is_detection_cancelled(self) -> bool:
        return self._detection_cancelled_flag


# ---------------------------------------------------------------------------
# Finding a rate
# ---------------------------------------------------------------------------


class TestSweepFindsARate:
    def test_reports_found_with_the_rate_and_the_device(self) -> None:
        driver = ScriptedDriver({57600: CandidateVerdict.ANSWERED})

        result = driver.detect_baud("/dev/ttyUSB0")

        assert result.outcome is DetectionOutcome.FOUND
        assert result.baud_rate == 57600
        assert result.device is not None
        assert result.device.model == "ZED-F9P"

    def test_stops_at_the_first_answer(self) -> None:
        driver = ScriptedDriver({57600: CandidateVerdict.ANSWERED})

        driver.detect_baud("/dev/ttyUSB0")

        # 115200 is first in the standard order, 57600 second — and the
        # sweep must not keep going once a rate has answered.
        assert driver.tried == [115200, 57600]

    def test_candidates_carry_every_rate_tried_with_its_verdict(self) -> None:
        driver = ScriptedDriver({57600: CandidateVerdict.ANSWERED})

        result = driver.detect_baud("/dev/ttyUSB0")

        assert [(c.baud_rate, c.verdict) for c in result.candidates] == [
            (115200, CandidateVerdict.SILENT),
            (57600, CandidateVerdict.ANSWERED),
        ]


class TestCandidateOrder:
    def test_preferred_rate_is_tried_first(self) -> None:
        driver = ScriptedDriver({})

        driver.detect_baud("/dev/ttyUSB0", preferred_baud=460800)

        assert driver.tried[0] == 460800

    def test_preferred_rate_is_not_tried_twice(self) -> None:
        driver = ScriptedDriver({})

        driver.detect_baud("/dev/ttyUSB0", preferred_baud=460800)

        assert driver.tried.count(460800) == 1
        assert sorted(driver.tried) == sorted(DETECTION_CANDIDATES)

    def test_an_unlisted_preferred_rate_is_still_tried_first(self) -> None:
        driver = ScriptedDriver({})

        driver.detect_baud("/dev/ttyUSB0", preferred_baud=4800)

        assert driver.tried[0] == 4800
        assert set(driver.tried) == {4800, *DETECTION_CANDIDATES}


# ---------------------------------------------------------------------------
# The confirmation rule (map #140, decision 6)
# ---------------------------------------------------------------------------


class TestConfirmationRule:
    """Confirm only when there is no disconfirming evidence."""

    def test_first_candidate_hit_is_confirmed_against_a_wrong_rate(self) -> None:
        driver = ScriptedDriver({115200: CandidateVerdict.ANSWERED})

        driver.detect_baud("/dev/ttyUSB0")

        # 115200 leads the standard order, so this is a first-Candidate
        # hit and carries no disconfirming evidence: one deliberately
        # wrong rate must be tried before the answer is believed.
        assert driver.tried == [115200, 9600]

    def test_a_confirmed_first_candidate_hit_is_found_not_indifferent(self) -> None:
        driver = ScriptedDriver({115200: CandidateVerdict.ANSWERED})

        result = driver.detect_baud("/dev/ttyUSB0")

        assert result.outcome is DetectionOutcome.FOUND
        assert result.baud_rate == 115200

    def test_a_port_that_answers_at_every_rate_is_rate_indifferent(self) -> None:
        driver = ScriptedDriver(
            dict.fromkeys(DETECTION_CANDIDATES, CandidateVerdict.ANSWERED)
        )

        result = driver.detect_baud("/dev/ttyUSB0")

        assert result.outcome is DetectionOutcome.RATE_INDIFFERENT
        assert result.baud_rate == 115200

    def test_a_later_hit_skips_confirmation_entirely(self) -> None:
        driver = ScriptedDriver({38400: CandidateVerdict.ANSWERED})

        result = driver.detect_baud("/dev/ttyUSB0")

        # Two rates already failed, so the port has proven itself
        # rate-sensitive — there is nothing left to confirm.
        assert driver.tried == [115200, 57600, 38400]
        assert result.outcome is DetectionOutcome.FOUND

    def test_the_confirming_candidate_is_recorded_like_any_other(self) -> None:
        driver = ScriptedDriver({115200: CandidateVerdict.ANSWERED})

        result = driver.detect_baud("/dev/ttyUSB0")

        assert [(c.baud_rate, c.verdict) for c in result.candidates] == [
            (115200, CandidateVerdict.ANSWERED),
            (9600, CandidateVerdict.SILENT),
        ]

    def test_a_hit_at_the_slowest_rate_is_confirmed_against_the_fastest(self) -> None:
        driver = ScriptedDriver({9600: CandidateVerdict.ANSWERED})

        driver.detect_baud("/dev/ttyUSB0", preferred_baud=9600)

        assert driver.tried == [9600, 921600]


# ---------------------------------------------------------------------------
# Finding nothing
# ---------------------------------------------------------------------------


class TestSweepFindsNothing:
    def test_silence_everywhere_is_not_found(self) -> None:
        driver = ScriptedDriver({})

        result = driver.detect_baud("/dev/ttyUSB0")

        assert result.outcome is DetectionOutcome.NOT_FOUND
        assert result.baud_rate is None
        assert result.device is None

    def test_every_candidate_is_still_reported(self) -> None:
        driver = ScriptedDriver({})

        result = driver.detect_baud("/dev/ttyUSB0")

        assert len(result.candidates) == len(DETECTION_CANDIDATES)
        assert all(c.verdict is CandidateVerdict.SILENT for c in result.candidates)

    def test_bytes_without_an_answer_is_not_a_find(self) -> None:
        """The diagnostic case: the link is there, UBX input is not."""
        driver = ScriptedDriver({57600: CandidateVerdict.BYTES_NO_ANSWER})

        result = driver.detect_baud("/dev/ttyUSB0")

        assert result.outcome is DetectionOutcome.NOT_FOUND
        assert result.baud_rate is None
        verdicts = {c.baud_rate: c.verdict for c in result.candidates}
        assert verdicts[57600] is CandidateVerdict.BYTES_NO_ANSWER


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_a_cancelled_sweep_stops_early(self) -> None:
        driver = ScriptedDriver({})
        driver.cancel_after = 2

        driver.detect_baud("/dev/ttyUSB0")

        assert driver.tried == [115200, 57600]

    def test_a_cancelled_sweep_reports_not_found_with_what_it_saw(self) -> None:
        driver = ScriptedDriver({})
        driver.cancel_after = 2

        result = driver.detect_baud("/dev/ttyUSB0")

        assert result.outcome is DetectionOutcome.NOT_FOUND
        assert len(result.candidates) == 2

    def test_cancelling_on_a_first_candidate_hit_skips_confirmation(self) -> None:
        """A cancel must not be overridden by the confirmation step."""
        driver = ScriptedDriver({115200: CandidateVerdict.ANSWERED})
        driver.cancel_after = 1

        result = driver.detect_baud("/dev/ttyUSB0")

        assert driver.tried == [115200]
        assert result.outcome is DetectionOutcome.FOUND


# ---------------------------------------------------------------------------
# The u-blox trial — the one vendor-specific half
# ---------------------------------------------------------------------------


class TestUbloxTrial:
    """``try_baud_candidate`` classifying what came back at one rate."""

    @pytest.fixture(autouse=True)
    def _mock_fcntl(self) -> Iterator[None]:
        with patch("sp_rtk_base.services.drivers.ublox.fcntl.flock"):
            yield

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_a_mon_ver_reply_is_answered_and_carries_identity(
        self, mock_serial_cls: MagicMock, mock_reader_cls: MagicMock
    ) -> None:
        mock_serial_cls.return_value = _open_port()
        reader = MagicMock()
        reader.read.return_value = (b"", _mon_ver())
        mock_reader_cls.return_value = reader

        verdict, info = UbloxDriver().try_baud_candidate("/dev/ttyUSB0", 57600, 1.5)

        assert verdict is CandidateVerdict.ANSWERED
        assert info is not None
        assert info.model == "ZED-F9P"
        assert info.firmware_version == "HPG 1.32"

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_framed_traffic_without_a_reply_is_bytes_no_answer(
        self, mock_serial_cls: MagicMock, mock_reader_cls: MagicMock
    ) -> None:
        """The diagnostic case — the link is at this rate, UBX input is off."""
        mock_serial_cls.return_value = _open_port()
        reader = MagicMock()
        # Well-framed RTCM keeps arriving; MON-VER never does.
        reader.read.return_value = (b"", SimpleNamespace(identity="RTCM(1005)"))
        mock_reader_cls.return_value = reader

        verdict, info = UbloxDriver().try_baud_candidate("/dev/ttyUSB0", 57600, 0.01)

        assert verdict is CandidateVerdict.BYTES_NO_ANSWER
        assert info is None

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_nothing_parseable_is_silent(
        self, mock_serial_cls: MagicMock, mock_reader_cls: MagicMock
    ) -> None:
        """Raw garbage at a wrong baud does not parse, so it is not evidence."""
        mock_serial_cls.return_value = _open_port()
        reader = MagicMock()
        reader.read.return_value = (b"\xff\xfe", None)
        mock_reader_cls.return_value = reader

        verdict, info = UbloxDriver().try_baud_candidate("/dev/ttyUSB0", 57600, 0.01)

        assert verdict is CandidateVerdict.SILENT
        assert info is None

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_the_port_is_closed_whatever_the_verdict(
        self, mock_serial_cls: MagicMock, mock_reader_cls: MagicMock
    ) -> None:
        """The next Candidate must find the port free."""
        ser = _open_port()
        mock_serial_cls.return_value = ser
        reader = MagicMock()
        reader.read.return_value = (b"", _mon_ver())
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        driver.try_baud_candidate("/dev/ttyUSB0", 57600, 1.5)

        ser.close.assert_called()
        assert not driver.is_connected

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_the_read_timeout_never_exceeds_the_candidate_budget(
        self, mock_serial_cls: MagicMock, mock_reader_cls: MagicMock
    ) -> None:
        """_READ_TIMEOUT's 3s is longer than a whole Candidate is allowed."""
        mock_serial_cls.return_value = _open_port()
        reader = MagicMock()
        reader.read.return_value = (b"", _mon_ver())
        mock_reader_cls.return_value = reader

        UbloxDriver().try_baud_candidate("/dev/ttyUSB0", 57600, 0.4)

        assert mock_serial_cls.call_args.kwargs["timeout"] == 0.4

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_a_cancel_landing_mid_trial_is_silent_not_a_crash(
        self, mock_serial_cls: MagicMock, mock_reader_cls: MagicMock
    ) -> None:
        """A cancel is not evidence about the rate, so it is the weakest
        verdict — and the sweep's own check is what stops the loop."""
        ser = _open_port()
        mock_serial_cls.return_value = ser
        reader = MagicMock()
        reader.read.return_value = (b"", _mon_ver())
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        driver.cancel_connect()  # raises the shared cancel signal

        verdict, info = driver.try_baud_candidate("/dev/ttyUSB0", 57600, 1.5)

        assert verdict is CandidateVerdict.SILENT
        assert info is None
        ser.close.assert_called()

    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_an_unopenable_port_raises_rather_than_reporting_a_verdict(
        self, mock_serial_cls: MagicMock
    ) -> None:
        """The sweep decides what that means; the trial does not guess."""
        mock_serial_cls.side_effect = serial.SerialException("permission denied")

        with pytest.raises(serial.SerialException):
            UbloxDriver().try_baud_candidate("/dev/ttyUSB0", 57600, 1.5)


class TestUnopenablePort:
    """A port that never opens at any rate is not a Detection outcome."""

    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_a_sweep_that_never_opened_the_port_raises(
        self, mock_serial_cls: MagicMock
    ) -> None:
        mock_serial_cls.side_effect = serial.SerialException("permission denied")

        with pytest.raises(ConnectionError, match="Could not open"):
            UbloxDriver().detect_baud("/dev/ttyUSB0")

    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_the_underlying_reason_survives_into_the_message(
        self, mock_serial_cls: MagicMock
    ) -> None:
        """'Not found' would send someone hunting a rate that was never
        the problem."""
        mock_serial_cls.side_effect = serial.SerialException("permission denied")

        with pytest.raises(ConnectionError, match="permission denied"):
            UbloxDriver().detect_baud("/dev/ttyUSB0")


# ---------------------------------------------------------------------------
# The service — guards, and leaving nothing behind
# ---------------------------------------------------------------------------


class TestServiceDetection:
    """The fake driver stands in for a real one.

    ``detect_baud`` builds its own throwaway driver — that is what
    makes "leaves the service as it found it" structural — so the
    factory is patched rather than the registry mutated, which would
    leak a vendor key into every test that runs after.
    """

    @pytest.fixture(autouse=True)
    def _fake_factory(self) -> Iterator[None]:
        real = create_driver

        def _factory(vendor: str) -> FakeGpsDriver:
            if vendor == "fake":
                return FakeGpsDriver()
            return real(vendor)  # type: ignore[return-value]

        with patch("sp_rtk_base.services.drivers.create_driver", side_effect=_factory):
            yield

    @pytest.mark.asyncio()
    async def test_reports_what_the_sweep_found(self) -> None:
        svc = DeviceService()

        result = await svc.detect_baud("FAKE", vendor="fake")

        assert result.outcome is DetectionOutcome.FOUND
        assert result.baud_rate == FAKE_DETECTED_BAUD

    @pytest.mark.asyncio()
    async def test_refuses_while_the_relay_is_running(self) -> None:
        svc = DeviceService()
        svc.set_relay_check(lambda: True)

        with pytest.raises(DetectionRefusedError) as exc:
            await svc.detect_baud("FAKE", vendor="fake")

        assert exc.value.code == "relay_running"

    @pytest.mark.asyncio()
    async def test_refuses_while_a_device_is_connected(self) -> None:
        """The rate is already known — there is nothing to detect."""
        svc = DeviceService()
        svc.set_driver(FakeGpsDriver())
        await svc.connect("FAKE", 115200)

        with pytest.raises(DetectionRefusedError) as exc:
            await svc.detect_baud("FAKE", vendor="fake")

        assert exc.value.code == "device_connected"

    @pytest.mark.asyncio()
    async def test_leaves_the_service_exactly_as_it_found_it(self) -> None:
        """Detection persists nothing and connects nothing (decision 9)."""
        svc = DeviceService()
        before = svc.get_status()

        await svc.detect_baud("FAKE", vendor="fake")

        after = svc.get_status()
        assert after.state is before.state
        assert after.port is None
        assert after.baud_rate is None
        assert after.info is None
        assert not svc.is_connected

    @pytest.mark.asyncio()
    async def test_does_not_load_a_driver_into_the_service(self) -> None:
        """The throwaway driver is what makes the invariant structural."""
        svc = DeviceService()

        await svc.detect_baud("FAKE", vendor="fake")

        assert not svc.is_available

    @pytest.mark.asyncio()
    async def test_an_unknown_vendor_is_a_value_error(self) -> None:
        svc = DeviceService()

        with pytest.raises(ValueError, match="Unknown GPS driver"):
            await svc.detect_baud("FAKE", vendor="nonesuch")

    @pytest.mark.asyncio()
    async def test_a_rate_indifferent_port_is_reported_as_such(self) -> None:
        svc = DeviceService()

        result = await svc.detect_baud(FAKE_RATE_INDIFFERENT_PORT, vendor="fake")

        assert result.outcome is DetectionOutcome.RATE_INDIFFERENT


class TestStaleCancelSignal:
    """A cancel left over from an abandoned connect must not kill the next sweep."""

    @pytest.fixture(autouse=True)
    def _mock_fcntl(self) -> Iterator[None]:
        with patch("sp_rtk_base.services.drivers.ublox.fcntl.flock"):
            yield

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_a_detection_clears_a_stale_cancel_before_sweeping(
        self, mock_serial_cls: MagicMock, mock_reader_cls: MagicMock
    ) -> None:
        mock_serial_cls.return_value = _open_port()
        reader = MagicMock()
        reader.read.return_value = (b"", _mon_ver())
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        # Exactly the state an operator leaves behind by hitting Cancel
        # on a connect: only ``connect()`` ever clears this event.
        driver.cancel_connect()

        result = driver.detect_baud("/dev/ttyUSB0")

        assert result.outcome is not DetectionOutcome.NOT_FOUND
        assert result.candidates != []


# ---------------------------------------------------------------------------
# The offered rates and the swept rates must not drift apart
# ---------------------------------------------------------------------------


class TestRateSetsAgree:
    """A rate an operator can pick must be a rate Detection tries.

    The two lists are deliberately separate — ``BAUD_RATES`` is the
    order a dropdown reads best in, ``DETECTION_CANDIDATES`` is
    likelihood order — but they must cover the same set. Adding a rate
    to one and not the other gives an operator a rate Detect can never
    find, or sweeps a rate they cannot then select.
    """

    def test_every_offered_rate_is_swept(self) -> None:
        assert set(BAUD_RATES) == set(DETECTION_CANDIDATES)

    def test_the_default_is_one_of_the_offered_rates(self) -> None:
        assert DEFAULT_BAUD in BAUD_RATES

    def test_the_default_matches_the_documented_deployment(self) -> None:
        """docs/zed-f9p-base-station-config-reference.md: FTDI -> UART1
        at 57600. The UI used to pre-fill a rate matching nothing."""
        assert DEFAULT_BAUD == 57600

    def test_the_fake_answers_at_a_rate_the_default_does_not_preselect(self) -> None:
        """Demo mode has to demonstrate something.

        The fake's rate is only interesting while it differs from the
        pre-filled one — a Detection that confirms the default was right
        all along shows an operator nothing. Issue #146 moving the
        default to 57600 silently voided that contrast once already.
        """
        assert FAKE_DETECTED_BAUD != DEFAULT_BAUD


# ---------------------------------------------------------------------------
# A busy line at the wrong rate (found on the bench, test-base.lan)
# ---------------------------------------------------------------------------


class _MisframedBusyLine:
    """A receiver streaming RTCM, read at the wrong rate.

    Bytes never stop arriving and none of them form a protocol header, so
    the serial timeout — which only fires on silence — never fires, and
    ``UBXReader.read()`` never returns on its own. This is the base on the
    bench: it streams RTCM on UART1, and Detection's confirmation step
    deliberately tries a wrong rate on exactly that line.
    """

    is_open = True

    def read(self, size: int = 1) -> bytes:
        time.sleep(0.001)
        return b"\x00" * size

    def fileno(self) -> int:
        return 0

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


class TestABusyLineCannotHangATrial:
    @pytest.fixture(autouse=True)
    def _mock_fcntl(self) -> Iterator[None]:
        with patch("sp_rtk_base.services.drivers.ublox.fcntl.flock"):
            yield

    def _run_with_watchdog(self, fn: object, limit_s: float) -> tuple[bool, object]:
        result: list[object] = []
        t = threading.Thread(target=lambda: result.append(fn()), daemon=True)  # type: ignore[operator]
        t.start()
        t.join(limit_s)
        return (not t.is_alive(), result[0] if result else None)

    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_a_trial_on_a_busy_misframed_line_ends_within_its_budget(
        self, mock_serial_cls: MagicMock
    ) -> None:
        """The real UBXReader, not a mock — the hang lives inside it."""
        mock_serial_cls.return_value = _MisframedBusyLine()
        driver = UbloxDriver()

        finished, outcome = self._run_with_watchdog(
            lambda: driver.try_baud_candidate("/dev/ttyUSB0", 9600, 0.3), 3.0
        )

        assert finished, "try_baud_candidate hung on a busy line"
        assert outcome == (CandidateVerdict.SILENT, None)

    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_a_trial_on_a_busy_line_releases_the_port(
        self, mock_serial_cls: MagicMock
    ) -> None:
        """A hung trial held the port and its flock until the app restarted."""
        line = _MisframedBusyLine()
        mock_serial_cls.return_value = line
        driver = UbloxDriver()

        finished, _ = self._run_with_watchdog(
            lambda: driver.try_baud_candidate("/dev/ttyUSB0", 9600, 0.3), 3.0
        )

        assert finished
        assert not line.is_open

    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_a_connect_at_the_wrong_rate_on_a_busy_line_times_out(
        self, mock_serial_cls: MagicMock
    ) -> None:
        """Same trap in Connect: 'No response within 10s' could never fire."""
        mock_serial_cls.return_value = _MisframedBusyLine()
        driver = UbloxDriver()
        driver.CONNECT_TIMEOUT = 0.3  # type: ignore[misc]

        def attempt() -> str:
            try:
                driver.connect("/dev/ttyUSB0", 9600)
            except ConnectionError as exc:
                return str(exc)
            return "connected?!"

        finished, message = self._run_with_watchdog(attempt, 3.0)

        assert finished, "connect hung on a busy line"
        assert BAUD_MISMATCH_HINT in str(message)
