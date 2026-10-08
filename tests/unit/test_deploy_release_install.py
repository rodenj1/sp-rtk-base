"""``deploy/install.sh`` and ``deploy/upgrade.sh`` leave a base the UI's
Update can work on (sp-rtk-base #238, spec #235).

- ``upgrade.sh`` hands the venv back to the service user after pip, even
  when pip fails partway.
- Both scripts move the Relay to the newest version the new app allows.
- ``install.sh`` fetches its host files (units, polkit rule, shared
  scripts) from tag ``v<installed version>``, never ``main``; from a local
  checkout it uses the checkout.
- ``install.sh``'s closing hint names ``upgrade.sh``.

Like the other deploy tests, these need no root: they assert on the
scripts' text and run the extracted fragments under ``bash -c`` with the
host commands (``pip``, ``chown``, ``curl``, ``install``) stubbed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "deploy" / "install.sh"
UPGRADE_SCRIPT = REPO_ROOT / "deploy" / "upgrade.sh"

RAW_BASE = "https://raw.githubusercontent.com/rodenj1/sp-rtk-base"


@pytest.fixture(scope="module")
def install_text() -> str:
    return INSTALL_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def upgrade_text() -> str:
    return UPGRADE_SCRIPT.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    """The script text from ``start`` up to and including ``end``."""
    i = text.index(start)
    j = text.index(end, i) + len(end)
    return text[i:j]


def _function(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.DOTALL | re.MULTILINE)
    assert match, f"{name}() not found"
    return match.group(0)


def _fake_pip(tmp_path: Path, *, fail: bool = False) -> Path:
    """A venv whose ``bin/pip`` records its argv and drops a file, as a
    root-run pip would."""
    venv = tmp_path / "prefix" / "venv"
    (venv / "bin").mkdir(parents=True)
    pip = venv / "bin" / "pip"
    pip.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/pip-args"\n'
        f'touch "{venv}/written-by-pip"\n'
        f"exit {1 if fail else 0}\n",
        encoding="utf-8",
    )
    pip.chmod(0o755)
    return venv


# ---------------------------------------------------------------------------
# upgrade.sh
# ---------------------------------------------------------------------------


class TestUpgradeInstall:
    """The pip step of upgrade.sh, run with a fake pip and a recording chown."""

    @pytest.fixture(scope="class")
    def install_block(self, upgrade_text: str) -> str:
        relay_knob = re.search(r"^RELAY_NAME=.*$", upgrade_text, re.MULTILINE)
        assert relay_knob, "RELAY_NAME knob not found in upgrade.sh"
        block = _between(upgrade_text, 'if [[ -n "$VERSION" ]]; then', "trap - EXIT")
        return f"{relay_knob.group(0)}\n{block}"

    def _run(
        self, tmp_path: Path, block: str, version: str = "", *, fail: bool = False
    ) -> subprocess.CompletedProcess[str]:
        venv = _fake_pip(tmp_path, fail=fail)
        script = (
            "set -euo pipefail\n"
            f'chown() {{ echo "chown $*" >> "{tmp_path}/calls"; }}\n'
            f'INSTALL_PREFIX="{venv.parent}"\n'
            f'VENV_DIR="{venv}"\n'
            'SERVICE_USER="sp-rtk-base"\n'
            'old_ver="0.8.0"\n'
            f'VERSION="{version}"\n'
            f"{block}\n"
            f'echo "after-install" >> "{tmp_path}/calls"\n'
        )
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )

    def test_hands_the_venv_back_to_the_service_user(
        self, tmp_path: Path, install_block: str
    ) -> None:
        result = self._run(tmp_path, install_block)
        assert result.returncode == 0, result.stderr
        calls = (tmp_path / "calls").read_text().splitlines()
        assert calls[0] == f"chown -R sp-rtk-base:sp-rtk-base {tmp_path / 'prefix'}"
        assert calls[-1] == "after-install"

    def test_hands_the_venv_back_even_when_pip_fails(
        self, tmp_path: Path, install_block: str
    ) -> None:
        result = self._run(tmp_path, install_block, fail=True)
        assert result.returncode != 0
        calls = (tmp_path / "calls").read_text().splitlines()
        assert calls == [f"chown -R sp-rtk-base:sp-rtk-base {tmp_path / 'prefix'}"]

    def test_latest_moves_the_relay_too(
        self, tmp_path: Path, install_block: str
    ) -> None:
        self._run(tmp_path, install_block)
        args = (tmp_path / "pip-args").read_text().splitlines()
        assert "--upgrade" in args
        assert "sp-rtk-base" in args
        assert "sp-rtk-base-relay" in args

    def test_chosen_version_moves_the_relay_within_its_pin(
        self, tmp_path: Path, install_block: str
    ) -> None:
        """The Relay is named without a version, so pip picks the newest
        one the chosen app's own requirement allows, upgrading or
        downgrading as needed."""
        self._run(tmp_path, install_block, version="0.3.0")
        args = (tmp_path / "pip-args").read_text().splitlines()
        assert "--upgrade" in args
        assert "sp-rtk-base==0.3.0" in args
        assert "sp-rtk-base-relay" in args

    def test_service_user_is_overridable_like_install_sh(
        self, upgrade_text: str
    ) -> None:
        assert 'SERVICE_USER="${SERVICE_USER:-sp-rtk-base}"' in upgrade_text


# ---------------------------------------------------------------------------
# install.sh: the Relay
# ---------------------------------------------------------------------------


class TestInstallMovesTheRelay:
    def test_pip_install_names_the_relay(self, install_text: str) -> None:
        step5 = _between(install_text, "# Step 5 —", "installed_version=")
        pip_lines = [line for line in step5.splitlines() if 'pip" install' in line]
        assert len(pip_lines) == 1
        assert '"$pin" "$RELAY_NAME"' in pip_lines[0]
        assert "--upgrade" in pip_lines[0]
        assert 'RELAY_NAME="sp-rtk-base-relay"' in install_text


# ---------------------------------------------------------------------------
# install.sh: host files come from the release tag
# ---------------------------------------------------------------------------


class TestFetchDeployFile:
    """``fetch_deploy_file <path under deploy/> <dest>``: the one way
    install.sh lays down a file from the repo."""

    @pytest.fixture(scope="class")
    def helper(self, install_text: str) -> str:
        return _function(install_text, "fetch_deploy_file")

    def _run(
        self, tmp_path: Path, helper: str, *, src_dir: str, tag: str
    ) -> subprocess.CompletedProcess[str]:
        calls = tmp_path / "calls"
        script = (
            "set -euo pipefail\n"
            'die() { echo "DIE $*" >&2; exit 1; }\n'
            f'curl() {{ echo "curl $*" >> "{calls}"; }}\n'
            f'install() {{ echo "install $*" >> "{calls}"; }}\n'
            f'chmod() {{ echo "chmod $*" >> "{calls}"; }}\n'
            f'RAW_REPO_URL="{RAW_BASE}"\n'
            f'DEPLOY_SRC_DIR="{src_dir}"\n'
            f'RELEASE_TAG="{tag}"\n'
            f"{helper}\n"
            'fetch_deploy_file sp-rtk-base.service "$1"\n'
        )
        return subprocess.run(
            ["bash", "-c", script, "_", "/dest/unit"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_without_a_checkout_fetches_from_the_release_tag(
        self, tmp_path: Path, helper: str
    ) -> None:
        result = self._run(tmp_path, helper, src_dir="", tag="v0.9.0")
        assert result.returncode == 0, result.stderr
        calls = (tmp_path / "calls").read_text()
        assert f"{RAW_BASE}/v0.9.0/deploy/sp-rtk-base.service" in calls
        assert "-o /dest/unit" in calls
        assert "/main/" not in calls

    def test_from_a_checkout_uses_the_checkout(
        self, tmp_path: Path, helper: str
    ) -> None:
        checkout = tmp_path / "deploy"
        checkout.mkdir()
        (checkout / "sp-rtk-base.service").write_text("[Unit]\n")
        result = self._run(tmp_path, helper, src_dir=str(checkout), tag="v0.9.0")
        assert result.returncode == 0, result.stderr
        calls = (tmp_path / "calls").read_text()
        assert f"{checkout}/sp-rtk-base.service /dest/unit" in calls
        assert "curl" not in calls

    def test_never_falls_back_to_main_without_a_tag(
        self, tmp_path: Path, helper: str
    ) -> None:
        result = self._run(tmp_path, helper, src_dir="", tag="")
        assert result.returncode != 0
        assert "DIE" in result.stderr
        assert not (tmp_path / "calls").exists()


class TestInstallFetchesByTag:
    def test_no_fetch_from_main(self, install_text: str) -> None:
        code = "\n".join(
            line
            for line in install_text.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "sp-rtk-base/main" not in code.replace(
            "/main/deploy/upgrade.sh",
            "",  # the closing hint, not a fetch
        )
        assert "REPO_RAW_BASE" not in install_text

    def test_every_host_file_goes_through_the_helper(self, install_text: str) -> None:
        for rel in (
            "shared/net-provision-teardown.sh",
            "sp-rtk-base.service",
            "polkit/10-sp-rtk-base-net-provision.rules",
            "sp-rtk-base-net-provision.service",
        ):
            assert re.search(rf"fetch_deploy_file {re.escape(rel)} ", install_text), rel
        # Only the helper itself calls curl to download.
        curl_calls = re.findall(r"^\s*curl -fsSL", install_text, re.MULTILINE)
        assert len(curl_calls) == 1

    def test_tag_is_the_installed_version(self, install_text: str) -> None:
        assert 'RELEASE_TAG="v${installed_version}"' in install_text

    def test_tag_is_resolved_before_the_first_fetch(self, install_text: str) -> None:
        tag_idx = install_text.index('RELEASE_TAG="v${installed_version}"')
        installed_idx = install_text.index("installed_version=")
        first_fetch = re.search(r"^\s*fetch_deploy_file ", install_text, re.MULTILINE)
        assert first_fetch
        assert installed_idx < tag_idx < first_fetch.start()

    def test_teardown_lib_is_loaded_before_mode_resolution(
        self, install_text: str
    ) -> None:
        load_idx = install_text.index(
            "fetch_deploy_file shared/net-provision-teardown.sh"
        )
        assert load_idx < install_text.index("# Step 6.5 — Determine deployment mode")


class TestLocalCheckoutDetection:
    """A piped ``curl | bash`` install has no checkout, whatever the
    current directory holds; a script run from a file uses its own dir."""

    @pytest.fixture(scope="class")
    def detection(self, install_text: str) -> str:
        return _between(install_text, 'DEPLOY_SRC_DIR=""', "\nfi\n")

    def test_piped_install_ignores_files_in_the_current_directory(
        self, tmp_path: Path, detection: str
    ) -> None:
        (tmp_path / "sp-rtk-base.service").write_text("stale\n")
        result = subprocess.run(
            ["bash", "-s"],
            input=f'{detection}\necho "src=[$DEPLOY_SRC_DIR]"\n',
            capture_output=True,
            text=True,
            check=True,
            cwd=tmp_path,
        )
        assert result.stdout.strip() == "src=[]"

    def test_script_file_uses_its_own_directory(
        self, tmp_path: Path, detection: str
    ) -> None:
        deploy = tmp_path / "checkout" / "deploy"
        deploy.mkdir(parents=True)
        script = deploy / "install.sh"
        script.write_text(f'{detection}\necho "src=[$DEPLOY_SRC_DIR]"\n')
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            check=True,
            cwd=tmp_path,
        )
        assert result.stdout.strip() == f"src=[{deploy}]"


# ---------------------------------------------------------------------------
# install.sh: closing hint
# ---------------------------------------------------------------------------


class TestClosingHint:
    def test_upgrade_hint_names_upgrade_sh(self, install_text: str) -> None:
        step9 = install_text[install_text.index("# Step 9 — Final status") :]
        hint = [line for line in step9.splitlines() if "Upgrade:" in line]
        assert len(hint) == 1
        assert "upgrade.sh" in hint[0]
        assert "pip install" not in step9


# ---------------------------------------------------------------------------
# install.sh: a piped install stops before changing a base it can't finish
# ---------------------------------------------------------------------------


def _fake_venv_python(tmp_path: Path, *, editable_from: str | None) -> Path:
    """A venv whose ``bin/python`` answers the checkout question the way
    the real one would for an editable install (or a regular one)."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    answer = f'echo "{editable_from}"' if editable_from else "true"
    python.write_text(f"#!/bin/sh\ncat >/dev/null\n{answer}\n")
    python.chmod(0o755)
    return venv


class TestPipedInstallOfACheckout:
    """test-base, 2026-10-07: a base running sp-rtk-base editable from a
    git checkout reports a stamped version (``0.9.0+dev-49a04a0``) that
    has no release tag. A piped ``curl | bash`` install then failed
    half-way, after pip, on a 404 for its first host file. It must stop
    before changing anything and say to run the checkout's installer."""

    @pytest.fixture(scope="class")
    def guard(self, install_text: str) -> str:
        return _between(
            install_text,
            "# A base that runs sp-rtk-base from a checkout",
            "# End of the checkout guard\n",
        )

    def _run(
        self, tmp_path: Path, guard: str, *, src_dir: str, editable_from: str | None
    ) -> subprocess.CompletedProcess[str]:
        venv = _fake_venv_python(tmp_path, editable_from=editable_from)
        script = (
            "set -euo pipefail\n"
            'die() { echo "DIE $*" >&2; exit 1; }\n'
            f'VENV_DIR="{venv}"\n'
            f'DEPLOY_SRC_DIR="{src_dir}"\n'
            f"{guard}\n"
            "echo CONTINUED\n"
        )
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )

    def test_piped_install_over_a_checkout_stops_and_names_its_installer(
        self, tmp_path: Path, guard: str
    ) -> None:
        result = self._run(
            tmp_path, guard, src_dir="", editable_from="/opt/sp-rtk-base-src"
        )
        assert result.returncode != 0
        assert "CONTINUED" not in result.stdout
        assert "sudo /opt/sp-rtk-base-src/deploy/install.sh" in result.stderr

    def test_the_checkouts_own_installer_goes_on(
        self, tmp_path: Path, guard: str
    ) -> None:
        result = self._run(
            tmp_path,
            guard,
            src_dir="/opt/sp-rtk-base-src/deploy",
            editable_from="/opt/sp-rtk-base-src",
        )
        assert result.returncode == 0, result.stderr
        assert "CONTINUED" in result.stdout

    def test_a_regular_install_goes_on(self, tmp_path: Path, guard: str) -> None:
        result = self._run(tmp_path, guard, src_dir="", editable_from=None)
        assert result.returncode == 0, result.stderr
        assert "CONTINUED" in result.stdout

    def test_a_fresh_host_without_a_venv_goes_on(
        self, tmp_path: Path, guard: str
    ) -> None:
        script = (
            "set -euo pipefail\n"
            'die() { echo "DIE $*" >&2; exit 1; }\n'
            f'VENV_DIR="{tmp_path / "missing"}"\n'
            'DEPLOY_SRC_DIR=""\n'
            f"{guard}\n"
            "echo CONTINUED\n"
        )
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert "CONTINUED" in result.stdout

    def test_the_guard_runs_before_any_change(self, install_text: str) -> None:
        guard_idx = install_text.index("# A base that runs sp-rtk-base from a checkout")
        assert guard_idx < install_text.index("# Step 1 — OS dependencies")


class TestRequireReleaseTag:
    """After pip, a piped install checks the installed version's tag exists
    before it fetches anything, so a version without one stops with a
    clear message instead of a bare curl 404."""

    @pytest.fixture(scope="class")
    def helper(self, install_text: str) -> str:
        return _function(install_text, "require_release_tag")

    def _run(
        self, tmp_path: Path, helper: str, *, src_dir: str, tag_exists: bool
    ) -> subprocess.CompletedProcess[str]:
        calls = tmp_path / "calls"
        status = 0 if tag_exists else 22
        script = (
            "set -euo pipefail\n"
            'die() { echo "DIE $*" >&2; exit 1; }\n'
            f'curl() {{ echo "curl $*" >> "{calls}"; return {status}; }}\n'
            f'RAW_REPO_URL="{RAW_BASE}"\n'
            f'DEPLOY_SRC_DIR="{src_dir}"\n'
            'RELEASE_TAG="v0.9.0+dev-49a04a0"\n'
            'installed_version="0.9.0+dev-49a04a0"\n'
            f"{helper}\n"
            "require_release_tag\n"
            "echo CONTINUED\n"
        )
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )

    def test_a_version_without_a_tag_stops_with_a_clear_message(
        self, tmp_path: Path, helper: str
    ) -> None:
        result = self._run(tmp_path, helper, src_dir="", tag_exists=False)
        assert result.returncode != 0
        assert "CONTINUED" not in result.stdout
        assert "0.9.0+dev-49a04a0" in result.stderr
        assert "checkout" in result.stderr

    def test_a_tagged_release_goes_on(self, tmp_path: Path, helper: str) -> None:
        result = self._run(tmp_path, helper, src_dir="", tag_exists=True)
        assert result.returncode == 0, result.stderr
        assert "CONTINUED" in result.stdout
        calls = (tmp_path / "calls").read_text()
        assert f"{RAW_BASE}/v0.9.0+dev-49a04a0/deploy/" in calls

    def test_a_checkout_needs_no_tag(self, tmp_path: Path, helper: str) -> None:
        result = self._run(tmp_path, helper, src_dir="/src/deploy", tag_exists=False)
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "calls").exists()

    def test_it_runs_before_the_first_fetch(self, install_text: str) -> None:
        check = re.search(r"^require_release_tag$", install_text, re.MULTILINE)
        first_fetch = re.search(r"^\s*fetch_deploy_file ", install_text, re.MULTILINE)
        assert check and first_fetch
        assert check.start() < first_fetch.start()


class TestVenvOwnershipRightAfterPip:
    def test_chown_comes_before_anything_that_can_fail(self, install_text: str) -> None:
        installed_idx = install_text.index("installed_version=")
        chown_idx = install_text.index(
            'chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_PREFIX"'
        )
        tag_check = re.search(r"^require_release_tag$", install_text, re.MULTILINE)
        assert tag_check
        assert installed_idx < chown_idx < tag_check.start()
