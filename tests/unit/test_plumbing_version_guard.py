"""The CI guard: host files that ``install.sh`` lays down can't change
without a bump to ``deploy/plumbing-version`` (sp-rtk-base#242).

Runs ``tools/check_plumbing_version.py`` against a throwaway git repo.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "tools" / "check_plumbing_version.py"

INSTALL_SH = """\
#!/usr/bin/env bash
fetch_deploy_file() { :; }
fetch_deploy_file sp-rtk-base.service "$SYSTEMD_UNIT"
fetch_deploy_file sp-rtk-base-update.service "$UPDATE_SYSTEMD_UNIT"
    fetch_deploy_file polkit/10-net.rules "$POLKIT_RULE_DEST"
fetch_deploy_file plumbing-version "$plumbing_tmp"
"""


# Run outside any git hook's environment (GIT_DIR, GIT_INDEX_FILE, ...).
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=True,
            env=ENV,
        ).stdout

    def write(self, path: str, text: str) -> None:
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def commit(self, message: str = "change") -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)

    def guard(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GUARD), "--repo", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
            env=ENV,
        )


@pytest.fixture()
def repo(tmp_path: Path) -> Repo:
    """A release ``v1.0.0`` with host files and no plumbing version yet."""
    r = Repo(tmp_path / "repo")
    r.write("deploy/install.sh", INSTALL_SH)
    r.write("deploy/sp-rtk-base.service", "[Service]\n")
    r.write("deploy/sp-rtk-base-update.service", "[Service]\n")
    r.write("deploy/polkit/10-net.rules", "rule\n")
    r.write("deploy/upgrade.sh", "#!/bin/sh\n")
    r.write("src/app.py", "x = 1\n")
    r.commit("v1.0.0")
    r.git("tag", "v1.0.0")
    return r


class TestTheGuard:
    @pytest.mark.parametrize(
        "path",
        [
            "deploy/sp-rtk-base.service",
            "deploy/sp-rtk-base-update.service",
            "deploy/polkit/10-net.rules",
        ],
    )
    def test_fails_a_host_file_change_without_a_bump(
        self, repo: Repo, path: str
    ) -> None:
        repo.write(path, "changed\n")
        repo.commit()

        result = repo.guard()

        assert result.returncode == 1
        assert path in result.stderr

    def test_passes_with_a_bump(self, repo: Repo) -> None:
        repo.write("deploy/sp-rtk-base-update.service", "changed\n")
        repo.write("deploy/plumbing-version", "1\n")
        repo.commit()

        assert repo.guard().returncode == 0

    def test_a_bump_must_go_past_the_tags_number(self, repo: Repo) -> None:
        repo.write("deploy/plumbing-version", "2\n")
        repo.commit()
        repo.git("tag", "v1.1.0")
        repo.write("deploy/sp-rtk-base.service", "changed\n")
        repo.commit()

        assert repo.guard().returncode == 1

        repo.write("deploy/plumbing-version", "3\n")
        repo.commit()

        assert repo.guard().returncode == 0

    def test_removing_a_host_file_counts(self, repo: Repo) -> None:
        (repo.root / "deploy/polkit/10-net.rules").unlink()
        repo.write("deploy/install.sh", INSTALL_SH.replace("polkit", "#"))
        repo.commit()

        assert repo.guard().returncode == 1

    def test_passes_when_only_other_files_changed(self, repo: Repo) -> None:
        repo.write("src/app.py", "x = 2\n")
        repo.write("deploy/upgrade.sh", "#!/bin/sh\necho\n")
        repo.commit()

        assert repo.guard().returncode == 0

    def test_passes_with_no_tag_yet(self, tmp_path: Path) -> None:
        r = Repo(tmp_path / "fresh")
        r.write("deploy/install.sh", INSTALL_SH)
        r.commit()

        assert r.guard().returncode == 0

    def test_a_shallow_clone_cant_tell(self, repo: Repo, tmp_path: Path) -> None:
        repo.write("src/app.py", "x = 3\n")
        repo.commit()
        shallow = tmp_path / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{repo.root}", str(shallow)],
            check=True,
            env=ENV,
        )

        result = subprocess.run(
            [sys.executable, str(GUARD), "--repo", str(shallow)],
            capture_output=True,
            text=True,
            check=False,
            env=ENV,
        )

        assert result.returncode == 2
        assert "Shallow" in result.stderr


def test_this_repo_reads_its_install_sh() -> None:
    """The guard finds the real host files in ``install.sh``."""
    sys.path.insert(0, str(GUARD.parent))
    try:
        from check_plumbing_version import host_files
    finally:
        sys.path.pop(0)

    files = host_files((REPO_ROOT / "deploy" / "install.sh").read_text())

    assert {
        "deploy/sp-rtk-base.service",
        "deploy/sp-rtk-base-update.service",
        "deploy/sp-rtk-base-update.path",
        "deploy/sp-rtk-base-net-provision.service",
        "deploy/polkit/10-sp-rtk-base-net-provision.rules",
        "deploy/shared/net-provision-teardown.sh",
    } == files
