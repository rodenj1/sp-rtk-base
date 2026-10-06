"""Which baud fields the GPS config page locks over Bluetooth (rtk_development#47).

The page disables the baud field of the UART the Bluetooth module is on,
or every UART field while the Console port is unknown, with a note naming
the rate. ``console_baud_lock`` is the pure decision behind it.
"""

from __future__ import annotations

from sp_rtk_base.models.device_models import (
    BluetoothLink,
    ConsolePortUnknownReason,
    DeviceConnectionState,
    DeviceStatus,
    PortId,
    SerialLink,
)
from sp_rtk_base.models.profile_models import BaudAssertion
from sp_rtk_base.ui.pages.gps_config import console_baud_lock

LIVE = BaudAssertion(uart1=38400, uart2=115200)
BLUETOOTH = BluetoothLink(device_name="RTK_BASE_TST", mac="00:11:22:33:44:55")


def _status(**kw: object) -> DeviceStatus:
    return DeviceStatus(state=DeviceConnectionState.CONNECTED, **kw)  # type: ignore[arg-type]


def test_over_bluetooth_locks_the_console_ports_field_naming_its_rate() -> None:
    lock = console_baud_lock(_status(link=BLUETOOTH, console_port=PortId.UART2), LIVE)

    assert lock is not None
    assert lock.uarts == ("uart2",)
    assert "UART2" in lock.note
    assert "115200" in lock.note
    assert "serial cable" in lock.note


def test_over_bluetooth_with_the_console_port_unknown_locks_both_fields() -> None:
    lock = console_baud_lock(
        _status(
            link=BLUETOOTH,
            console_port=None,
            console_port_unknown_reason=ConsolePortUnknownReason.NO_ANSWER,
        ),
        LIVE,
    )

    assert lock is not None
    assert lock.uarts == ("uart1", "uart2")
    assert "38400" in lock.note
    assert "115200" in lock.note
    assert "unknown" in lock.note


def test_over_serial_nothing_is_locked() -> None:
    serial = SerialLink(port="/dev/ttyUSB1", baud_rate=38400)

    assert (
        console_baud_lock(_status(link=serial, console_port=PortId.UART2), LIVE) is None
    )


def test_disconnected_nothing_is_locked() -> None:
    assert console_baud_lock(DeviceStatus(), LIVE) is None
