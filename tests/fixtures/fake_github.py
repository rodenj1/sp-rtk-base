"""GitHub as recorded on 2026-10-07 (``tests/fixtures/github``), plus whatever
a test publishes into it: a :data:`~sp_rtk_base.update.release.Fetch` that
serves raw ``CHANGELOG.md`` files and the REST "get a release by tag" answer.

Recorded:

- ``CHANGELOG.md`` of sp-rtk-base at ``v0.9.0`` and of the Relay at
  ``v4.1.0``. Their known gaps: sp-rtk-base 0.5.1 and the withdrawn
  0.3.23 have no section, nor does Relay 2.1.3.
- The Release of sp-rtk-base ``v0.5.1`` and of the Relay ``v2.1.3`` (the
  gaps' fallback), and the 404 GitHub answers for ``v0.3.23``, which has
  no tag and no Release.

Like GitHub, a missing file is an HTTP 404; a test fails a URL with
another status (403 for the rate limit) through :meth:`FakeGitHub.fail`.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
from pathlib import Path
from typing import Any

GITHUB_DIR = Path(__file__).parent / "github"
RAW = "https://raw.githubusercontent.com/rodenj1"
API = "https://api.github.com/repos/rodenj1"


def changelog_url(repo: str, version: str) -> str:
    return f"{RAW}/{repo}/v{version}/CHANGELOG.md"


def release_url(repo: str, version: str) -> str:
    return f"{API}/{repo}/releases/tags/v{version}"


def recorded_changelog(repo: str, version: str) -> str:
    return (GITHUB_DIR / repo / f"CHANGELOG-v{version}.md").read_text()


def _recorded_release(repo: str, version: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (GITHUB_DIR / repo / f"release-v{version}.json").read_text()
    )
    return data


_NOT_FOUND = (GITHUB_DIR / "sp-rtk-base" / "release-v0.3.23.json").read_bytes()


def http_error(url: str, code: int, body: bytes = b"") -> urllib.error.HTTPError:
    """The error :func:`urllib.request.urlopen` raises for ``code``."""
    return urllib.error.HTTPError(
        url, code, f"HTTP {code}", email.message.Message(), io.BytesIO(body)
    )


class FakeGitHub:
    """GitHub as recorded, plus whatever a test publishes into it."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {
            changelog_url("sp-rtk-base", "0.9.0"): recorded_changelog(
                "sp-rtk-base", "0.9.0"
            ).encode(),
            changelog_url("sp-rtk-base-relay", "4.1.0"): recorded_changelog(
                "sp-rtk-base-relay", "4.1.0"
            ).encode(),
        }
        for repo, version in (("sp-rtk-base", "0.5.1"), ("sp-rtk-base-relay", "2.1.3")):
            self.files[release_url(repo, version)] = json.dumps(
                _recorded_release(repo, version)
            ).encode()
        self.failing: dict[str, int] = {}
        self.fetched: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.fetched.append(url)
        if url in self.failing:
            raise http_error(url, self.failing[url])
        if url not in self.files:
            raise http_error(url, 404, _NOT_FOUND)
        return self.files[url]

    def fail(self, url: str, code: int = 403) -> None:
        """Answer ``url`` with ``code`` (403 is GitHub's rate limit)."""
        self.failing[url] = code

    def publish_changelog(
        self, repo: str, version: str, sections: str, *, on_top_of: str
    ) -> None:
        """Tag ``v<version>`` with ``sections`` above the changelog at ``on_top_of``."""
        old = self.files[changelog_url(repo, on_top_of)].decode()
        at = old.index("## ") if old.startswith("## ") else old.index("\n## ") + 1
        new = f"{old[:at]}{sections.strip()}\n\n{old[at:]}"
        self.files[changelog_url(repo, version)] = new.encode()

    def publish_release(
        self, repo: str, version: str, body: str | None, *, published: str
    ) -> None:
        """A GitHub Release for ``v<version>``, shaped like the recorded ones."""
        doc = _recorded_release(repo, "0.5.1" if repo == "sp-rtk-base" else "2.1.3")
        doc.update(tag_name=f"v{version}", name=f"v{version}", body=body)
        doc["published_at"] = f"{published}T12:00:00Z"
        self.files[release_url(repo, version)] = json.dumps(doc).encode()

    def remove(self, url: str) -> None:
        del self.files[url]

    def write_to(self, directory: Path) -> None:
        """Lay the files out for ``SP_RTK_BASE_FAKE_PYPI_DIR`` (e2e).

        ``https://<host>/<path>`` is served from ``<directory>/<host>/<path>``.
        """
        for url, body in self.files.items():
            path = directory / url.removeprefix("https://")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)


class Web:
    """PyPI and GitHub behind one fetch, as the base sees them."""

    def __init__(self, pypi: Any, github: FakeGitHub) -> None:
        self.pypi = pypi
        self.github = github

    def __call__(self, url: str) -> bytes:
        if url.startswith("https://pypi.org/"):
            result: bytes = self.pypi(url)
            return result
        return self.github(url)
