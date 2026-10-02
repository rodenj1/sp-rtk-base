"""Tests for the u-blox driver's correction input (issue #195).

Seam under test: the UbloxDriver's public correction-input methods
(``begin_correction_input``, ``write_corrections``, ``end_correction_input``)
against a simulated receiver link that answers CFG-VALSET / CFG-VALGET.
"""

from __future__ import annotations

import pytest

from sp_rtk_base.models.device_models import PortId
from tests.unit.msm_frames import other_frame
from tests.unit.ublox_sim import SimReceiver, connected_driver

UART1 = "CFG_UART1INPROT_RTCM3X"
UART2 = "CFG_UART2INPROT_RTCM3X"
USB = "CFG_USBINPROT_RTCM3X"


def _sim(uart1: int = 0, uart2: int = 0, usb: int = 0) -> SimReceiver:
    return SimReceiver({UART1: uart1, UART2: uart2, USB: usb})


class TestBegin:
    def test_enables_rtcm3_input_on_the_console_port_only(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)

        driver.begin_correction_input(PortId.UART2)

        assert sim.ram == {UART1: 0, UART2: 1, USB: 0}

    def test_enables_every_port_when_the_console_port_is_unknown(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)

        driver.begin_correction_input(None)

        assert sim.ram == {UART1: 1, UART2: 1, USB: 1}

    def test_writes_ram_only_never_bbr_or_flash(self) -> None:
        sim = _sim()
        driver = connected_driver(sim)

        driver.begin_correction_input(None)
        driver.end_correction_input()

        assert sim.valsets
        assert all(m.ram == 1 and not m.bbr and not m.flash for m in sim.valsets)


class TestEnd:
    def test_restores_the_settings_begin_found(self) -> None:
        sim = _sim(uart1=1, uart2=0, usb=0)
        driver = connected_driver(sim)
        driver.begin_correction_input(None)

        driver.end_correction_input()

        assert sim.ram == {UART1: 1, UART2: 0, USB: 0}

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
