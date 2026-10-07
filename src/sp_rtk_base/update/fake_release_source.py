"""A fake release source: PyPI documents and GitHub files read from a directory.

The e2e server runs with ``SP_RTK_BASE_FAKE_PYPI_DIR`` pointing here
instead of at PyPI and GitHub:

- ``https://pypi.org/pypi/<package>[/<version>]/json`` is served from
  ``<dir>/<package>[-<version>].json``;
- ``https://raw.githubusercontent.com/<path>`` and
  ``https://api.github.com/<path>`` are served from ``<dir>/<host>/<path>``.

A missing file is an HTTP 404, so a test fails a fetch by removing one.
"""

from __future__ import annotations

import email.message
import io
import re
import urllib.error
from pathlib import Path

from sp_rtk_base.update.release import (
    GITHUB_API_BASE_URL,
    GITHUB_RAW_BASE_URL,
    PYPI_BASE_URL,
    Fetch,
)

_PYPI_URL = re.compile(
    re.escape(PYPI_BASE_URL)
    + r"/(?P<package>[A-Za-z0-9._-]+)(?:/(?P<version>[A-Za-z0-9.+!_-]+))?/json"
)
_GITHUB_URL = re.compile(
    "(?:"
    + "|".join(re.escape(base) for base in (GITHUB_RAW_BASE_URL, GITHUB_API_BASE_URL))
    + r")/(?P<path>[A-Za-z0-9._/-]+)"
)


def _not_found(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url, 404, "Not Found", email.message.Message(), io.BytesIO(b"")
    )


def directory_fetch(directory: Path) -> Fetch:
    """A :data:`Fetch` serving PyPI JSON and GitHub files from ``directory``."""

    def fetch(url: str) -> bytes:
        path = _path_for(directory, url)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise _not_found(url) from exc

    return fetch


def _path_for(directory: Path, url: str) -> Path:
    match = _PYPI_URL.fullmatch(url)
    if match is not None:
        name = match["package"]
        if match["version"]:
            name += f"-{match['version']}"
        return directory / f"{name}.json"
    match = _GITHUB_URL.fullmatch(url)
    if match is not None and ".." not in match["path"].split("/"):
        return directory / url.removeprefix("https://")
    raise OSError(f"The fake release source serves no {url}")
