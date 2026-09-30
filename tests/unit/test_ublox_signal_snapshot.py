"""u-blox Signal Snapshot: NAV-SIG + NAV-SAT → vendor-neutral signals.

The driver is fed real pyubx2 messages (built, serialised and parsed
back, so field names and types match the receiver's), interleaved with
unrelated traffic, through a mocked serial port and reader.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import struct
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock

import pytest
from pyubx2 import GET, POLL, SET, UBXMessage, UBXReader
from pyubx2.ubxtypes_configdb import UBX_CONFIG_DATABASE

from sp_rtk_base.models.device_models import GnssConstellation
from sp_rtk_base.models.signal_quality_models import Band
from sp_rtk_base.services.drivers.ublox import UbloxDriver

GPS, SBAS, GAL, BDS, QZSS, GLO = 0, 1, 2, 3, 5, 6


def _parsed(msg: UBXMessage) -> Any:
    return UBXReader(BytesIO(msg.serialize())).read()[1]


def _min_elevation(deg: int) -> Any:
    key, _ = UBX_CONFIG_DATABASE["CFG_NAVSPG_INFIL_MINELEV"]
    payload = bytes([1, 0, 0, 0]) + struct.pack("<Ib", key, deg)
    return _parsed(UBXMessage("CFG", "CFG-VALGET", GET, payload=payload))


def _nav_sat(sats: list[tuple[int, int, int, int]]) -> Any:
    """(gnssId, svId, cno, elev) per satellite."""
    fields: dict[str, int] = {"iTOW": 0, "version": 1, "numSvs": len(sats)}
    for i, (gnss, sv, cno, elev) in enumerate(sats, start=1):
        fields |= {
            f"gnssId_{i:02d}": gnss,
            f"svId_{i:02d}": sv,
            f"cno_{i:02d}": cno,
            f"elev_{i:02d}": elev,
        }
    return _parsed(UBXMessage("NAV", "NAV-SAT", GET, **fields))


def _nav_sig(sigs: list[tuple[int, int, int, int]]) -> Any:
    """(gnssId, svId, sigId, cno) per signal."""
    fields: dict[str, int] = {"iTOW": 0, "version": 0, "numSigs": len(sigs)}
    for i, (gnss, sv, sig, cno) in enumerate(sigs, start=1):
        fields |= {
            f"gnssId_{i:02d}": gnss,
            f"svId_{i:02d}": sv,
            f"sigId_{i:02d}": sig,
            f"cno_{i:02d}": cno,
        }
    return _parsed(UBXMessage("NAV", "NAV-SIG", GET, **fields))


class _Unrelated:
    """Interleaved traffic the driver must skip (e.g. an RTCM frame)."""

    identity = "1074"


def _driver(responses: list[Any]) -> tuple[UbloxDriver, MagicMock]:
    driver = UbloxDriver()
    ser = MagicMock()
    ser.is_open = True
    reader = MagicMock()
    reader.read.side_effect = [(b"", r) for r in responses] + [(b"", None)] * 200
    driver._serial = ser
    driver._reader = reader
    return driver, ser


SKY_ELEVATIONS = [
    (GPS, 5, 45, 40),
    (GPS, 7, 0, 25),  # tracked but no signal
    (GAL, 11, 44, 30),
    (BDS, 20, 43, 50),
    (GLO, 9, 46, 9),  # below the 15° mask
    (QZSS, 3, 40, 60),
    (SBAS, 131, 49, 45),  # never in MSM, so never in a Snapshot
]
SKY_SIGNALS = [
    (GPS, 5, 0, 45),
    (GPS, 5, 3, 42),  # L1C/A, L2CL
    (GPS, 7, 0, 0),  # cno 0: not tracked
    (GAL, 11, 0, 44),
    (GAL, 11, 5, 40),
    (GAL, 11, 3, 41),  # E1C, E5bI, E5aI
    (BDS, 20, 0, 43),
    (BDS, 20, 2, 41),  # B1I D1, B2I D1
    (GLO, 9, 0, 46),  # below mask
    (QZSS, 3, 0, 40),
    (QZSS, 3, 5, 38),  # L1C/A, L2CL
    (SBAS, 131, 0, 49),
]


def test_signals_are_joined_banded_and_filtered_to_above_the_mask() -> None:
    driver, _ = _driver(
        [
            _min_elevation(15),
            _Unrelated(),
            _nav_sat(SKY_ELEVATIONS),
            _Unrelated(),
            _nav_sig(SKY_SIGNALS),
        ]
    )

    snapshot = driver.get_signal_snapshot()

    got = {(s.constellation, s.satellite, s.band, s.cn0_dbhz) for s in snapshot.signals}
    assert got == {
        (GnssConstellation.GPS, 5, Band.L1, 45.0),
        (GnssConstellation.GPS, 5, Band.L2, 42.0),
        (GnssConstellation.GALILEO, 11, Band.L1, 44.0),
        (GnssConstellation.GALILEO, 11, Band.L2, 40.0),
        (GnssConstellation.GALILEO, 11, Band.OTHER, 41.0),
        (GnssConstellation.BEIDOU, 20, Band.L1, 43.0),
        (GnssConstellation.BEIDOU, 20, Band.L2, 41.0),
        (GnssConstellation.QZSS, 3, Band.L1, 40.0),
        (GnssConstellation.QZSS, 3, Band.L2, 38.0),
    }


def _written(ser: MagicMock) -> list[str]:
    """Identities of the UBX messages the driver wrote to the port."""
    out: list[str] = []
    for call in ser.write.call_args_list:
        for mode in (POLL, SET):  # polls, and configuration writes
            try:
                parsed = UBXReader(BytesIO(call.args[0]), msgmode=mode).read()[1]
            except Exception:
                continue
            if parsed is not None:
                out.append(parsed.identity)
                break
    return out


def test_the_elevation_mask_is_read_once_until_the_configuration_changes() -> None:
    ack = type("Ack", (), {"identity": "ACK-ACK"})()
    driver, ser = _driver(
        [
            _min_elevation(15),
            _nav_sat(SKY_ELEVATIONS),
            _nav_sig(SKY_SIGNALS),
            _nav_sat(SKY_ELEVATIONS),
            _nav_sig(SKY_SIGNALS),
            ack,  # the configuration write
            _min_elevation(20),
            _nav_sat(SKY_ELEVATIONS),
            _nav_sig(SKY_SIGNALS),
        ]
    )

    driver.get_signal_snapshot()
    driver.get_signal_snapshot()
    # Every configuration write (e.g. configure_optimisations changing the
    # mask) goes through this one sender.
    driver._send_cfg_valset([("CFG_NAVSPG_INFIL_MINELEV", 20)])
    third = driver.get_signal_snapshot()

    assert [i for i in _written(ser) if i != "CFG-VALSET"] == [
        "CFG-VALGET",
        "NAV-SAT",
        "NAV-SIG",
        "NAV-SAT",
        "NAV-SIG",
        "CFG-VALGET",
        "NAV-SAT",
        "NAV-SIG",
    ]
    # With the mask now 20°, GAL 11 (30°) stays; GPS 5 (40°) stays.
    assert {s.satellite for s in third.signals} == {5, 11, 20, 3}


def test_no_nav_reply_is_an_error_not_an_empty_snapshot() -> None:
    driver, _ = _driver(
        [_min_elevation(15)]
    )  # the receiver never answers the NAV polls

    with pytest.raises(RuntimeError, match="No NAV-SAT response"):
        driver.get_signal_snapshot()
