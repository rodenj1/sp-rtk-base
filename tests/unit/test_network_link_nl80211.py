"""nl80211 Wi-Fi readings, against replies recorded on test-base.

``tests/fixtures/nl80211/nl80211-fixtures.json`` holds raw generic
netlink replies captured read-only on test-base (Pi 3B+, kernel 6.18,
brcmfmac), one ``recv()`` datagram per hex string, with the SSID
("Test"), BSSID (02:00:00:00:00:01) and MAC scrubbed.  The AP-mode
interface and the station without a signal weren't captured: they are
built here from the recorded messages by changing one attribute.
"""

from __future__ import annotations

import errno
import json
import struct
from pathlib import Path

import pytest

from sp_rtk_base.models.network_link_models import StationReading, WifiReading
from sp_rtk_base.services.network_link import nl80211

_FIXTURES = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "nl80211" / "nl80211-fixtures.json"
    ).read_text()
)
FAMILY_ID: int = _FIXTURES["nl80211_family_id"]
BSSID = "02:00:00:00:00:01"


def _replies(name: str) -> list[bytes]:
    return [bytes.fromhex(datagram) for datagram in _FIXTURES[name]["reply_hex"]]


def _attr(kind: int, payload: bytes) -> bytes:
    raw = struct.pack("HH", 4 + len(payload), kind) + payload
    return raw + b"\0" * (-len(raw) % 4)


def _genl(family: int, cmd: int, attrs: bytes, flags: int = 0) -> bytes:
    body = struct.pack("BBH", cmd, 1, 0) + attrs
    return struct.pack("IHHII", 16 + len(body), family, flags, 1, 0) + body


def _rewritten(datagram: bytes, edit: dict[int, bytes | None]) -> bytes:
    """*datagram*'s first genl message with top-level attributes replaced
    (or, for ``None``, removed), re-encoded with correct lengths."""
    length, family, flags, _seq, _pid = struct.unpack_from("IHHII", datagram)
    cmd = datagram[16]
    attrs = b""
    offset = 20
    while offset + 4 <= length:
        size, kind = struct.unpack_from("HH", datagram, offset)
        payload = datagram[offset + 4 : offset + size]
        if (kind & 0x3FFF) in edit:
            payload = edit[kind & 0x3FFF]
        if payload is not None:
            attrs += _attr(kind, payload)
        offset += (size + 3) & ~3
    return _genl(family, cmd, attrs, flags)


def _nested(datagram: bytes, kind: int) -> bytes:
    """The payload of top-level attribute *kind* of *datagram*'s first message."""
    offset = 20
    while True:
        size, found = struct.unpack_from("HH", datagram, offset)
        if found & 0x3FFF == kind:
            return datagram[offset + 4 : offset + size]
        offset += (size + 3) & ~3


def _without(nest: bytes, kind: int) -> bytes:
    out = b""
    offset = 0
    while offset + 4 <= len(nest):
        size, found = struct.unpack_from("HH", nest, offset)
        if found & 0x3FFF != kind:
            out += _attr(found, nest[offset + 4 : offset + size])
        offset += (size + 3) & ~3
    return out


def _ap_mode_interface() -> list[bytes]:
    station, ack = _replies("get_interface_wlan0_station_mode")
    return [_rewritten(station, {5: struct.pack("I", 3)}), ack]


def _station_without_signal() -> list[bytes]:
    station, ack = _replies("get_station_wlan0_bssid")
    info = _without(_nested(station, 21), 7)
    return [_rewritten(station, {21: info}), ack]


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def test_the_family_lookup_gives_the_nl80211_family_id() -> None:
    assert nl80211.parse_family_id(_replies("ctrl_getfamily_nl80211")) == FAMILY_ID


def test_an_interface_in_station_mode_reads_its_mode_frequency_and_ssid() -> None:
    reading = nl80211.parse_interface(_replies("get_interface_wlan0_station_mode"))

    assert reading == WifiReading(
        interface="wlan0", mode="station", wiphy=0, frequency_mhz=5745, ssid="Test"
    )


def test_an_interface_in_ap_mode_reads_as_an_access_point() -> None:
    reading = nl80211.parse_interface(_ap_mode_interface())

    assert reading.mode == "ap"


def test_the_station_reads_its_signal_bitrates_and_failed_sends() -> None:
    station = nl80211.parse_station(_replies("get_station_wlan0_bssid"))

    assert station == StationReading(
        bssid=BSSID,
        signal_dbm=-55,
        tx_bitrate_bps=325_000_000,
        rx_bitrate_bps=433_300_000,
        tx_failed=11,
    )


def test_a_station_without_a_signal_reads_everything_else() -> None:
    station = nl80211.parse_station(_station_without_signal())

    assert station is not None
    assert station.signal_dbm is None
    assert station.tx_bitrate_bps == 325_000_000


def test_the_station_dump_gives_the_associated_bssid() -> None:
    assert nl80211.parse_station_bssid(_replies("get_station_dump_wlan0")) == BSSID


def test_an_empty_station_dump_means_not_associated() -> None:
    done = _replies("get_station_dump_wlan0")[1]

    assert nl80211.parse_station_bssid([done]) is None


def test_an_error_reply_raises_its_errno() -> None:
    with pytest.raises(OSError) as raised:
        nl80211.parse_interface(_replies("get_interface_eth0_enodev_error"))

    assert raised.value.errno == errno.ENODEV


# ---------------------------------------------------------------------------
# WifiReader: the requests in order, against a fake netlink exchange
# ---------------------------------------------------------------------------

_DUMP = 0x300
_DONE = _replies("get_station_dump_wlan0")[1]


def _scan_result(bssid: bytes, status: int | None) -> bytes:
    bss = _attr(1, bssid)
    if status is not None:
        bss += _attr(9, struct.pack("I", status))
    return _genl(FAMILY_ID, 34, _attr(3, struct.pack("I", 3)) + _attr(47, bss), 0x2)


class FakeNl80211:
    """Answers the requests test-base's brcmfmac answered, from the fixtures.

    ``interfaces`` maps ifindex to the ``GET_INTERFACE`` reply (or the
    errno it fails with); ``dump`` is the station dump's reply, or the
    errno it fails with; ``scan`` the ``GET_SCAN`` dump's reply.
    """

    def __init__(
        self,
        interfaces: dict[int, list[bytes] | int],
        dump: list[bytes] | int | None = None,
        scan: list[bytes] | None = None,
    ) -> None:
        self.interfaces = interfaces
        self.dump = _replies("get_station_dump_wlan0") if dump is None else dump
        self.scan = scan or [_DONE]
        self.commands: list[tuple[int, bool]] = []

    def __call__(self, kind: int, flags: int, body: bytes) -> list[bytes]:
        if kind == 0x10:
            return _replies("ctrl_getfamily_nl80211")
        assert kind == FAMILY_ID
        cmd = body[0]
        attrs = _attrs(body[4:])  # after the genl header
        dump = bool(flags & _DUMP)
        self.commands.append((cmd, dump))
        if cmd == 32:
            assert dump  # GET_SCAN is only ever the cached-results dump
            return self.scan
        if cmd == 5:
            reply = self.interfaces[struct.unpack("I", attrs[3])[0]]
            if isinstance(reply, int):
                raise OSError(reply, "nl80211 error")
            return reply
        assert cmd == 17
        if dump:
            if isinstance(self.dump, int):
                raise OSError(self.dump, "nl80211 error")
            return self.dump
        assert attrs[6] == bytes.fromhex("020000000001")
        return _replies("get_station_wlan0_bssid")


def _attrs(buffer: bytes) -> dict[int, bytes]:
    found: dict[int, bytes] = {}
    offset = 0
    while offset + 4 <= len(buffer):
        size, kind = struct.unpack_from("HH", buffer, offset)
        found[kind & 0x3FFF] = buffer[offset + 4 : offset + size]
        offset += (size + 3) & ~3
    return found


_INDEX = {"wlan0": 3, "wlan1": 4}.__getitem__


def test_an_associated_interface_reads_its_info_and_its_station() -> None:
    fake = FakeNl80211({3: _replies("get_interface_wlan0_station_mode")})

    (reading,) = nl80211.WifiReader(fake, _INDEX)(["wlan0"])

    assert reading.mode == "station"
    assert reading.frequency_mhz == 5745
    assert reading.station is not None
    assert reading.station.signal_dbm == -55


def test_the_station_info_is_read_for_the_bssid_not_from_the_dump() -> None:
    fake = FakeNl80211({3: _replies("get_interface_wlan0_station_mode")})

    nl80211.WifiReader(fake, _INDEX)(["wlan0"])

    assert fake.commands[-1] == (17, False)


def test_firmware_that_rejects_the_station_dump_gets_the_bssid_from_the_scan_cache() -> (
    None
):
    fake = FakeNl80211(
        {3: _replies("get_interface_wlan0_station_mode")},
        dump=errno.EOPNOTSUPP,
        scan=[
            _scan_result(bytes.fromhex("020000000009"), None),
            _scan_result(bytes.fromhex("020000000001"), 1),
            _DONE,
        ],
    )

    (reading,) = nl80211.WifiReader(fake, _INDEX)(["wlan0"])

    assert reading.station is not None
    assert reading.station.bssid == BSSID


def test_an_unassociated_interface_has_no_station() -> None:
    fake = FakeNl80211({3: _replies("get_interface_wlan0_station_mode")}, dump=[_DONE])

    (reading,) = nl80211.WifiReader(fake, _INDEX)(["wlan0"])

    assert reading.station is None
    assert (17, False) not in fake.commands


def test_an_access_point_interface_asks_for_no_station() -> None:
    fake = FakeNl80211({3: _ap_mode_interface()})

    (reading,) = nl80211.WifiReader(fake, _INDEX)(["wlan0"])

    assert (reading.mode, reading.station) == ("ap", None)
    assert all(cmd != 17 for cmd, _ in fake.commands)


def test_an_interface_nl80211_fails_for_is_left_out_and_the_others_read() -> None:
    fake = FakeNl80211(
        {3: errno.ENODEV, 4: _replies("get_interface_wlan0_station_mode")}
    )

    readings = nl80211.WifiReader(fake, _INDEX)(["wlan0", "wlan1"])

    assert len(readings) == 1


def test_no_nl80211_family_fails_the_whole_read() -> None:
    def no_family(kind: int, flags: int, body: bytes) -> list[bytes]:
        raise OSError(errno.ENOENT, "no such family")

    with pytest.raises(OSError):
        nl80211.WifiReader(no_family, _INDEX)(["wlan0"])


def test_no_wifi_interfaces_reads_nothing_and_asks_nothing() -> None:
    def unused(kind: int, flags: int, body: bytes) -> list[bytes]:
        raise AssertionError("no request expected")

    assert nl80211.WifiReader(unused, _INDEX)([]) == ()
