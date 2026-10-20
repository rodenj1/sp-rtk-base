"""The Network link sampler's host reads, against fakes of the host.

sysfs is a fake ``/sys/class/net`` tree in a temp directory; a file the
kernel would refuse with ``EINVAL`` is made to raise it through the
reader's file-read hook.  Route netlink replies are hand-built datagrams
in the kernel's layout.
"""

from __future__ import annotations

import errno
import socket
import struct
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from sp_rtk_base.models.network_link_models import (
    AddressReading,
    Counters,
    EgressReading,
    HostReading,
    InterfaceReading,
    WifiReading,
)
from sp_rtk_base.services.network_link.reader import (
    FAKE_NETWORK_LINKS_ENV,
    FileHostReader,
    HostReader,
    host_reader_from_env,
    parse_addresses,
    read_interfaces,
)

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


def _interface(
    root: Path,
    name: str,
    *,
    device: bool = True,
    wifi: bool = False,
    type_: int = 1,
    devtype: str | None = None,
    files: dict[str, str] | None = None,
    counters: dict[str, int] | None = None,
) -> None:
    """One interface directory in the fake sysfs tree."""
    path = root / name
    (path / "statistics").mkdir(parents=True)
    if device:
        (path / "device").mkdir()
    if wifi:
        (path / "phy80211").mkdir()
    (path / "type").write_text(f"{type_}\n")
    uevent = f"INTERFACE={name}\n" + (f"DEVTYPE={devtype}\n" if devtype else "")
    (path / "uevent").write_text(uevent)
    for file, text in (files or {}).items():
        (path / file).write_text(f"{text}\n")
    for field, file in _COUNTER_FILES.items():
        (path / "statistics" / file).write_text(f"{(counters or {}).get(field, 0)}\n")


EINVAL = "<EINVAL>"


def _reader_raising_einval() -> Callable[[Path], str]:
    """Read files as text, but raise EINVAL for any whose content is the
    marker, as the kernel does for ``speed`` on a down interface."""

    def read(path: Path) -> str:
        text = path.read_text()
        if text.strip() == EINVAL:
            raise OSError(errno.EINVAL, "Invalid argument", str(path))
        return text

    return read


@pytest.fixture()
def sysfs(tmp_path: Path) -> Path:
    return tmp_path / "net"


def test_a_cabled_ethernet_interface_reads_its_state_speed_duplex_and_counters(
    sysfs: Path,
) -> None:
    _interface(
        sysfs,
        "eth0",
        files={"operstate": "up", "carrier": "1", "speed": "100", "duplex": "full"},
        counters={"rx_bytes": 123, "tx_drops": 4, "rx_errors": 2},
    )

    assert read_interfaces(sysfs) == (
        InterfaceReading(
            name="eth0",
            kind="ethernet",
            operstate="up",
            carrier=True,
            speed_mbps=100,
            full_duplex=True,
            counters=Counters(rx_bytes=123, tx_drops=4, rx_errors=2),
        ),
    )


def test_an_ethernet_interface_without_a_cable_has_no_speed_or_duplex(
    sysfs: Path,
) -> None:
    _interface(
        sysfs,
        "eth0",
        files={
            "operstate": "down",
            "carrier": "0",
            "speed": "-1",
            "duplex": "unknown",
        },
    )

    (eth0,) = read_interfaces(sysfs)
    assert (eth0.carrier, eth0.speed_mbps, eth0.full_duplex) == (False, None, None)


def test_files_that_raise_einval_read_as_missing(sysfs: Path) -> None:
    # An admin-down interface refuses carrier, speed and duplex.
    _interface(
        sysfs,
        "eth0",
        files={
            "operstate": "down",
            "carrier": EINVAL,
            "speed": EINVAL,
            "duplex": EINVAL,
        },
    )

    (eth0,) = read_interfaces(sysfs, read=_reader_raising_einval())
    assert (eth0.carrier, eth0.speed_mbps, eth0.full_duplex) == (None, None, None)


def test_a_wifi_interface_reads_operstate_and_never_speed_or_carrier(
    sysfs: Path,
) -> None:
    _interface(
        sysfs,
        "wlan0",
        wifi=True,
        devtype="wlan",
        files={
            "operstate": "dormant",
            "carrier": "1",
            "speed": EINVAL,
            "duplex": EINVAL,
        },
        counters={"tx_bytes": 9},
    )

    assert read_interfaces(sysfs, read=_reader_raising_einval()) == (
        InterfaceReading(
            name="wlan0",
            kind="wifi",
            operstate="dormant",
            counters=Counters(tx_bytes=9),
        ),
    )


def test_virtual_and_wwan_interfaces_are_not_network_links(sysfs: Path) -> None:
    _interface(sysfs, "lo", device=False, type_=772, files={"operstate": "unknown"})
    _interface(sysfs, "wg0", device=False, type_=65534, files={"operstate": "up"})
    _interface(sysfs, "br0", device=False, files={"operstate": "up"})
    _interface(sysfs, "wwan0", devtype="wwan", files={"operstate": "up"})
    _interface(sysfs, "can0", type_=280, files={"operstate": "up"})
    _interface(sysfs, "eth0", files={"operstate": "up", "carrier": "1"})

    assert [i.name for i in read_interfaces(sysfs)] == ["eth0"]


def test_an_interface_that_vanishes_mid_read_is_left_out(sysfs: Path) -> None:
    _interface(sysfs, "eth0", files={"operstate": "up", "carrier": "1"})
    _interface(sysfs, "eth1", files={"operstate": "up", "carrier": "1"})

    def read(path: Path) -> str:
        if path.parent.name == "eth1" or path.parent.parent.name == "eth1":
            raise FileNotFoundError(errno.ENOENT, "No such file", str(path))
        return path.read_text()

    assert [i.name for i in read_interfaces(sysfs, read=read)] == ["eth0"]


# ---------------------------------------------------------------------------
# Addresses: the route netlink RTM_GETADDR dump
# ---------------------------------------------------------------------------

RTM_NEWADDR, NLMSG_ERROR, NLMSG_DONE = 20, 2, 3
IFA_ADDRESS, IFA_LOCAL, IFA_FLAGS = 1, 2, 8
IFA_F_TENTATIVE = 0x40


def _attr(kind: int, payload: bytes) -> bytes:
    raw = struct.pack("HH", 4 + len(payload), kind) + payload
    return raw + b"\0" * (-len(raw) % 4)


def _message(kind: int, body: bytes, flags: int = 0x2) -> bytes:
    return struct.pack("IHHII", 16 + len(body), kind, flags, 1, 0) + body


def _newaddr(
    index: int,
    address: str,
    prefixlen: int,
    *,
    scope: int = 0,
    local: str | None = None,
    flags: int = 0,
) -> bytes:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    body = struct.pack("BBBBI", family, prefixlen, 0, scope, index)
    body += _attr(IFA_ADDRESS, socket.inet_pton(family, address))
    if local is not None:
        body += _attr(IFA_LOCAL, socket.inet_pton(family, local))
    body += _attr(IFA_FLAGS, struct.pack("I", flags))
    return _message(RTM_NEWADDR, body)


NAMES = {1: "lo", 2: "eth0", 3: "wlan0", 4: "wg0"}
DONE = _message(NLMSG_DONE, struct.pack("i", 0))


def test_addresses_are_read_for_every_interface_by_name() -> None:
    datagrams = [
        _newaddr(1, "127.0.0.1", 8, scope=254)
        + _newaddr(2, "192.0.2.10", 24, local="192.0.2.10")
        + _newaddr(3, "192.0.2.20", 24, local="192.0.2.20"),
        _newaddr(4, "10.8.0.1", 32, local="10.8.0.2")
        + _newaddr(2, "2001:db8::10", 64)
        + _newaddr(2, "fe80::1", 64, scope=253)
        + DONE,
    ]

    assert parse_addresses(datagrams, NAMES) == (
        AddressReading(interface="eth0", address="192.0.2.10", prefixlen=24),
        AddressReading(interface="wlan0", address="192.0.2.20", prefixlen=24),
        # On a point-to-point link the local address is ours, not the peer.
        AddressReading(interface="wg0", address="10.8.0.2", prefixlen=32),
        AddressReading(interface="eth0", address="2001:db8::10", prefixlen=64),
        AddressReading(interface="eth0", address="fe80::1", prefixlen=64),
    )


def test_tentative_addresses_and_unknown_interfaces_are_left_out() -> None:
    datagrams = [
        _newaddr(2, "2001:db8::10", 64, flags=IFA_F_TENTATIVE)
        + _newaddr(9, "192.0.2.99", 24, local="192.0.2.99")
        + DONE
    ]

    assert parse_addresses(datagrams, NAMES) == ()


def test_an_error_reply_raises() -> None:
    error = _message(NLMSG_ERROR, struct.pack("i", -errno.EPERM) + b"\0" * 16)

    with pytest.raises(OSError, match="netlink") as raised:
        parse_addresses([error], NAMES)
    assert raised.value.errno == errno.EPERM


# ---------------------------------------------------------------------------
# HostReader: one sample of every source, each failing on its own
# ---------------------------------------------------------------------------


def test_a_host_read_gives_every_source(sysfs: Path) -> None:
    _interface(sysfs, "eth0", files={"operstate": "up", "carrier": "1"})
    rows = (AddressReading(interface="eth0", address="192.0.2.10", prefixlen=24),)
    reader = HostReader(
        sysfs_root=sysfs, addresses=lambda: rows, picked_source=lambda: "192.0.2.10"
    )

    reading = reader.read()

    assert reading.interfaces is not None
    assert [i.name for i in reading.interfaces] == ["eth0"]
    assert reading.addresses == rows
    assert reading.egress == EgressReading(source="192.0.2.10")


def test_nl80211_is_asked_about_the_wifi_interfaces_only(sysfs: Path) -> None:
    _interface(sysfs, "eth0", files={"operstate": "up", "carrier": "1"})
    _interface(sysfs, "wlan0", files={"operstate": "up"}, wifi=True)
    asked: list[list[str]] = []

    def wifi(names: Iterable[str]) -> tuple[WifiReading, ...]:
        asked.append(list(names))
        return (WifiReading(interface="wlan0"),)

    reading = HostReader(
        sysfs_root=sysfs, addresses=lambda: (), picked_source=lambda: None, wifi=wifi
    ).read()

    assert asked == [["wlan0"]]
    assert reading.wifi == (WifiReading(interface="wlan0"),)


def test_a_failing_nl80211_is_no_reading_for_wifi_only(sysfs: Path) -> None:
    _interface(sysfs, "wlan0", files={"operstate": "up"}, wifi=True)

    def wifi(names: Iterable[str]) -> tuple[WifiReading, ...]:
        raise OSError(errno.EAFNOSUPPORT, "netlink blocked")

    reading = HostReader(
        sysfs_root=sysfs, addresses=lambda: (), picked_source=lambda: None, wifi=wifi
    ).read()

    assert reading.interfaces is not None
    assert reading.wifi is None


def test_an_unreachable_network_is_a_successful_uplink_probe(sysfs: Path) -> None:
    reader = HostReader(
        sysfs_root=sysfs, addresses=lambda: (), picked_source=lambda: None
    )

    assert reader.read().egress == EgressReading(source=None)


def test_a_source_that_raises_is_no_reading_for_that_source_only(sysfs: Path) -> None:
    _interface(sysfs, "eth0", files={"operstate": "up", "carrier": "1"})

    def fail() -> tuple[AddressReading, ...]:
        raise OSError(errno.EAFNOSUPPORT, "netlink blocked")

    def fail_probe() -> str | None:
        raise OSError(errno.EPERM, "denied")

    reading = HostReader(
        sysfs_root=sysfs, addresses=fail, picked_source=fail_probe
    ).read()

    assert reading.interfaces is not None
    assert reading.addresses is None
    assert reading.egress is None


def test_an_unreadable_sysfs_is_no_reading_for_interfaces(tmp_path: Path) -> None:
    reading = HostReader(
        sysfs_root=tmp_path / "missing",
        addresses=lambda: (),
        picked_source=lambda: None,
    ).read()

    assert reading.interfaces is None
    assert reading.addresses == ()


# ---------------------------------------------------------------------------
# The e2e fake host
# ---------------------------------------------------------------------------


def test_the_fake_host_serves_the_reading_in_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "network-links.json"
    path.write_text(
        '{"interfaces": [{"name": "eth0", "kind": "ethernet", "carrier": true,'
        ' "counters": {"rx_bytes": 7}}],'
        ' "addresses": [], "egress": {"source": null}}'
    )
    monkeypatch.setenv(FAKE_NETWORK_LINKS_ENV, str(path))

    reading = host_reader_from_env().read()

    assert reading.interfaces == (
        InterfaceReading(
            name="eth0", kind="ethernet", carrier=True, counters=Counters(rx_bytes=7)
        ),
    )
    assert reading.egress == EgressReading(source=None)


@pytest.mark.parametrize("content", [None, "not json", '{"interfaces": 3}'])
def test_the_fake_host_without_a_reading_fails_every_source(
    tmp_path: Path, content: str | None
) -> None:
    path = tmp_path / "network-links.json"
    if content is not None:
        path.write_text(content)

    assert FileHostReader(path).read() == HostReading()


def test_the_real_host_is_read_without_the_fake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(FAKE_NETWORK_LINKS_ENV, raising=False)

    assert isinstance(host_reader_from_env(), HostReader)
