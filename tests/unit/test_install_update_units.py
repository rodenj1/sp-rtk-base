"""``install.sh`` lays down the Update units, and ``--no-update`` leaves
Update off; ``uninstall.sh`` removes them (sp-rtk-base #239, ADR 0005).

Like the other deploy tests, these need no root: they run fragments of
the scripts under ``bash -c`` with ``systemctl`` and the file helpers
stubbed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "deploy" / "install.sh"
UNINSTALL_SCRIPT = REPO_ROOT / "deploy" / "uninstall.sh"


@pytest.fixture(scope="module")
def install_text() -> str:
    return INSTALL_SCRIPT.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i) + len(end)
    return text[i:j]


class TestNoUpdateFlag:
    @pytest.fixture(scope="class")
    def parsing(self, install_text: str) -> str:
        knobs = _between(install_text, 'MODE="${MODE:-}"', "\ndone\n")
        return 'die() { echo "DIE $*" >&2; exit 1; }\n' + knobs

    def _parse(self, parsing: str, *args: str) -> str:
        result = subprocess.run(
            ["bash", "-c", f'{parsing}\necho "$NO_UPDATE|$MODE|$VERSION"', "_", *args],
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        return result.stdout.strip()

    def test_update_is_on_by_default(self, parsing: str) -> None:
        assert self._parse(parsing, "--mode", "appliance") == "false|appliance|"

    def test_no_update_turns_it_off(self, parsing: str) -> None:
        assert (
            self._parse(parsing, "--no-update", "--mode", "managed-host", "0.9.0")
            == "true|managed-host|0.9.0"
        )

    def test_no_update_is_not_taken_for_a_version(self, parsing: str) -> None:
        assert self._parse(parsing, "0.9.0", "--no-update") == "true||0.9.0"


class TestUpdateUnits:
    """Step 8's Update block, with a recording ``systemctl`` and
    ``fetch_deploy_file``."""

    @pytest.fixture(scope="class")
    def block(self, install_text: str) -> str:
        return _between(
            install_text,
            "# Update units (ADR 0005)",
            "# End of the Update units",
        )

    def _run(
        self,
        tmp_path: Path,
        block: str,
        *,
        no_update: bool = False,
        installed_before: bool = False,
        path_enabled: bool = True,
        plumbing: str = "7\n",
        expect_ok: bool = True,
    ) -> list[str]:
        calls = tmp_path / "calls"
        units = tmp_path / "systemd"
        units.mkdir(exist_ok=True)
        # What fetch_deploy_file serves: the repo's units, and a plumbing
        # version of our choosing.
        served = tmp_path / "served"
        served.mkdir(exist_ok=True)
        for unit in ("sp-rtk-base-update.service", "sp-rtk-base-update.path"):
            (served / unit).write_text((REPO_ROOT / "deploy" / unit).read_text())
        (served / "plumbing-version").write_text(plumbing)
        if installed_before:
            (units / "sp-rtk-base-update.service").write_text("old\n")
            (units / "sp-rtk-base-update.path").write_text("old\n")
        script = (
            "set -euo pipefail\n"
            'log() { :; }; ok() { :; }; warn() { echo "WARN $*" >&2; }\n'
            'die() { echo "DIE $*" >&2; exit 1; }\n'
            f'fetch_deploy_file() {{ echo "fetch $1 $2" >> "{calls}"; '
            f'cp "{served}/$1" "$2"; }}\n'
            f'systemctl() {{ echo "systemctl $*" >> "{calls}"; '
            f'if [[ "$1" == is-enabled ]]; then {"true" if path_enabled else "false"}; '
            "fi; }\n"
            f'UPDATE_SYSTEMD_UNIT="{units}/sp-rtk-base-update.service"\n'
            f'UPDATE_PATH_UNIT="{units}/sp-rtk-base-update.path"\n'
            f'NO_UPDATE="{"true" if no_update else "false"}"\n'
            f"{block}\n"
        )
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )
        if not expect_ok:
            assert result.returncode != 0
            assert "DIE" in result.stderr
            return calls.read_text().splitlines()
        assert result.returncode == 0, result.stderr
        return calls.read_text().splitlines()

    def _service_lines(self, tmp_path: Path) -> list[str]:
        text = (tmp_path / "systemd" / "sp-rtk-base-update.service").read_text()
        service = text.split("[Service]\n", 1)[1].split("\n[", 1)[0]
        return service.splitlines()

    def test_lays_down_both_units_and_turns_update_on(
        self, tmp_path: Path, block: str
    ) -> None:
        calls = self._run(tmp_path, block)

        assert (
            f"fetch sp-rtk-base-update.service {tmp_path}/systemd/sp-rtk-base-update.service"
            in calls
        )
        assert (
            f"fetch sp-rtk-base-update.path {tmp_path}/systemd/sp-rtk-base-update.path"
            in calls
        )
        assert "systemctl daemon-reload" in calls
        assert "systemctl enable --now sp-rtk-base-update.path" in calls
        assert not any("disable" in c for c in calls)
        # The service itself is started by the path unit only.
        assert not any("enable" in c and "update.service" in c for c in calls)

    def test_no_update_leaves_the_path_unit_disabled(
        self, tmp_path: Path, block: str
    ) -> None:
        calls = self._run(tmp_path, block, no_update=True)

        assert "systemctl disable --now sp-rtk-base-update.path" in calls
        assert not any(c.startswith("systemctl enable") for c in calls)
        # The units are still laid down, so turning Update on later is one command.
        assert any(c.startswith("fetch sp-rtk-base-update.path") for c in calls)

    def test_a_rerun_keeps_update_off_where_the_admin_turned_it_off(
        self, tmp_path: Path, block: str
    ) -> None:
        calls = self._run(tmp_path, block, installed_before=True, path_enabled=False)

        assert not any(c.startswith("systemctl enable") for c in calls)

    def test_a_rerun_keeps_update_on(self, tmp_path: Path, block: str) -> None:
        calls = self._run(tmp_path, block, installed_before=True, path_enabled=True)

        assert "systemctl enable --now sp-rtk-base-update.path" in calls

    def test_writes_the_plumbing_version_into_the_update_unit(
        self, tmp_path: Path, block: str
    ) -> None:
        calls = self._run(tmp_path, block, plumbing="7\n")

        assert any(c.startswith("fetch plumbing-version ") for c in calls)
        lines = self._service_lines(tmp_path)
        assert lines.count("Environment=SP_RTK_BASE_PLUMBING=7") == 1
        # ... before systemd reads the unit.
        reload_at = calls.index("systemctl daemon-reload")
        fetch_at = next(
            i for i, c in enumerate(calls) if c.startswith("fetch plumbing-version")
        )
        assert fetch_at < reload_at

    def test_a_rerun_writes_the_line_once(self, tmp_path: Path, block: str) -> None:
        self._run(tmp_path, block, plumbing="7\n")
        self._run(tmp_path, block, installed_before=True, plumbing="8\n")

        lines = self._service_lines(tmp_path)
        assert [
            ln for ln in lines if ln.startswith("Environment=SP_RTK_BASE_PLUMB")
        ] == ["Environment=SP_RTK_BASE_PLUMBING=8"]

    def test_writes_it_with_update_turned_off_too(
        self, tmp_path: Path, block: str
    ) -> None:
        self._run(tmp_path, block, no_update=True, plumbing="7\n")

        assert "Environment=SP_RTK_BASE_PLUMBING=7" in self._service_lines(tmp_path)

    def test_a_garbled_plumbing_version_stops_the_install(
        self, tmp_path: Path, block: str
    ) -> None:
        calls = self._run(tmp_path, block, plumbing="<html>\n", expect_ok=False)

        assert not any(c.startswith("systemctl enable") for c in calls)

    def test_the_repos_plumbing_version_is_a_number(self) -> None:
        text = (REPO_ROOT / "deploy" / "plumbing-version").read_text()
        assert text.strip().isdigit()

    def test_runs_in_both_modes(self, install_text: str) -> None:
        block_idx = install_text.index("# Update units (ADR 0005)")
        appliance_idx = install_text.index('if [[ "$MODE" == "appliance" ]]; then\n')
        assert install_text.index("# Step 8 — systemd unit") < block_idx < appliance_idx


class TestUpdateDirectory:
    def test_the_state_dir_holds_an_update_dir_the_app_can_write(
        self, install_text: str
    ) -> None:
        step3 = _between(install_text, "# Step 3 —", "# Step 4 —")
        assert (
            'install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" '
            '"${STATE_DIR}/update"' in step3
        )


class TestUninstall:
    @pytest.fixture(scope="class")
    def uninstall_text(self) -> str:
        return UNINSTALL_SCRIPT.read_text(encoding="utf-8")

    def test_stops_and_removes_the_update_units(
        self, tmp_path: Path, uninstall_text: str
    ) -> None:
        block = _between(
            uninstall_text, "# Update units (ADR 0005)", "# End of the Update units"
        )
        units = tmp_path / "systemd"
        units.mkdir()
        service = units / "sp-rtk-base-update.service"
        path = units / "sp-rtk-base-update.path"
        service.write_text("x")
        path.write_text("x")
        calls = tmp_path / "calls"
        script = (
            "set -euo pipefail\n"
            f'systemctl() {{ echo "systemctl $*" >> "{calls}"; }}\n'
            f'UPDATE_SYSTEMD_UNIT="{service}"\n'
            f'UPDATE_PATH_UNIT="{path}"\n'
            f"{block}\n"
        )
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, result.stderr
        made = calls.read_text().splitlines()
        assert "systemctl disable --now sp-rtk-base-update.path" in made
        assert "systemctl stop sp-rtk-base-update.service" in made
        assert not service.exists()
        assert not path.exists()

    def test_unit_paths_are_overridable_like_install_sh(
        self, install_text: str, uninstall_text: str
    ) -> None:
        for knob in (
            'UPDATE_SYSTEMD_UNIT="${UPDATE_SYSTEMD_UNIT:-/etc/systemd/system/sp-rtk-base-update.service}"',
            'UPDATE_PATH_UNIT="${UPDATE_PATH_UNIT:-/etc/systemd/system/sp-rtk-base-update.path}"',
        ):
            assert knob in install_text
            assert knob in uninstall_text
