"""The Network link sampler's host reads: one :class:`HostReading` per sample.

Read-only and standard library only: sysfs for the interfaces, a route
netlink address dump, and the shared Uplink probe (a UDP ``connect()``
that sends nothing).  No subprocesses, scans or NetworkManager calls.
Every read is blocking but quick, so the sampler runs it off the event
loop.  Each source is read on its own: one that raises is ``None`` in the
reading (No reading for that source) and never stops the others.

Which interfaces are Network links: physical (they have a ``device``) and
either Wi-Fi (they have ``phy80211``) or Ethernet (``type`` 1, no
``phy80211``, and not a wwan modem).  They are rediscovered every read.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import struct
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import ValidationError

from sp_rtk_base.models.network_link_models import (
    AddressReading,
    Counters,
    EgressReading,
    HostReading,
    InterfaceReading,
    LinkKind,
)
from sp_rtk_base.services.network_link import netlink
from sp_rtk_base.uplink import picked_source as probe_picked_source

logger = logging.getLogger(__name__)

SYSFS_NET = Path("/sys/class/net")

FAKE_NETWORK_LINKS_ENV = "SP_RTK_BASE_FAKE_NETWORK_LINKS"
"""Names a JSON :class:`HostReading` the app samples instead of the host (e2e)."""

_ARPHRD_ETHER = 1
_COUNTER_FILES = {
    "rx_bytes": "rx_bytes",
    "tx_bytes": "tx_bytes",
    "rx_packets": "rx_packets",
    "tx_packets": "tx_packets",
    "rx_errors": "rx_errors",
    "tx_errors": "tx_errors",
    "rx_drops": "rx_dropped",
    "tx_drops": "tx_dropped",
}
# What a sysfs attribute raises when the driver has no value right now
# (``speed`` on a down link, ``carrier`` on an admin-down one).
_NO_VALUE_ERRNOS = frozenset({errno.EINVAL, errno.ENODATA, errno.EOPNOTSUPP})

ReadFile = Callable[[Path], str]
T = TypeVar("T")


def _read_text(path: Path) -> str:
    return path.read_text()


class _VanishedError(Exception):
    """The interface went away while it was being read."""


def _value(read: ReadFile, path: Path, *, optional: bool = False) -> str | None:
    """A sysfs attribute's text, or ``None`` when the driver has no value
    (or, for an *optional* attribute, no such file).

    Raises:
        _VanishedError: a required attribute is gone: so is the interface.
    """
    try:
        return read(path).strip()
    except FileNotFoundError as exc:
        if optional:
            return None
        raise _VanishedError from exc
    except OSError as exc:
        if exc.errno == errno.ENODEV:
            raise _VanishedError from exc
        if exc.errno in _NO_VALUE_ERRNOS:
            return None
        raise


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _kind(path: Path, read: ReadFile) -> LinkKind | None:
    """The interface's kind if it is a Network link, else ``None``."""
    if not (path / "device").exists():
        return None  # virtual: lo, wg0, tun0, bridges
    if (path / "phy80211").exists():
        return "wifi"
    if _int(_value(read, path / "type")) != _ARPHRD_ETHER:
        return None
    uevent = _value(read, path / "uevent", optional=True) or ""
    if "DEVTYPE=wwan" in uevent.splitlines():
        return None
    return "ethernet"


def _interface(path: Path, read: ReadFile) -> InterfaceReading | None:
    kind = _kind(path, read)
    if kind is None:
        return None
    counters = Counters(
        **{
            field: _int(_value(read, path / "statistics" / file)) or 0
            for field, file in _COUNTER_FILES.items()
        }
    )
    operstate = _value(read, path / "operstate")
    if kind == "wifi":
        # carrier, speed and duplex mean nothing for Wi-Fi.
        return InterfaceReading(
            name=path.name, kind=kind, operstate=operstate, counters=counters
        )
    carrier_text = _value(read, path / "carrier", optional=True)
    carrier = None if carrier_text is None else carrier_text == "1"
    speed = duplex = None
    if carrier:
        # Only read with a cable in: lan78xx wakes from autosuspend for it.
        speed = _int(_value(read, path / "speed", optional=True))
        duplex = _value(read, path / "duplex", optional=True)
    return InterfaceReading(
        name=path.name,
        kind=kind,
        operstate=operstate,
        carrier=carrier,
        speed_mbps=speed if speed is not None and speed > 0 else None,
        full_duplex={"full": True, "half": False}.get(duplex or ""),
        counters=counters,
    )


def read_interfaces(
    root: Path = SYSFS_NET, read: ReadFile = _read_text
) -> tuple[InterfaceReading, ...]:
    """Every Network link interface under *root* (``/sys/class/net``), by name.

    Raises:
        OSError: *root* can't be listed, or a file failed for a reason
            other than having no value (``EINVAL``) or vanishing.
    """
    found: list[InterfaceReading] = []
    for path in sorted(root.iterdir()):
        try:
            interface = _interface(path, read)
        except _VanishedError:
            continue
        if interface is not None:
            found.append(interface)
    return tuple(found)


# ---------------------------------------------------------------------------
# Addresses: the route netlink RTM_GETADDR dump
# ---------------------------------------------------------------------------

_RTM_NEWADDR = 20
_RTM_GETADDR = 22
_IFADDRMSG = struct.Struct("BBBBI")  # family, prefixlen, flags, scope, index
_IFA_ADDRESS, _IFA_LOCAL, _IFA_FLAGS = 1, 2, 8
_IFA_F_DADFAILED, _IFA_F_TENTATIVE = 0x08, 0x40
_RT_SCOPE_HOST = 254


def parse_addresses(
    datagrams: Iterable[bytes], names: Mapping[int, str]
) -> tuple[AddressReading, ...]:
    """The addresses in an ``RTM_GETADDR`` dump's reply, in order.

    *names* maps interface index to name.  Host-scope (loopback),
    tentative and failed-duplicate addresses, and any on an interface
    *names* doesn't know, are left out.

    Raises:
        OSError: the reply is a netlink error.
    """
    found: list[AddressReading] = []
    for datagram in datagrams:
        for message in netlink.messages(datagram):
            if message.type != _RTM_NEWADDR:
                continue
            family, prefixlen, header_flags, scope, index = _IFADDRMSG.unpack_from(
                message.payload
            )
            attrs = netlink.attributes(message.payload[_IFADDRMSG.size :])
            flags = (
                struct.unpack("I", attrs[_IFA_FLAGS])[0]
                if _IFA_FLAGS in attrs
                else header_flags
            )
            # IPv4's IFA_ADDRESS is the peer on a point-to-point link.
            raw = attrs.get(_IFA_LOCAL) or attrs.get(_IFA_ADDRESS)
            name = names.get(index)
            if (
                raw is None
                or name is None
                or scope == _RT_SCOPE_HOST
                or flags & (_IFA_F_TENTATIVE | _IFA_F_DADFAILED)
            ):
                continue
            found.append(
                AddressReading(
                    interface=name,
                    address=socket.inet_ntop(family, raw),
                    prefixlen=prefixlen,
                )
            )
    return tuple(found)


class AddressReader:  # pragma: no cover - host I/O; parse_addresses is tested
    """Reads every interface's addresses over one long-lived route socket."""

    def __init__(self) -> None:
        self._socket = netlink.NetlinkSocket(netlink.NETLINK_ROUTE)

    def __call__(self) -> tuple[AddressReading, ...]:
        body = _IFADDRMSG.pack(socket.AF_UNSPEC, 0, 0, 0, 0)
        replies = self._socket.exchange(
            _RTM_GETADDR, netlink.NLM_F_REQUEST | netlink.NLM_F_DUMP, body
        )
        names = dict(socket.if_nameindex())
        return parse_addresses(replies, names)

    def close(self) -> None:
        self._socket.close()


# ---------------------------------------------------------------------------
# One sample of every source
# ---------------------------------------------------------------------------


class HostReaderLike(Protocol):
    """What the Network link sampler reads the host with."""

    def read(self) -> HostReading: ...

    def close(self) -> None: ...


def _attempt(name: str, read: Callable[[], T]) -> T | None:
    """*read*'s result, or ``None`` (No reading) if it raised."""
    try:
        return read()
    except Exception:
        logger.debug("Network link read of %s failed", name, exc_info=True)
        return None


class HostReader:
    """Reads one :class:`HostReading`, each source failing on its own."""

    def __init__(
        self,
        sysfs_root: Path = SYSFS_NET,
        addresses: Callable[[], tuple[AddressReading, ...]] | None = None,
        picked_source: Callable[[], str | None] = probe_picked_source,
    ) -> None:
        self._sysfs_root = sysfs_root
        self._address_reader: AddressReader | None = None
        if addresses is None:
            addresses = self._address_reader = AddressReader()
        self._addresses = addresses
        self._picked_source = picked_source

    def read(self) -> HostReading:
        """One sample of every source.  Blocking: call it off the event loop."""
        interfaces = _attempt("sysfs", lambda: read_interfaces(self._sysfs_root))
        addresses = _attempt("addresses", self._addresses)
        egress = _attempt(
            "the Uplink", lambda: EgressReading(source=self._picked_source())
        )
        return HostReading(interfaces=interfaces, addresses=addresses, egress=egress)

    def close(self) -> None:
        """Close the netlink socket, if one was opened."""
        if self._address_reader is not None:  # pragma: no cover - host I/O
            self._address_reader.close()


class FileHostReader:
    """The e2e fake host: serves the :class:`HostReading` in a JSON file,
    re-read every sample.  No file, or one that isn't a reading, is a
    sample in which every source failed."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> HostReading:
        try:
            return HostReading.model_validate_json(self._path.read_text())
        except (OSError, ValueError, ValidationError, json.JSONDecodeError):
            return HostReading()

    def close(self) -> None:
        """Nothing to close."""


def host_reader_from_env() -> HostReaderLike:
    """The host, or the e2e fake when ``SP_RTK_BASE_FAKE_NETWORK_LINKS``
    names a file.  Opens nothing until the first read."""
    fake = os.environ.get(FAKE_NETWORK_LINKS_ENV)
    if fake:
        return FileHostReader(Path(fake))
    return HostReader()
