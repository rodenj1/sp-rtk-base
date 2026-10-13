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

from tests.fixtures.fake_dns import FakeDns, closed_port

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
        self.state = root / "state"
        self.answer = "false"
        """What ``ask`` answers in uninstall.sh (``true``: yes)."""
        self.public = [closed_port()]
        """The "public" resolvers install.sh checks (none answers)."""
        self.stderr = ""

    @property
    def unit(self) -> Path:
        return self.units / UNIT

    @property
    def drop_in(self) -> Path:
        return self.units / f"{UNIT}.d" / "resolver.conf"

    @property
    def agent_resolv(self) -> Path:
        return self.root / "etc" / "sp-rtk-base-portal-agent" / "resolv.conf"

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
            f'PORTAL_AGENT_STATE_DIR="{self.state}"\n'
            f"ask() {{ {self.answer}; }}\n"
            f'RESOLV_CONF="{self.resolv}"\n'
            f'PORTAL_AGENT_RESOLV_CONF="{self.agent_resolv}"\n'
            f'PORTAL_AGENT_PUBLIC_DNS="{" ".join(self.public)}"\n'
            f"{block}\n"
        )
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        self.stderr = result.stderr
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

    def test_public_dns_answers_so_the_lan_resolver_isnt_allowed(
        self, tmp_path: Path, install_block: str
    ) -> None:
        host = Host(tmp_path, resolvers=["192.168.0.1"])

        with FakeDns() as dns:
            host.public = [dns.server]
            calls = host.run(install_block)

        # The agent's own resolv.conf (bind-mounted by the unit) lists the
        # public resolver; no drop-in lets it reach the router.
        assert "nameserver 127.0.0.1\n" in host.agent_resolv.read_text()
        assert "192.168.0.1" not in host.agent_resolv.read_text()
        assert not host.drop_in.exists()
        assert "WARN" not in host.stderr
        assert calls.index("systemctl daemon-reload") < calls.index(
            f"systemctl restart {UNIT}"
        )

    def test_blocked_public_dns_falls_back_to_the_lan_resolver_with_a_warning(
        self, tmp_path: Path, install_block: str
    ) -> None:
        host = Host(tmp_path, resolvers=["192.168.1.1", "8.8.8.8", "fe80::1%eth0"])

        host.run(install_block)

        text = host.drop_in.read_text()
        assert "[Service]\n" in text
        assert "\nBindReadOnlyPaths=\n" in text
        allow = [ln for ln in text.splitlines() if ln.startswith("IPAddressAllow=")]
        assert allow == ["IPAddressAllow=192.168.1.1"]
        assert not host.agent_resolv.exists()
        assert "WARN" in host.stderr
        assert "192.168.1.1" in host.stderr
        assert "any port" in host.stderr

    def test_blocked_public_dns_with_a_loopback_resolver_opens_no_lan_address(
        self, tmp_path: Path, install_block: str
    ) -> None:
        host = Host(tmp_path, resolvers=["127.0.0.53"])

        host.run(install_block)

        assert "IPAddressAllow" not in host.drop_in.read_text()
        assert "\nBindReadOnlyPaths=\n" in host.drop_in.read_text()
        assert "WARN" in host.stderr

    def test_a_rerun_converges(self, tmp_path: Path, install_block: str) -> None:
        host = Host(tmp_path, resolvers=["10.0.0.1"])
        host.run(install_block)
        first_unit = host.unit.read_text()
        assert host.drop_in.exists()

        with FakeDns() as dns:
            host.public = [dns.server]
            host.run(install_block)

        assert host.unit.read_text() == first_unit
        assert not host.drop_in.exists()
        assert not host.drop_in.parent.exists()
        assert host.agent_resolv.exists()

        host.public = [closed_port()]
        host.run(install_block)

        assert host.drop_in.exists()
        assert not host.agent_resolv.exists()

    def test_a_missing_resolv_conf_is_no_error(
        self, tmp_path: Path, install_block: str
    ) -> None:
        host = Host(tmp_path, resolvers=[])
        host.resolv.unlink()

        host.run(install_block)

        assert "IPAddressAllow" not in host.drop_in.read_text()
        assert "can't resolve" in host.stderr
        assert host.unit.exists()

    def test_the_unit_bind_mounts_the_file_install_sh_writes(self) -> None:
        text = (DEPLOY / "install.sh").read_text()
        default = (
            'PORTAL_AGENT_RESOLV_CONF="${PORTAL_AGENT_RESOLV_CONF:-'
            '/etc/sp-rtk-base-portal-agent/resolv.conf}"'
        )
        assert default in text
        assert default in (DEPLOY / "uninstall.sh").read_text()
        assert (
            "BindReadOnlyPaths=/etc/sp-rtk-base-portal-agent/resolv.conf:/etc/resolv.conf"
            in (DEPLOY / UNIT).read_text()
        )

    def test_runs_in_both_modes(self) -> None:
        text = (DEPLOY / "install.sh").read_text()
        block_at = text.index(START)
        assert text.index("# Step 8 — systemd unit") < block_at
        assert block_at < text.index('if [[ "$MODE" == "appliance" ]]; then\n')

    def test_lays_down_the_unit_only_after_the_venv_has_the_agent(self) -> None:
        text = (DEPLOY / "install.sh").read_text()
        assert text.index("# Step 5 —") < text.index(START)


class TestClaimSurvivesReinstall:
    """The Claim lives in the agent's state directory; installing or
    upgrading over it never touches it (#86)."""

    @pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
    def test_the_scripts_never_touch_the_state_directory(self, script: str) -> None:
        text = (DEPLOY / script).read_text()
        assert "/var/lib/sp-rtk-base-portal-agent" not in text
        assert "PORTAL_AGENT_STATE" not in text


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
        host.agent_resolv.parent.mkdir(parents=True)
        host.agent_resolv.write_text("x")

        calls = host.run(_block("uninstall.sh"))

        assert f"systemctl disable --now {UNIT}" in calls
        assert not host.unit.exists()
        assert not host.drop_in.parent.exists()
        assert not host.agent_resolv.parent.exists()

    @pytest.mark.parametrize(("answer", "kept"), [("false", True), ("true", False)])
    def test_removes_the_claim_only_when_asked_to(
        self, tmp_path: Path, answer: str, kept: bool
    ) -> None:
        host = Host(tmp_path, resolvers=[])
        host.state.mkdir()
        (host.state / "base_key.pem").write_text("key")
        host.answer = answer

        host.run(_block("uninstall.sh"))

        assert host.state.exists() is kept

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
