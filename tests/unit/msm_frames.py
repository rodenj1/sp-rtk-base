"""Build real RTCM 3 MSM4 / MSM7 Frames for tests.

Encodes the header, masks and satellite / signal fields bit by bit
(RTCM 10403.x layout) and wraps them in a CRC-24Q frame, so tests feed
the Signal Quality MSM adapter exactly what a receiver sends.  Only the
C/N0 fields carry chosen values; ranges and phases are zero.
"""

from __future__ import annotations

from collections.abc import Iterable

from sp_rtk_base_relay import Frame
from sp_rtk_base_relay.rtcm_decoder import RTCMMessageDecoder

from sp_rtk_base.models.device_models import GnssConstellation
from sp_rtk_base.models.signal_quality_models import Band, Signal


class _Bits:
    def __init__(self) -> None:
        self.bits: list[int] = []

    def put(self, value: int, width: int) -> None:
        value &= (1 << width) - 1
        self.bits.extend((value >> (width - 1 - i)) & 1 for i in range(width))

    def to_bytes(self) -> bytes:
        padded = self.bits + [0] * (-len(self.bits) % 8)
        return bytes(
            int("".join(map(str, padded[i : i + 8])), 2)
            for i in range(0, len(padded), 8)
        )


def _frame(message_number: int, bits: _Bits) -> Frame:
    """Wrap an RTCM payload in its 0xD3 header and CRC-24Q trailer."""
    payload = bits.to_bytes()
    body = bytes([0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF]) + payload
    crc = RTCMMessageDecoder.calc_crc24q(body)
    trailer = bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])
    return Frame(message_id=message_number, data=body + trailer)


def msm_frame(
    message_number: int,
    cn0: dict[int, dict[int, float]],
    *,
    more_follow: bool = False,
    epoch_ms: int = 0,
) -> Frame:
    """An MSM4 or MSM7 Frame (by *message_number*'s last digit).

    Args:
        message_number: e.g. 1074 (GPS MSM4), 1127 (BeiDou MSM7).
        cn0: satellite ID (1-64) → {MSM signal ID (1-32): C/N0 in dB-Hz}.
        more_follow: the multiple message bit (DF393): 1 while more MSM
            for this epoch follow, 0 on the epoch's last one.
        epoch_ms: epoch time field.
    """
    msm7 = message_number % 10 == 7
    sats = sorted(cn0)
    sigs = sorted({sig for per_sat in cn0.values() for sig in per_sat})
    cells = [(sat, sig) for sat in sats for sig in sigs if sig in cn0[sat]]

    b = _Bits()
    b.put(message_number, 12)  # DF002
    b.put(0, 12)  # DF003 station
    b.put(epoch_ms, 30)  # epoch time
    b.put(int(more_follow), 1)  # DF393 multiple message bit
    b.put(0, 3)  # DF409 IODS
    b.put(0, 7)  # reserved
    b.put(0, 2)  # DF411 clock steering
    b.put(0, 2)  # DF412 external clock
    b.put(0, 1)  # DF417 smoothing
    b.put(0, 3)  # DF418 smoothing interval
    b.put(sum(1 << (64 - s) for s in sats), 64)  # DF394 satellite mask
    b.put(sum(1 << (32 - s) for s in sigs), 32)  # DF395 signal mask
    for sat in sats:  # DF396 cell mask
        for sig in sigs:
            b.put(int(sig in cn0[sat]), 1)

    for _ in sats:
        b.put(0, 8)  # DF397 rough range, integer ms
    if msm7:
        for _ in sats:
            b.put(0, 4)  # extended satellite info
    for _ in sats:
        b.put(0, 10)  # DF398 rough range, mod 1 ms
    if msm7:
        for _ in sats:
            b.put(0, 14)  # DF399 rough phase-range rate

    fine_range, fine_phase, lock = (20, 24, 10) if msm7 else (15, 22, 4)
    for _ in cells:
        b.put(0, fine_range)  # DF405 / DF400
    for _ in cells:
        b.put(0, fine_phase)  # DF406 / DF401
    for _ in cells:
        b.put(0, lock)  # DF407 / DF402 lock time
    for _ in cells:
        b.put(0, 1)  # DF420 half-cycle ambiguity
    for sat, sig in cells:  # DF408 (0.0625 dB-Hz) / DF403 (1 dB-Hz)
        value = cn0[sat][sig]
        b.put(round(value * 16) if msm7 else round(value), 10 if msm7 else 6)
    if msm7:
        for _ in cells:
            b.put(0, 15)  # DF404 fine phase-range rate

    return _frame(message_number, b)


def other_frame(message_number: int = 1005) -> Frame:
    """A CRC-valid non-MSM Frame (e.g. 1005 base position)."""
    b = _Bits()
    b.put(message_number, 12)
    b.put(0, 140)
    return _frame(message_number, b)


# One MSM4 message per constellation, and the MSM signal IDs used for
# each band (1C/2L, 1C/7I, 1C/2C, 2I/7I, 1C/2L).
MSM4 = {
    GnssConstellation.GPS: (1074, {Band.L1: 2, Band.L2: 16}),
    GnssConstellation.GLONASS: (1084, {Band.L1: 2, Band.L2: 8}),
    GnssConstellation.GALILEO: (1094, {Band.L1: 2, Band.L2: 14}),
    GnssConstellation.QZSS: (1114, {Band.L1: 2, Band.L2: 16}),
    GnssConstellation.BEIDOU: (1124, {Band.L1: 2, Band.L2: 14}),
}


def epoch_frames(signals: Iterable[Signal], msm7: bool = False) -> list[Frame]:
    """The MSM Frames a receiver would send for one epoch of *signals*."""
    by_constellation: dict[GnssConstellation, dict[int, dict[int, float]]] = {}
    for s in signals:
        _, sig_ids = MSM4[s.constellation]
        by_constellation.setdefault(s.constellation, {}).setdefault(s.satellite, {})[
            sig_ids[s.band]
        ] = s.cn0_dbhz
    order = [c for c in MSM4 if c in by_constellation]
    frames: list[Frame] = []
    for i, constellation in enumerate(order):
        number = MSM4[constellation][0] + (3 if msm7 else 0)
        frames.append(
            msm_frame(
                number, by_constellation[constellation], more_follow=i < len(order) - 1
            )
        )
    return frames


def sky(satellites: int, l1: float, l2: float | None) -> tuple[Signal, ...]:
    """A mixed-constellation sky: *satellites* satellites with the given C/N0."""
    constellations = [
        GnssConstellation.GPS,
        GnssConstellation.GALILEO,
        GnssConstellation.GLONASS,
        GnssConstellation.BEIDOU,
    ]
    out: list[Signal] = []
    for n in range(satellites):
        c, sv = constellations[n % 4], 1 + n // 4
        out.append(Signal(constellation=c, satellite=sv, band=Band.L1, cn0_dbhz=l1))
        if l2 is not None:
            out.append(Signal(constellation=c, satellite=sv, band=Band.L2, cn0_dbhz=l2))
    return tuple(out)
