"""Tests for the u-blox driver's correction input (issue #195).

Seam under test: the UbloxDriver's public correction-input methods
(``begin_correction_input``, ``write_corrections``, ``end_correction_input``)
against a simulated receiver link that answers CFG-VALSET / CFG-VALGET.
"""

from __future__ import annotations

import threading
import time

import pytest

from sp_rtk_base.models.device_models import PortId
from sp_rtk_base.services.drivers import ublox
from tests.unit.msm_frames import other_frame
from tests.unit.ublox_sim import SimReceiver, connected_driver, rxm_rtcm

UART1 = "CFG_UART1INPROT_RTCM3X"
UART2 = "CFG_UART2INPROT_RTCM3X"
USB = "CFG_USBINPROT_RTCM3X"
UART1_OUT = "CFG_UART1OUTPROT_RTCM3X"
RXM_RTCM_UART1 = "CFG_MSGOUT_UBX_RXM_RTCM_UART1"
UART2_OUT = "CFG_UART2OUTPROT_RTCM3X"
USB_OUT = "CFG_USBOUTPROT_RTCM3X"


def _sim(uart1: int = 0, uart2: int = 0, usb: int = 0, out: int = 1) -> SimReceiver:
    # A base: RTCM 3 output on every port (``out``), input as given.
    return SimReceiver(
        {
            UART1: uart1,
            UART2: uart2,
            USB: usb,
            UART1_OUT: out,
            UART2_OUT: out,
            USB_OUT: out,
        }
    )


def _inputs(sim: SimReceiver) -> dict[str, int]:
    return {k: sim.ram[k] for k in (UART1, UART2, USB)}


def _outputs(sim: SimReceiver) -> dict[str, int]:
    return {k: sim.ram[k] for k in (UART1_OUT, UART2_OUT, USB_OUT)}


class TestBegin:
    def test_enables_rtcm3_input_on_the_console_port_only(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)

        driver.begin_correction_input(PortId.UART2)

        assert _inputs(sim) == {UART1: 0, UART2: 1, USB: 0}

    def test_enables_every_port_when_the_console_port_is_unknown(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)

        driver.begin_correction_input(None)

        assert _inputs(sim) == {UART1: 1, UART2: 1, USB: 1}

    def test_writes_ram_only_never_bbr_or_flash(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)

        driver.begin_correction_input(None)
        driver.end_correction_input()

        assert sim.valsets
        assert all(m.ram == 1 and not m.bbr and not m.flash for m in sim.valsets)


class TestQuietOutput:
    """Its own RTCM 3 output would crowd the console link (#197)."""

    def test_begin_turns_rtcm3_output_off_on_the_console_port_only(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)

        driver.begin_correction_input(PortId.UART1)

        assert _outputs(sim) == {UART1_OUT: 0, UART2_OUT: 1, USB_OUT: 1}

    def test_begin_turns_it_off_on_every_port_when_the_console_is_unknown(
        self,
    ) -> None:
        sim = _sim()
        driver = connected_driver(sim)

        driver.begin_correction_input(None)

        assert _outputs(sim) == {UART1_OUT: 0, UART2_OUT: 0, USB_OUT: 0}

    def test_end_turns_the_output_back_on(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)
        driver.begin_correction_input(PortId.UART1)

        driver.end_correction_input()

        assert _outputs(sim) == {UART1_OUT: 1, UART2_OUT: 1, USB_OUT: 1}

    def test_end_leaves_off_an_output_that_was_already_off(self) -> None:
        sim = _sim(out=0)
        driver = connected_driver(sim)
        driver.begin_correction_input(PortId.UART1)

        driver.end_correction_input()

        assert _outputs(sim) == {UART1_OUT: 0, UART2_OUT: 0, USB_OUT: 0}


class TestEnd:
    def test_restores_the_settings_begin_found(self) -> None:
        sim = _sim(uart1=1, uart2=0, usb=0)
        driver = connected_driver(sim)
        driver.begin_correction_input(None)

        driver.end_correction_input()

        assert _inputs(sim) == {UART1: 1, UART2: 0, USB: 0}

    def test_without_a_begin_touches_nothing(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)

        driver.end_correction_input()

        assert sim.valsets == []

    def test_restores_only_once(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)
        driver.begin_correction_input(PortId.UART1)
        driver.end_correction_input()
        writes = len(sim.valsets)

        driver.end_correction_input()

        assert len(sim.valsets) == writes


class TestWriteCorrections:
    def test_writes_each_frame_whole_and_unchanged(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)
        frames = [other_frame(1005).data, other_frame(1077).data]

        for frame in frames:
            driver.write_corrections(frame)

        assert sim.raw_writes == frames

    def test_raises_when_disconnected(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)
        sim.is_open = False

        with pytest.raises(ConnectionError):
            driver.write_corrections(other_frame(1005).data)


class TestCorrectionInputCounters:
    def test_reads_the_console_ports_rtcm3_count_from_mon_comms(self) -> None:
        sim = _sim()
        # Protocol slots: UBX, NMEA, RTCM2 unused, RTCM3 (protId 5) last.
        sim.mon_comms = {
            "protIds": [0, 1, 0xFF, 5],
            "ports": {
                0x0100: {
                    "rxBytes": 9000,
                    "msgs": [12, 3, 0, 140],
                    "skipped": 7,
                    "overrunErrs": 0,
                },
                0x0300: {
                    "rxBytes": 50,
                    "msgs": [2, 0, 0, 0],
                    "skipped": 0,
                    "overrunErrs": 0,
                },
            },
        }
        driver = connected_driver(sim)

        counters = driver.get_correction_input_counters(PortId.UART1)

        assert counters is not None
        assert counters.rtcm3_messages == 140
        assert counters.rx_bytes == 9000
        assert counters.skipped_bytes == 7

    def test_is_none_when_the_console_port_is_unknown(self) -> None:
        driver = connected_driver(_sim())

        assert driver.get_correction_input_counters(None) is None


class TestLinkDiagnostics:
    """What the driver measures on the link (bench diagnosis, #197)."""

    def test_begin_asks_for_rxm_rtcm_on_the_console_port_and_end_restores_it(
        self,
    ) -> None:
        sim = _sim()
        sim.ram[RXM_RTCM_UART1] = 0
        driver = connected_driver(sim)

        driver.begin_correction_input(PortId.UART1)
        during = sim.ram[RXM_RTCM_UART1]
        driver.end_correction_input()

        assert (during, sim.ram[RXM_RTCM_UART1]) == (1, 0)

    def test_records_lock_timing_per_operation(self) -> None:
        sim = _sim()
        sim.mon_comms = {
            "protIds": [0, 1, 0xFF, 5],
            "ports": {
                0x0100: {
                    "rxBytes": 1,
                    "msgs": [0, 0, 0, 0],
                    "skipped": 0,
                    "overrunErrs": 0,
                }
            },
        }
        driver = connected_driver(sim)

        driver.write_corrections(other_frame(1077).data)
        driver.get_correction_input_counters(PortId.UART1)
        diagnostics = driver.get_link_diagnostics()

        assert diagnostics is not None
        for op in ("write_corrections", "mon_comms"):
            timing = diagnostics.lock[op]
            assert timing.wait_ms is not None and timing.wait_ms.count == 1
            assert timing.hold_ms is not None and timing.hold_ms.count == 1

    def test_records_each_write_and_the_backlog_after_it(self) -> None:
        sim = _sim()
        sim.out_waiting = 1234
        driver = connected_driver(sim)

        driver.write_corrections(other_frame(1077).data)
        diagnostics = driver.get_link_diagnostics()

        assert diagnostics is not None
        assert diagnostics.write_ms is not None and diagnostics.write_ms.count == 1
        assert diagnostics.tx_backlog_bytes is not None
        assert diagnostics.tx_backlog_bytes.max == 1234

    def test_tallies_the_receivers_rtcm_verdicts_from_any_read(self) -> None:
        sim = _sim()
        sim.unsolicited = [
            rxm_rtcm(1077, used=2),
            rxm_rtcm(1077, used=1),
            rxm_rtcm(1005, used=0, crc_failed=1),
        ]
        driver = connected_driver(sim)

        driver.begin_correction_input(PortId.UART1)  # its reads pass them by
        diagnostics = driver.get_link_diagnostics()

        assert diagnostics is not None
        assert diagnostics.rtcm[1077].model_dump() == {
            "received": 2,
            "used": 1,
            "not_used": 1,
            "crc_failed": 0,
        }
        assert diagnostics.rtcm[1005].crc_failed == 1


class TestWritesOwnTheTxLine:
    """Correction writes don't wait for a poll's reply (bench, #197)."""

    def test_a_write_goes_out_while_a_slow_read_holds_the_driver_lock(self) -> None:
        sim = _sim()
        sim.read_delay_s = 0.4  # each read waits, like a NAV reply
        driver = connected_driver(sim)
        polling = threading.Thread(target=driver.get_survey_position)
        polling.start()
        time.sleep(0.1)  # the poll holds the lock, waiting for its reply

        began = time.monotonic()
        driver.write_corrections(other_frame(1077).data)
        took = time.monotonic() - began
        polling.join(5)

        assert took < 0.2

    def test_a_write_never_lands_inside_another(self) -> None:
        sim = _sim()
        sim.chunked = True  # bytes go out a few at a time
        driver = connected_driver(sim)
        frame = other_frame(1077).data
        writers = [
            threading.Thread(target=driver.write_corrections, args=(frame,))
            for _ in range(20)
        ]
        polls = [threading.Thread(target=driver.get_survey_position) for _ in range(5)]
        for thread in writers + polls:
            thread.start()
        for thread in writers + polls:
            thread.join(5)

        # On the wire, every Frame is still in one piece.
        assert bytes(sim.wire).count(frame) == 20


class TestPositionReuse:
    def test_a_position_read_reuses_the_surveys_fresh_nav_pvt(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)
        driver.get_survey_position()
        polls = len(sim.nav_polls)

        position = driver.get_position()

        assert len(sim.nav_polls) == polls  # no second NAV-PVT poll
        assert position.rtk_status == "fixed"

    def test_without_a_fresh_one_it_polls(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)

        driver.get_position()

        assert sim.nav_polls == [b"\x01\x07"]


class TestPositionReuseLimits:
    def test_an_old_reading_is_polled_afresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ublox, "_PVT_REUSE_S", 0.05)
        sim = _sim()
        driver = connected_driver(sim)
        driver.get_survey_position()
        polls = len(sim.nav_polls)
        time.sleep(0.1)

        driver.get_position()

        assert len(sim.nav_polls) == polls + 1

    def test_a_disconnected_receiver_isnt_answered_from_the_reading(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)
        driver.get_survey_position()
        sim.is_open = False

        with pytest.raises(ConnectionError):
            driver.get_position()
