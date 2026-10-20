"""Wi-Fi readings from nl80211 over generic netlink, standard library only.

Strictly read-only: interface info (``GET_INTERFACE``) and the station
info for the associated BSSID (``GET_STATION`` doit).  The station dump
is only used to learn the BSSID: older brcmfmac firmware rejects it
(``EOPNOTSUPP``), so then the BSSID comes from the cached scan results
(the ``GET_SCAN`` dump, which reads the cache and never starts a scan).
No scans, no surveys (a noise survey retunes the radio).

The parsers are pure and tested against replies recorded on test-base.
Every attribute is optional: brcmfmac leaves out what its firmware
doesn't report.  :class:`WifiReader` runs the requests through an
``exchange`` (a :class:`~.netlink.NetlinkSocket`'s, by default).
"""

from __future__ import annotations

import errno
import socket
import struct
from collections.abc import Callable, Iterable, Iterator

from sp_rtk_base.models.network_link_models import (
    StationReading,
    WifiMode,
    WifiReading,
)
from sp_rtk_base.services.network_link import netlink

GENL_ID_CTRL = 0x10
CTRL_CMD_GETFAMILY = 3
CTRL_ATTR_FAMILY_ID = 1
CTRL_ATTR_FAMILY_NAME = 2

NL80211_CMD_GET_INTERFACE = 5
NL80211_CMD_NEW_INTERFACE = 7
NL80211_CMD_GET_STATION = 17
NL80211_CMD_NEW_STATION = 19
NL80211_CMD_GET_SCAN = 32
NL80211_CMD_NEW_SCAN_RESULTS = 34

NL80211_ATTR_WIPHY = 1
NL80211_ATTR_IFINDEX = 3
NL80211_ATTR_IFNAME = 4
NL80211_ATTR_IFTYPE = 5
NL80211_ATTR_MAC = 6
NL80211_ATTR_STA_INFO = 21
NL80211_ATTR_WIPHY_FREQ = 38
NL80211_ATTR_BSS = 47
NL80211_ATTR_SSID = 52

NL80211_BSS_BSSID = 1
NL80211_BSS_STATUS = 9
_BSS_STATUS_ASSOCIATED = 1

NL80211_STA_INFO_SIGNAL = 7
NL80211_STA_INFO_TX_BITRATE = 8
NL80211_STA_INFO_TX_FAILED = 12
NL80211_STA_INFO_RX_BITRATE = 14

NL80211_RATE_INFO_BITRATE = 1  # u16, 100 kbit/s
NL80211_RATE_INFO_BITRATE32 = 5  # u32, 100 kbit/s

_GENL_HEADER = struct.Struct("BBH")  # cmd, version, reserved
_GENL_VERSION = 1
_ATTRS_OFFSET = _GENL_HEADER.size

# nl80211_iftype values: STATION 2, AP 3, MONITOR 6, P2P_CLIENT 8, P2P_GO 9.
_MODES: dict[int, WifiMode] = {2: "station", 3: "ap", 6: "monitor", 8: "p2p", 9: "p2p"}

Exchange = Callable[[int, int, bytes], list[bytes]]
"""Sends one request (type, flags, body) and returns every reply datagram."""


def _genl_messages(datagrams: Iterable[bytes], cmd: int) -> Iterator[dict[int, bytes]]:
    """The attributes of each reply message with genl command *cmd*.

    Raises:
        OSError: a reply is a netlink error.
    """
    for datagram in datagrams:
        for message in netlink.messages(datagram):
            if message.type in (netlink.NLMSG_ERROR, netlink.NLMSG_DONE):
                continue
            if message.payload[:1] == bytes([cmd]):
                yield netlink.attributes(message.payload[_ATTRS_OFFSET:])


def _u32(raw: bytes | None) -> int | None:
    return None if raw is None or len(raw) < 4 else struct.unpack_from("I", raw)[0]


def _mac(raw: bytes) -> str:
    return ":".join(f"{byte:02x}" for byte in raw)


def parse_family_id(datagrams: Iterable[bytes]) -> int:
    """The family id in a ``CTRL_CMD_GETFAMILY`` reply.

    Raises:
        OSError: an error reply, or no family id in the reply.
    """
    for attrs in _genl_messages(datagrams, 1):  # CTRL_CMD_NEWFAMILY
        raw = attrs.get(CTRL_ATTR_FAMILY_ID)
        if raw is not None and len(raw) >= 2:
            family: int = struct.unpack_from("H", raw)[0]
            return family
    raise OSError(errno.ENOENT, "no nl80211 family in the reply")


def parse_interface(datagrams: Iterable[bytes]) -> WifiReading:
    """The interface in a ``GET_INTERFACE`` reply, with no station.

    Raises:
        OSError: an error reply, or no interface in the reply.
    """
    for attrs in _genl_messages(datagrams, NL80211_CMD_NEW_INTERFACE):
        name = attrs.get(NL80211_ATTR_IFNAME, b"").split(b"\0", 1)[0]
        iftype = _u32(attrs.get(NL80211_ATTR_IFTYPE))
        ssid = attrs.get(NL80211_ATTR_SSID)
        return WifiReading(
            interface=name.decode(errors="replace"),
            mode=_MODES.get(iftype, "other") if iftype is not None else "other",
            wiphy=_u32(attrs.get(NL80211_ATTR_WIPHY)),
            frequency_mhz=_u32(attrs.get(NL80211_ATTR_WIPHY_FREQ)),
            ssid=None if ssid is None else ssid.decode(errors="replace"),
        )
    raise OSError(errno.ENOENT, "no interface in the reply")


def _bitrate_bps(nest: bytes | None) -> int | None:
    """A ``rate_info`` nest's bitrate in bits/s: the 32-bit one if there."""
    if nest is None:
        return None
    rate = netlink.attributes(nest)
    raw32 = _u32(rate.get(NL80211_RATE_INFO_BITRATE32))
    if raw32 is not None:
        return raw32 * 100_000
    raw16 = rate.get(NL80211_RATE_INFO_BITRATE)
    if raw16 is None or len(raw16) < 2:
        return None
    bitrate: int = struct.unpack_from("H", raw16)[0]
    return bitrate * 100_000


def parse_station(datagrams: Iterable[bytes]) -> StationReading | None:
    """The station in a ``GET_STATION`` reply; ``None`` when there's none.

    Raises:
        OSError: an error reply.
    """
    for attrs in _genl_messages(datagrams, NL80211_CMD_NEW_STATION):
        mac = attrs.get(NL80211_ATTR_MAC)
        if mac is None:
            continue
        info = netlink.attributes(attrs.get(NL80211_ATTR_STA_INFO, b""))
        signal = info.get(NL80211_STA_INFO_SIGNAL)
        return StationReading(
            bssid=_mac(mac),
            signal_dbm=struct.unpack_from("b", signal)[0] if signal else None,
            tx_bitrate_bps=_bitrate_bps(info.get(NL80211_STA_INFO_TX_BITRATE)),
            rx_bitrate_bps=_bitrate_bps(info.get(NL80211_STA_INFO_RX_BITRATE)),
            tx_failed=_u32(info.get(NL80211_STA_INFO_TX_FAILED)),
        )
    return None


def parse_station_bssid(datagrams: Iterable[bytes]) -> str | None:
    """The associated BSSID in a station dump; ``None`` when there's none."""
    station = parse_station(datagrams)
    return None if station is None else station.bssid


def parse_scan_bssid(datagrams: Iterable[bytes]) -> str | None:
    """The associated BSS's BSSID in a ``GET_SCAN`` dump; ``None`` when
    no cached BSS is the associated one."""
    for attrs in _genl_messages(datagrams, NL80211_CMD_NEW_SCAN_RESULTS):
        bss = netlink.attributes(attrs.get(NL80211_ATTR_BSS, b""))
        bssid = bss.get(NL80211_BSS_BSSID)
        if bssid is not None and _u32(bss.get(NL80211_BSS_STATUS)) == (
            _BSS_STATUS_ASSOCIATED
        ):
            return _mac(bssid)
    return None


def _genl_request(cmd: int, attrs: bytes) -> bytes:
    return _GENL_HEADER.pack(cmd, _GENL_VERSION, 0) + attrs


def _nla(kind: int, payload: bytes) -> bytes:
    raw = struct.pack("HH", 4 + len(payload), kind) + payload
    return raw + b"\0" * (-len(raw) % 4)


class WifiReader:
    """Reads every given Wi-Fi interface over one generic netlink exchange.

    The nl80211 family id is looked up on first use and again after a
    failure.  An interface nl80211 fails for is left out of the result;
    failing to find nl80211 at all raises.  Blocking: call it off the
    event loop.
    """

    def __init__(
        self,
        exchange: Exchange | None = None,
        index: Callable[[str], int] = socket.if_nametoindex,
    ) -> None:
        self._socket: netlink.NetlinkSocket | None = None
        if exchange is None:  # pragma: no cover - host I/O
            self._socket = netlink.NetlinkSocket(netlink.NETLINK_GENERIC)
            exchange = self._socket.exchange
        self._exchange = exchange
        self._index = index
        self._family: int | None = None

    def __call__(self, names: Iterable[str]) -> tuple[WifiReading, ...]:
        names = list(names)
        if not names:
            return ()
        family = self._family_id()
        readings: list[WifiReading] = []
        for name in names:
            try:
                readings.append(self._read(family, name))
            except OSError:
                continue
        if not readings:
            self._family = None  # look it up again: nl80211 may have reloaded
        return tuple(readings)

    def _family_id(self) -> int:
        if self._family is None:
            body = _GENL_HEADER.pack(CTRL_CMD_GETFAMILY, 1, 0) + _nla(
                CTRL_ATTR_FAMILY_NAME, b"nl80211\0"
            )
            self._family = parse_family_id(
                self._exchange(
                    GENL_ID_CTRL, netlink.NLM_F_REQUEST | netlink.NLM_F_ACK, body
                )
            )
        return self._family

    def _ask(self, family: int, cmd: int, attrs: bytes, *, dump: bool) -> list[bytes]:
        flags = netlink.NLM_F_REQUEST | (
            netlink.NLM_F_DUMP if dump else netlink.NLM_F_ACK
        )
        return self._exchange(family, flags, _genl_request(cmd, attrs))

    def _read(self, family: int, name: str) -> WifiReading:
        ifindex = _nla(NL80211_ATTR_IFINDEX, struct.pack("I", self._index(name)))
        reading = parse_interface(
            self._ask(family, NL80211_CMD_GET_INTERFACE, ifindex, dump=False)
        )
        if reading.mode != "station":
            return reading
        bssid = self._bssid(family, ifindex)
        if bssid is None:
            return reading
        mac = _nla(NL80211_ATTR_MAC, bytes.fromhex(bssid.replace(":", "")))
        try:
            station = parse_station(
                self._ask(family, NL80211_CMD_GET_STATION, ifindex + mac, dump=False)
            )
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
            station = None  # disassociated between the two requests
        return reading.model_copy(update={"station": station})

    def _bssid(self, family: int, ifindex: bytes) -> str | None:
        """The associated BSSID: from the station dump, or from the cached
        scan results where the firmware rejects the dump."""
        try:
            return parse_station_bssid(
                self._ask(family, NL80211_CMD_GET_STATION, ifindex, dump=True)
            )
        except OSError as exc:
            if exc.errno != errno.EOPNOTSUPP:
                raise
        return parse_scan_bssid(
            self._ask(family, NL80211_CMD_GET_SCAN, ifindex, dump=True)
        )

    def close(self) -> None:
        """Close the netlink socket, if this reader opened one."""
        if self._socket is not None:  # pragma: no cover - host I/O
            self._socket.close()
