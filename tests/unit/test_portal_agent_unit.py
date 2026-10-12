"""The Portal agent's systemd unit (ticket #83, spec #82).

The unit is what keeps the agent off the LAN whatever its code does, so its
text is pinned here: the sp-rtk-base unit's sandboxing, plus the address
family, device and IP address rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_UNIT = REPO_ROOT / "deploy" / "sp-rtk-base-portal-agent.service"
APP_UNIT = REPO_ROOT / "deploy" / "sp-rtk-base.service"

LAN_RANGES = {
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "fc00::/7",
    "fe80::/10",
}


def _directives(path: Path, section: str = "Service") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    current = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = line.strip("[]")
            continue
        if current == section:
            key, _, value = line.partition("=")
            out.append((key, value))
    return out


def _values(path: Path, key: str) -> list[str]:
    return [v for k, v in _directives(path) if k == key]


def _words(path: Path, key: str) -> set[str]:
    return {w for v in _values(path, key) for w in v.split()}


def _app_sandbox() -> list[tuple[str, str]]:
    """The sp-rtk-base unit's lines after its sandboxing banner."""
    text = APP_UNIT.read_text()
    block = text.split("# --- Sandboxing / hardening", 1)[1].split("[Install]", 1)[0]
    lines = [
        ln.strip()
        for ln in block.splitlines()
        if "=" in ln and not ln.strip().startswith("#")
    ]
    return [(k, v) for k, _, v in (ln.partition("=") for ln in lines)]


def test_carries_the_app_units_sandboxing() -> None:
    agent = set(_directives(AGENT_UNIT))
    # The agent writes nothing outside its runtime directory, and has no
    # devices: those two lines are stricter on purpose.
    shared = [
        (k, v) for k, v in _app_sandbox() if k not in {"ReadWritePaths", "DevicePolicy"}
    ]
    assert shared, "the app unit's sandboxing block wasn't found"
    for directive in shared:
        assert directive in agent, f"missing {directive[0]}={directive[1]}"


def test_has_no_devices() -> None:
    assert _values(AGENT_UNIT, "DevicePolicy") == ["closed"]
    assert _values(AGENT_UNIT, "DeviceAllow") == []


def test_only_inet_and_unix_sockets() -> None:
    assert _words(AGENT_UNIT, "RestrictAddressFamilies") == {
        "AF_INET",
        "AF_INET6",
        "AF_UNIX",
    }


def test_denied_the_lan_and_allowed_only_loopback() -> None:
    assert _words(AGENT_UNIT, "IPAddressDeny") == LAN_RANGES
    assert _words(AGENT_UNIT, "IPAddressAllow") == {"127.0.0.0/8", "::1"}


def test_writes_nothing_on_disk_but_its_socket() -> None:
    assert _values(AGENT_UNIT, "ReadWritePaths") == []
    assert _values(AGENT_UNIT, "RuntimeDirectory") == ["sp-rtk-base-portal-agent"]
    assert _values(AGENT_UNIT, "RuntimeDirectoryMode") == ["0700"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("User", "sp-rtk-base"),
        ("Group", "sp-rtk-base"),
        ("ExecStart", "/opt/sp-rtk-base/venv/bin/sp-rtk-base-portal-agent"),
        (
            "Environment",
            "SP_RTK_BASE_PORTAL_AGENT_SOCKET=/run/sp-rtk-base-portal-agent/control.sock",
        ),
    ],
)
def test_runs_the_agent_as_the_service_user(key: str, value: str) -> None:
    assert value in _values(AGENT_UNIT, key)


def test_is_enabled_at_boot() -> None:
    assert ("WantedBy", "multi-user.target") in _directives(AGENT_UNIT, "Install")
