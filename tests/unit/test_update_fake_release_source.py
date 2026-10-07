"""The fake release source e2e runs the server against (no real PyPI)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sp_rtk_base.update.fake_release_source import directory_fetch
from sp_rtk_base.update.release import ReleaseTarget, resolve_release

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
