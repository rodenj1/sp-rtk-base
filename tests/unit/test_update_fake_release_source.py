"""The fake release source e2e runs the server against (no real PyPI)."""

from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from sp_rtk_base.update.fake_release_source import directory_fetch
from sp_rtk_base.update.release import ReleaseTarget, resolve_release
from sp_rtk_base.update.release_notes import package_notes
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.fake_pypi import FakePyPI

RECORDED = Path(__file__).parent.parent / "fixtures" / "pypi"


def test_serves_recorded_pypi_documents_by_url() -> None:
    fetch = directory_fetch(RECORDED)

    assert resolve_release(fetch, (3, 11)) == ReleaseTarget(app="0.9.0", relay="4.1.0")


def test_a_missing_document_is_a_failed_fetch(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        directory_fetch(tmp_path)("https://pypi.org/pypi/sp-rtk-base/json")


def test_only_pypi_urls(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        directory_fetch(tmp_path)("https://example.com/../../etc/passwd")


def test_serves_github_files_laid_out_by_the_fake(tmp_path: Path) -> None:
    pypi, github = FakePyPI(), FakeGitHub()
    pypi.write_to(tmp_path)
    github.write_to(tmp_path)
    fetch = directory_fetch(tmp_path)

    notes = package_notes(fetch, "sp-rtk-base", running="0.5.0", target="0.9.0")

    assert notes.loaded
    assert [r.source for r in notes.releases if r.version == "0.5.1"] == ["release"]


def test_a_missing_github_file_is_a_404(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/rodenj1/sp-rtk-base/releases/tags/v0.3.23"

    with pytest.raises(urllib.error.HTTPError) as caught:
        directory_fetch(tmp_path)(url)

    assert caught.value.code == 404


@pytest.mark.parametrize(
    "url",
    [
        "https://raw.githubusercontent.com/rodenj1/../../../etc/passwd",
        "https://api.github.com/repos/rodenj1/sp-rtk-base/%2e%2e/x",
    ],
)
def test_never_outside_the_directory(tmp_path: Path, url: str) -> None:
    with pytest.raises(OSError):
        directory_fetch(tmp_path / "fake")(url)
