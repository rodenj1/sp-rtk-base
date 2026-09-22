"""Console-port attribution from UBX-MON-COMMS counters (ADR 0003).

Pure: given two MON-COMMS snapshots taken either side of a burst of UBX
polls, decide which receiver port the burst arrived on — the **console
port** (see ``CONTEXT.md``). ``UbloxDriver.identify_console_port`` does
the I/O; everything decision-shaped lives here, so it is tested against
the real counter deltas captured on the bench (issue #153).

Every clause of the rule exists because a simpler one failed. Do not
"simplify" it without re-running ``tools/probe_console_port.py`` on
hardware — see ADR 0003 for each failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from sp_rtk_base.models.device_models import (
    ConsolePortReading,
    ConsolePortUnknownReason,
    PortId,
)

#: Integration Manual UBX-18010802 R16 §3.8 Table 27 — the ``portId``
#: values MON-COMMS reports. UART2 is ``0x0201``, **not** ``0x0200``:
#: u-blox's own ubxlib encodes ``port << 8`` with a ``+1`` special case
#: for UART2, and an equality table built from the obvious pattern
#: silently never matches it. ``0x0101`` and ``0x0200`` are *Reserved* —
#: and live with heavy internal traffic on HPG 1.51.
TABLE_27_PORT_IDS: frozenset[int] = frozenset({0x0000, 0x0100, 0x0201, 0x0300, 0x0400})

#: The Table 27 ports a host serial link can actually be attached to.
#: I2C (``0x0000``) and SPI (``0x0400``) are in the table but cannot
#: carry a host serial device, so an answer naming them is unusable and
#: reads as unknown rather than extending ``PortId`` (ADR 0003).
_CONSOLE_CAPABLE: dict[int, PortId] = {
    0x0100: PortId.UART1,
    0x0201: PortId.UART2,
    0x0300: PortId.USB,
}


@dataclass(frozen=True)
class PortCounters:
    """One port's cumulative counters from a single MON-COMMS snapshot."""

    rx_bytes: int  # U4, "bytes ever received"
    ubx_msgs: int  # U2, msgs[0] — the UBX protocol slot


def attribute_console_port(
    before: dict[int, PortCounters],
    after: dict[int, PortCounters],
    bytes_written: int,
    frames_written: int,
) -> ConsolePortReading:
    """Name the port the probe arrived on, or say why it cannot.

    A port carries the probe's fingerprint when **both** its ``rxBytes``
    rose by at least ``bytes_written`` **and** its UBX message count rose
    by at least ``frames_written``. "At least", never "exactly": the
    MON-COMMS polls themselves land on the console port too, and their
    ordering against the snapshot is undocumented. Bytes alone are not
    enough: the *Reserved* ports clear any byte threshold on internal
    traffic.

    Only Table 27 ports are candidates. Keyed on ``portId`` throughout —
    a real receiver reported ``nPorts=4`` on a five-port module — and a
    port present in only one snapshot is ignored rather than guessed at.
    Deltas are modular so a wrapped counter still attributes.

    Returns:
        ``known(port)`` for exactly one console-capable Table 27 match;
        otherwise ``unknown`` with the reason (see
        :class:`ConsolePortUnknownReason`).
    """
    fingerprinted: list[int] = []
    for port_id, b in before.items():
        a = after.get(port_id)
        if a is None:
            continue
        rx = (a.rx_bytes - b.rx_bytes) % 2**32
        ubx = (a.ubx_msgs - b.ubx_msgs) % 2**16
        if rx >= bytes_written and ubx >= frames_written:
            fingerprinted.append(port_id)

    in_table = [p for p in fingerprinted if p in TABLE_27_PORT_IDS]

    if len(in_table) > 1:
        return ConsolePortReading.unknown(ConsolePortUnknownReason.AMBIGUOUS)
    if len(in_table) == 1:
        port = _CONSOLE_CAPABLE.get(in_table[0])
        if port is None:
            # I2C or SPI: a clear answer, but not one a host serial link
            # can be attached to — our model has no use for it.
            return ConsolePortReading.unknown(ConsolePortUnknownReason.UNRECOGNISED)
        return ConsolePortReading.known(port)
    if fingerprinted:
        # Only a portId outside Table 27 carried it: a decode problem on
        # our side, which must not read like the receiver saying no.
        return ConsolePortReading.unknown(ConsolePortUnknownReason.UNRECOGNISED)
    return ConsolePortReading.unknown(ConsolePortUnknownReason.NO_ANSWER)
