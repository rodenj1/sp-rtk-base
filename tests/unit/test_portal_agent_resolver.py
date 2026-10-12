"""The drop-in that lets the Portal agent reach a LAN DNS resolver (#83)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sp_rtk_base.portal_agent.resolver import drop_in, main


@pytest.mark.parametrize(
    ("resolv_conf", "allowed"),
    [
        ("nameserver 127.0.0.53\noptions edns0\n", None),
        ("nameserver 1.1.1.1\nnameserver 2606:4700::1111\n", None),
        ("nameserver 192.168.1.1\n", "192.168.1.1"),
        ("nameserver 172.20.0.1\nnameserver 172.32.0.1\n", "172.20.0.1"),
        ("nameserver 169.254.1.1\n", "169.254.1.1"),
        ("nameserver fd00::53\nnameserver fe80::1%wlan0\n", "fd00::53 fe80::1"),
        ("nameserver 10.0.0.1\nnameserver 10.0.0.1\n", "10.0.0.1"),
        ("# nameserver 10.0.0.9\nnameserver\nnameserver bogus\n", None),
    ],
)
def test_allows_only_the_resolvers_the_unit_denies(
    resolv_conf: str, allowed: str | None
) -> None:
    text = drop_in(resolv_conf)

    if allowed is None:
        assert text == ""
    else:
        assert "[Service]\n" in text
        assert f"\nIPAddressAllow={allowed}\n" in text


def test_main_prints_the_drop_in_for_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 192.168.0.1\n")

    assert main([str(resolv)]) == 0
    assert "IPAddressAllow=192.168.0.1\n" in capsys.readouterr().out


def test_main_prints_nothing_for_a_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(tmp_path / "nope")]) == 0
    assert capsys.readouterr().out == ""
