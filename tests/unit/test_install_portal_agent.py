"""``install.sh``, ``upgrade.sh`` and ``uninstall.sh`` handle the Portal
agent's unit (ticket #83, spec #82).

Like the other deploy tests these need no root: they run the scripts'
Portal agent blocks under ``bash -c`` with ``systemctl`` and
``fetch_deploy_file`` stubbed, and the venv's Python standing in for the
installed one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
START = "# Portal agent (spec #82)"
END = "# End of the Portal agent"
VENV_DIR = Path(sys.executable).parent.parent
UNIT = "sp-rtk-base-portal-agent.service"


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i) + len(end)
    return text[i:j]


def _block(script: str) -> str:
    return _between((DEPLOY / script).read_text(encoding="utf-8"), START, END)


class Host:
    """A pretend host: a systemd unit dir, a resolv.conf and a call log."""

    def __init__(self, root: Path, *, resolvers: list[str], unit_known: bool = True):
        self.root = root
        self.units = root / "systemd"
        self.units.mkdir()
        self.calls = root / "calls"
        self.calls.touch()
        self.resolv = root / "resolv.conf"
        self.resolv.write_text(
            "# generated\nsearch lan\n"
            + "".join(f"nameserver {r}\n" for r in resolvers)
        )
        self.unit_known = unit_known

    @property
    def unit(self) -> Path:
        return self.units / UNIT

    @property
    def drop_in(self) -> Path:
        return self.units / f"{UNIT}.d" / "resolver.conf"

    def run(self, block: str) -> list[str]:
        script = (
            "set -euo pipefail\n"
            'log() { :; }; ok() { :; }; warn() { echo "WARN $*" >&2; }\n'
            'die() { echo "DIE $*" >&2; exit 1; }\n'
            f'fetch_deploy_file() {{ echo "fetch $1 $2" >> "{self.calls}"; '
            f'cp "{DEPLOY}/$1" "$2"; }}\n'
            f'systemctl() {{ echo "systemctl $*" >> "{self.calls}"; '
            f'if [[ "$1" == list-unit-files ]]; then {"true" if self.unit_known else "false"}; fi; }}\n'
            f'VENV_DIR="{VENV_DIR}"\n'
            f'PORTAL_AGENT_SYSTEMD_UNIT="{self.unit}"\n'
            f'RESOLV_CONF="{self.resolv}"\n'
            f"{block}\n"
        )
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        return self.calls.read_text().splitlines()


@pytest.fixture()
def install_block() -> str:
    return _block("install.sh")


class TestInstall:
    def test_lays_down_enables_and_starts_the_agent(
        self, tmp_path: Path, install_block: str
    ) -> None:
        host = Host(tmp_path, resolvers=["127.0.0.53"])

        calls = host.run(install_block)

        assert f"fetch {UNIT} {host.unit}" in calls
        assert host.unit.read_text() == (DEPLOY / UNIT).read_text()
        reload_at = calls.index("systemctl daemon-reload")
        assert calls.index(f"systemctl enable {UNIT}") > reload_at
        assert calls.index(f"systemctl restart {UNIT}") > reload_at

    def test_a_loopback_resolver_needs_no_allowance(
        self, tmp_path: Path, install_block: str
    ) -> None:
        host = Host(tmp_path, resolvers=["127.0.0.53", "::1"])

        host.run(install_block)

        assert not host.drop_in.exists()

    def test_a_lan_resolver_is_allowed_by_address(
        self, tmp_path: Path, install_block: str
    ) -> None:
        host = Host(tmp_path, resolvers=["192.168.1.1", "8.8.8.8", "fe80::1%eth0"])

        calls = host.run(install_block)

        text = host.drop_in.read_text()
        assert "[Service]\n" in text
        allow = [ln for ln in text.splitlines() if ln.startswith("IPAddressAllow=")]
        assert allow == ["IPAddressAllow=192.168.1.1 fe80::1"]
        # ... before systemd reads the unit.
        assert calls.index("systemctl daemon-reload") >= 0

    def test_a_rerun_is_idempotent_and_drops_a_stale_allowance(
        self, tmp_path: Path, install_block: str
    ) -> None:
        host = Host(tmp_path, resolvers=["10.0.0.1"])
        host.run(install_block)
        first_unit = host.unit.read_text()
        assert host.drop_in.exists()

        host.resolv.write_text("nameserver 1.1.1.1\n")
        host.run(install_block)

        assert host.unit.read_text() == first_unit
        assert not host.drop_in.exists()

    def test_a_missing_resolv_conf_needs_no_allowance(
        self, tmp_path: Path, install_block: str
    ) -> None:
        host = Host(tmp_path, resolvers=[])
        host.resolv.unlink()

        host.run(install_block)

        assert not host.drop_in.exists()
        assert host.unit.exists()

    def test_runs_in_both_modes(self) -> None:
        text = (DEPLOY / "install.sh").read_text()
        block_at = text.index(START)
        assert text.index("# Step 8 — systemd unit") < block_at
        assert block_at < text.index('if [[ "$MODE" == "appliance" ]]; then\n')

    def test_lays_down_the_unit_only_after_the_venv_has_the_agent(self) -> None:
        text = (DEPLOY / "install.sh").read_text()
        assert text.index("# Step 5 —") < text.index(START)


class TestUpgrade:
    def test_restarts_an_installed_agent(self, tmp_path: Path) -> None:
        host = Host(tmp_path, resolvers=[])

        calls = host.run(_block("upgrade.sh"))

        assert f"systemctl try-restart {UNIT}" in calls

    def test_leaves_a_host_without_the_agent_alone(self, tmp_path: Path) -> None:
        host = Host(tmp_path, resolvers=[], unit_known=False)

        calls = host.run(_block("upgrade.sh"))

        assert not any("restart" in c for c in calls)
        assert any("re-run" in ln for ln in _block("upgrade.sh").splitlines())


class TestUninstall:
    def test_stops_disables_and_removes_the_agent(self, tmp_path: Path) -> None:
        host = Host(tmp_path, resolvers=["192.168.1.1"])
        host.unit.write_text("x")
        host.drop_in.parent.mkdir()
        host.drop_in.write_text("x")

        calls = host.run(_block("uninstall.sh"))

        assert f"systemctl disable --now {UNIT}" in calls
        assert not host.unit.exists()
        assert not host.drop_in.parent.exists()

    def test_removes_it_before_the_app_tree(self) -> None:
        text = (DEPLOY / "uninstall.sh").read_text()
        assert text.index(START) < text.index('rm -rf "$INSTALL_PREFIX"')


class TestUpdateUnit:
    """An Update restarts the agent with the app, so it runs the new code."""

    def test_the_update_restarts_the_agent(self) -> None:
        text = (DEPLOY / "sp-rtk-base-update.service").read_text()
        assert f"ExecStart=-+/usr/bin/systemctl try-restart {UNIT}" in text

    def test_a_rollback_restarts_it_too(self) -> None:
        text = (DEPLOY / "sp-rtk-base-update.service").read_text()
        rollback = next(ln for ln in text.splitlines() if "update/rollback ]" in ln)
        assert f"try-restart {UNIT}" in rollback
