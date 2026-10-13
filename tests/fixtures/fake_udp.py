"""The agent's UDP, faked: what it sends, and datagrams delivered to it.

``FakeUdp`` is passed as the agent's ``open_udp``. Every socket the agent
opens is recorded; ``sent`` lists ``(packet, (address, port), socket number)``
and ``deliver(packet, source)`` hands a datagram to the newest socket, as a
reply down the NAT mapping would arrive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

OnDatagram = Callable[[bytes, tuple[str, int]], None]


@dataclass
class _Socket:
    owner: FakeUdp
    number: int
    on_datagram: OnDatagram
    closed: bool = False

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        assert not self.closed, "sent on a closed socket"
        self.owner.sent.append((data, addr, self.number))

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeUdp:
    sent: list[tuple[bytes, tuple[str, int], int]] = field(
        default_factory=lambda: list[tuple[bytes, tuple[str, int], int]]()
    )
    sockets: list[_Socket] = field(default_factory=lambda: list[_Socket]())

    async def __call__(self, address: str, on_datagram: OnDatagram) -> _Socket:
        sock = _Socket(self, len(self.sockets), on_datagram)
        self.sockets.append(sock)
        return sock

    def deliver(self, packet: bytes, source: tuple[str, int]) -> None:
        open_ = [s for s in self.sockets if not s.closed]
        assert open_, "the agent has no UDP socket open"
        open_[-1].on_datagram(packet, source)
