"""A fake release source: PyPI documents read from a directory.

The e2e server runs with ``SP_RTK_BASE_FAKE_PYPI_DIR`` pointing here
instead of at PyPI. ``https://pypi.org/pypi/<package>[/<version>]/json``
is served from ``<dir>/<package>[-<version>].json``; a missing file is a
failed fetch, so a test fails the check by removing one.
"""

from __future__ import annotations

import re
from pathlib import Path

from sp_rtk_base.update.release import PYPI_BASE_URL, Fetch

_PYPI_URL = re.compile(
    re.escape(PYPI_BASE_URL)
    + r"/(?P<package>[A-Za-z0-9._-]+)(?:/(?P<version>[A-Za-z0-9.+!_-]+))?/json"
)


def directory_fetch(directory: Path) -> Fetch:
    """A :data:`Fetch` serving PyPI JSON from files in ``directory``."""

    def fetch(url: str) -> bytes:
        match = _PYPI_URL.fullmatch(url)
        if match is None:
            raise OSError(f"The fake release source serves no {url}")
        name = match["package"]
        if match["version"]:
            name += f"-{match['version']}"
        path = directory / f"{name}.json"
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise OSError(f"HTTP 404 for {url}") from exc

    return fetch
