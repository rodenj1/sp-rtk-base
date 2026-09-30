"""Build real RTCM 3 MSM4 / MSM7 Frames for tests.

Encodes the header, masks and satellite / signal fields bit by bit
(RTCM 10403.x layout) and wraps them in a CRC-24Q frame, so tests feed
the Signal Quality MSM adapter exactly what a receiver sends.  Only the
C/N0 fields carry chosen values; ranges and phases are zero.
"""

from __future__ import annotations

from sp_rtk_base_relay import Frame
from sp_rtk_base_relay.rtcm_decoder import RTCMMessageDecoder


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

    payload = b.to_bytes()
    body = bytes([0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF]) + payload
    crc = RTCMMessageDecoder.calc_crc24q(body)
    data = body + bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])
    return Frame(message_id=message_number, data=data)


def other_frame(message_number: int = 1005) -> Frame:
    """A CRC-valid non-MSM Frame (e.g. 1005 base position)."""
    b = _Bits()
    b.put(message_number, 12)
    b.put(0, 140)
    payload = b.to_bytes()
    body = bytes([0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF]) + payload
    crc = RTCMMessageDecoder.calc_crc24q(body)
    return Frame(
        message_id=message_number,
        data=body + bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF]),
    )
