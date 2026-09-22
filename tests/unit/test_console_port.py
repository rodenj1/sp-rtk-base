"""Tests for console-port identification (issue #155, ADR 0003).

The **console port** is the receiver port this application's own link is
attached to, as the receiver sees it (see ``CONTEXT.md``). These tests
cover the seams agreed for #155: the pure attribution rule, the u-blox
driver's MON-COMMS read, and the service contract around it.

The attribution cases replay counter deltas taken from real hardware on
the bench (issue #153): two ZED-F9Ps on HPG 1.51, where two Table 27
*Reserved* ports carry heavy internal traffic.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sp_rtk_base.models.device_models import (
    ConsolePortReading,
    ConsolePortUnknownReason,
    PortId,
)
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.base import GpsReceiverDriver
from sp_rtk_base.services.drivers.fake import FAKE_CONSOLE_UNKNOWN_PORT, FakeGpsDriver
from sp_rtk_base.services.drivers.ublox import UbloxDriver
from sp_rtk_base.services.drivers.ublox_console_port import (
    PortCounters,
    attribute_console_port,
)

# The bench probe: 16 NAV-STATUS polls, 8 bytes each.
BYTES, FRAMES = 128, 16

UART1, UART2, RES_0101, RES_0200, USB, I2C, SPI = (
    0x0100,
    0x0201,
    0x0101,
    0x0200,
    0x0300,
    0x0000,
    0x0400,
)


def _snap(
    **deltas: tuple[int, int],
) -> tuple[dict[int, PortCounters], dict[int, PortCounters]]:
    """Build before/after snapshots from ``name=(rx_delta, ubx_delta)``."""
    ids = {
        "uart1": UART1,
        "uart2": UART2,
        "r0101": RES_0101,
        "r0200": RES_0200,
        "usb": USB,
        "i2c": I2C,
        "spi": SPI,
    }
    before = {ids[k]: PortCounters(rx_bytes=1000, ubx_msgs=50) for k in deltas}
    after = {
        ids[k]: PortCounters(rx_bytes=1000 + rx, ubx_msgs=50 + ubx)
        for k, (rx, ubx) in deltas.items()
    }
    return before, after


class TestAttributionOnRealBenchNumbers:
    def test_the_base_run_identifies_uart1(self) -> None:
        """ttyUSB0 @ 57600, run 1: busy Reserved ports must not confuse it."""
        before, after = _snap(
            uart1=(136, 17), r0101=(6074, 24), r0200=(5532, 18), uart2=(0, 0)
        )

        assert attribute_console_port(before, after, BYTES, FRAMES) == (
            ConsolePortReading.known(PortId.UART1)
        )

    def test_the_rover_run_identifies_uart1(self) -> None:
        """ttyUSB1 @ 115200: Reserved 0x0200 took +54 KB and +144 UBX."""
        before, after = _snap(
            uart1=(136, 17), r0101=(7456, 48), r0200=(54384, 144), uart2=(0, 0)
        )

        assert attribute_console_port(before, after, BYTES, FRAMES).port is PortId.UART1

    def test_bytes_alone_are_not_enough(self) -> None:
        """A Table 27 port that moved bytes but not UBX messages is noise."""
        before, after = _snap(uart1=(136, 17), uart2=(5000, 0))

        assert attribute_console_port(before, after, BYTES, FRAMES).port is PortId.UART1


class TestTheUart2Trap:
    def test_uart2_is_0x0201(self) -> None:
        """The obvious bank<<8 table maps 0x0200 and never matches UART2."""
        before, after = _snap(uart2=(136, 17), uart1=(0, 0))

        assert attribute_console_port(before, after, BYTES, FRAMES).port is PortId.UART2

    def test_a_reserved_port_alone_is_unrecognised_not_a_no(self) -> None:
        """A decode miss is our bug and must not read like a receiver 'no'."""
        before, after = _snap(r0200=(900, 40), uart1=(0, 0))

        reading = attribute_console_port(before, after, BYTES, FRAMES)
        assert reading.unknown_reason is ConsolePortUnknownReason.UNRECOGNISED


class TestUnknownOutcomes:
    def test_nothing_carrying_the_fingerprint_is_no_answer(self) -> None:
        before, after = _snap(uart1=(0, 0), uart2=(0, 0))

        reading = attribute_console_port(before, after, BYTES, FRAMES)
        assert reading.port is None
        assert reading.unknown_reason is ConsolePortUnknownReason.NO_ANSWER

    def test_two_table_ports_carrying_it_is_ambiguous(self) -> None:
        before, after = _snap(uart1=(136, 17), uart2=(136, 17))

        reading = attribute_console_port(before, after, BYTES, FRAMES)
        assert reading.unknown_reason is ConsolePortUnknownReason.AMBIGUOUS

    def test_an_i2c_answer_is_unknown(self) -> None:
        """A host serial device cannot be attached to I2C (ADR 0003)."""
        before, after = _snap(i2c=(136, 17), uart1=(0, 0))

        reading = attribute_console_port(before, after, BYTES, FRAMES)
        assert reading.port is None
        assert reading.unknown_reason is ConsolePortUnknownReason.UNRECOGNISED

    def test_an_spi_answer_is_unknown(self) -> None:
        before, after = _snap(spi=(136, 17), uart1=(0, 0))

        assert attribute_console_port(before, after, BYTES, FRAMES).port is None


class TestCounterMechanics:
    def test_a_wrapped_u4_counter_still_attributes(self) -> None:
        before = {UART1: PortCounters(rx_bytes=2**32 - 10, ubx_msgs=65530)}
        after = {UART1: PortCounters(rx_bytes=126, ubx_msgs=11)}

        assert attribute_console_port(before, after, BYTES, FRAMES).port is PortId.UART1

    def test_a_port_in_only_one_snapshot_is_ignored_not_guessed(self) -> None:
        before = {UART1: PortCounters(rx_bytes=0, ubx_msgs=0)}
        after = {
            UART1: PortCounters(rx_bytes=136, ubx_msgs=17),
            USB: PortCounters(rx_bytes=9999, ubx_msgs=99),
        }

        assert attribute_console_port(before, after, BYTES, FRAMES).port is PortId.UART1


class TestAPortThatVanishes:
    def test_a_port_missing_from_the_second_snapshot_is_ignored(self) -> None:
        before = {
            UART1: PortCounters(rx_bytes=0, ubx_msgs=0),
            UART2: PortCounters(rx_bytes=0, ubx_msgs=0),
        }
        after = {UART1: PortCounters(rx_bytes=136, ubx_msgs=17)}

        assert attribute_console_port(before, after, BYTES, FRAMES).port is PortId.UART1


class TestReadingModel:
    def test_known_has_no_reason(self) -> None:
        reading = ConsolePortReading.known(PortId.USB)

        assert reading.is_known
        assert reading.unknown_reason is None

    def test_unknown_has_no_port(self) -> None:
        reading = ConsolePortReading.unknown(ConsolePortUnknownReason.UNSUPPORTED)

        assert not reading.is_known
        assert reading.port is None


# ---------------------------------------------------------------------------
# Seam 2 — the drivers
# ---------------------------------------------------------------------------


def _mon_comms(**ports: tuple[int, int]) -> SimpleNamespace:
    """A parsed MON-COMMS, shaped as pyubx2 hands it over.

    ``ports`` maps a portId (as ``p0x0100=``) to ``(rx_bytes, ubx_msgs)``.
    """
    fields: dict[str, object] = {"identity": "MON-COMMS", "nPorts": len(ports)}
    for i, (key, (rx, ubx)) in enumerate(ports.items(), start=1):
        n = f"{i:02d}"
        fields[f"portId_{n}"] = int(key[1:], 16)
        fields[f"rxBytes_{n}"] = rx
        fields[f"msgs_{n}_01"] = ubx
    return SimpleNamespace(**fields)


def _mon_ver() -> SimpleNamespace:
    return SimpleNamespace(
        identity="MON-VER",
        swVersion="EXT CORE 1.00",
        hwVersion="00190000",
        extension_00="FWVER=HPG 1.51",
        extension_01="PROTVER=27.50",
        extension_02="MOD=ZED-F9P",
        extension_03=None,
    )


class TestUbloxIdentifiesTheConsolePort:
    @pytest.fixture(autouse=True)
    def _mock_fcntl(self) -> Iterator[None]:
        with patch("sp_rtk_base.services.drivers.ublox.fcntl.flock"):
            yield

    def _connected(
        self, mock_serial_cls: MagicMock, mock_reader_cls: MagicMock
    ) -> tuple[UbloxDriver, MagicMock, MagicMock]:
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser
        reader = MagicMock()
        reader.read.return_value = (b"", _mon_ver())
        mock_reader_cls.return_value = reader
        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0", 57600)
        return driver, ser, reader

    @patch("sp_rtk_base.services.drivers.ublox.time.sleep")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_reports_the_port_that_carried_the_probe(
        self, mock_serial_cls: MagicMock, mock_reader_cls: MagicMock, _sleep: MagicMock
    ) -> None:
        driver, _, reader = self._connected(mock_serial_cls, mock_reader_cls)
        reader.read.side_effect = [
            (b"", _mon_comms(p0100=(1000, 50), p0200=(0, 0), p0201=(0, 0))),
            (b"", _mon_comms(p0100=(1136, 67), p0200=(60000, 90), p0201=(0, 0))),
        ]

        assert driver.identify_console_port() == ConsolePortReading.known(PortId.UART1)

    @patch("sp_rtk_base.services.drivers.ublox.time.sleep")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_writes_a_burst_of_well_formed_ubx_polls(
        self, mock_serial_cls: MagicMock, mock_reader_cls: MagicMock, _sleep: MagicMock
    ) -> None:
        """Well-formed frames, so the UBX count corroborates the bytes."""
        driver, ser, reader = self._connected(mock_serial_cls, mock_reader_cls)
        reader.read.side_effect = [
            (b"", _mon_comms(p0100=(0, 0))),
            (b"", _mon_comms(p0100=(136, 17))),
        ]
        ser.write.reset_mock()

        driver.identify_console_port()

        burst = [c.args[0] for c in ser.write.call_args_list if len(c.args[0]) > 8]
        assert len(burst) == 1
        assert len(burst[0]) == 16 * 8
        assert burst[0].startswith(b"\xb5\x62")

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_a_receiver_that_never_answers_mon_comms_is_unsupported(
        self, mock_serial_cls: MagicMock, mock_reader_cls: MagicMock
    ) -> None:
        driver, _, reader = self._connected(mock_serial_cls, mock_reader_cls)
        reader.read.side_effect = None
        reader.read.return_value = (b"", None)

        reading = driver.identify_console_port()

        assert reading.unknown_reason is ConsolePortUnknownReason.UNSUPPORTED

    def test_an_unconnected_driver_is_unsupported_not_an_exception(self) -> None:
        reading = UbloxDriver().identify_console_port()

        assert reading.unknown_reason is ConsolePortUnknownReason.UNSUPPORTED


class TestOtherDrivers:
    def test_the_abc_default_is_unknown_unsupported(self) -> None:
        """A driver with no way to ask simply reports unknown."""
        reading = GpsReceiverDriver.identify_console_port(FakeGpsDriver())

        assert reading == ConsolePortReading.unknown(
            ConsolePortUnknownReason.UNSUPPORTED
        )

    def test_the_fake_reports_uart1_like_the_reference_rig(self) -> None:
        driver = FakeGpsDriver()
        driver.connect("FAKE", 57600)

        assert driver.identify_console_port() == ConsolePortReading.known(PortId.UART1)

    def test_the_fake_has_a_sentinel_port_for_unknown(self) -> None:
        driver = FakeGpsDriver()
        driver.connect(FAKE_CONSOLE_UNKNOWN_PORT, 57600)

        assert not driver.identify_console_port().is_known


# ---------------------------------------------------------------------------
# Seam 3 — the service lifecycle
# ---------------------------------------------------------------------------


class _ExplodingIdentifier(FakeGpsDriver):
    """A driver whose identification breaks every rule it was given."""

    def identify_console_port(self) -> ConsolePortReading:
        raise RuntimeError("identification blew up")


class TestServiceLifecycle:
    @pytest.mark.asyncio()
    async def test_connect_identifies_and_caches_the_console_port(self) -> None:
        svc = DeviceService()
        svc.set_driver(FakeGpsDriver())

        await svc.connect("FAKE", 57600)

        assert svc.console_port == ConsolePortReading.known(PortId.UART1)

    @pytest.mark.asyncio()
    async def test_an_unknown_console_port_does_not_fail_connect(self) -> None:
        svc = DeviceService()
        svc.set_driver(FakeGpsDriver())

        await svc.connect(FAKE_CONSOLE_UNKNOWN_PORT, 57600)

        assert svc.is_connected
        assert svc.console_port is not None
        assert svc.console_port.unknown_reason is ConsolePortUnknownReason.NO_ANSWER

    @pytest.mark.asyncio()
    async def test_even_a_raising_driver_does_not_fail_connect(self) -> None:
        """Connect never fails because of identification (ADR 0003)."""
        svc = DeviceService()
        svc.set_driver(_ExplodingIdentifier())

        await svc.connect("FAKE", 57600)

        assert svc.is_connected
        assert svc.console_port == ConsolePortReading.unknown(
            ConsolePortUnknownReason.UNSUPPORTED
        )

    @pytest.mark.asyncio()
    async def test_disconnect_forgets_the_console_port(self) -> None:
        svc = DeviceService()
        svc.set_driver(FakeGpsDriver())
        await svc.connect("FAKE", 57600)

        await svc.disconnect()

        assert svc.console_port is None

    @pytest.mark.asyncio()
    async def test_status_exposes_a_known_port(self) -> None:
        svc = DeviceService()
        svc.set_driver(FakeGpsDriver())
        await svc.connect("FAKE", 57600)

        status = svc.get_status()

        assert status.console_port is PortId.UART1
        assert status.console_port_unknown_reason is None

    @pytest.mark.asyncio()
    async def test_status_exposes_why_a_port_is_unknown(self) -> None:
        """The reason is what makes a refusal actionable."""
        svc = DeviceService()
        svc.set_driver(FakeGpsDriver())
        await svc.connect(FAKE_CONSOLE_UNKNOWN_PORT, 57600)

        status = svc.get_status()

        assert status.console_port is None
        assert status.console_port_unknown_reason is ConsolePortUnknownReason.NO_ANSWER

    def test_status_before_any_connect_has_neither(self) -> None:
        status = DeviceService().get_status()

        assert status.console_port is None
        assert status.console_port_unknown_reason is None
