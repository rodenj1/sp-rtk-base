"""Release resolution: the SP-Base and Relay an Update would install.

The app (to show an Available update) and the updater (to check the
request against its own answer) both call :func:`resolve_release`, so the
two sides of that check run the same logic.

Versions come from PyPI's JSON API, never from ``info.version`` alone:
pre-releases, dev releases, versions without files and versions whose
every file is yanked are never a target.

This module must stay importable without NiceGUI, FastAPI or the app's
services: the updater console script runs it on its own. HTTP goes
through an injectable :data:`Fetch`, so tests replay recorded responses.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any, cast

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict

APP_PACKAGE = "sp-rtk-base"
RELAY_PACKAGE = "sp-rtk-base-relay"
PYPI_BASE_URL = "https://pypi.org/pypi"
FETCH_TIMEOUT_SECONDS = 15.0

Fetch = Callable[[str], bytes]
"""Return the body at a URL, or raise on any failure (network, HTTP status)."""

PythonVersion = tuple[int, ...]
"""The running Python, e.g. ``(3, 11, 2)`` from ``sys.version_info[:3]``."""


class ReleaseCheckError(Exception):
    """The newest release couldn't be worked out (PyPI failed or made no sense)."""


class NewerNeedsPython(BaseModel):
    """A release newer than the target that this base's Python can't run."""

    model_config = ConfigDict(frozen=True)

    version: str
    """The newest such release."""
    python: str | None
    """The oldest Python ``A.B`` it runs on, when that can be told."""


class ReleaseTarget(BaseModel):
    """What an Update installs now: exactly these two versions."""

    model_config = ConfigDict(frozen=True)

    app: str
    relay: str
    newer_needs_python: NewerNeedsPython | None = None


def urllib_fetch(url: str) -> bytes:
    """The real :data:`Fetch`: a plain HTTPS GET with a timeout."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        body: bytes = response.read()
        return body


def resolve_release(fetch: Fetch, python: PythonVersion) -> ReleaseTarget:
    """Work out the target SP-Base and Relay for a base running ``python``.

    The target is the newest stable, non-yanked SP-Base release whose
    ``requires_python`` admits ``python``, and the newest stable,
    non-yanked Relay inside that release's ``requires_dist`` pin.

    Raises:
        ReleaseCheckError: PyPI failed, or no release fits.
    """
    running = Version(".".join(str(part) for part in python))
    app_releases = _stable_releases(_get_json(fetch, _index_url(APP_PACKAGE)))
    if not app_releases:
        raise ReleaseCheckError(f"PyPI lists no stable {APP_PACKAGE} release")

    fitting = [r for r in app_releases if _admits(r.requires_python, running)]
    if not fitting:
        raise ReleaseCheckError(f"No {APP_PACKAGE} release runs on Python {running}")
    target = fitting[0]
    newer_needs_python = None
    if app_releases[0].version > target.version:
        newest = app_releases[0]
        newer_needs_python = NewerNeedsPython(
            version=str(newest.version),
            python=_oldest_python(newest.requires_python, running),
        )

    pin = _relay_pin(fetch, str(target.version))
    relay_releases = _stable_releases(_get_json(fetch, _index_url(RELAY_PACKAGE)))
    relays = [
        r
        for r in relay_releases
        if pin.contains(r.version) and _admits(r.requires_python, running)
    ]
    if not relays:
        raise ReleaseCheckError(
            f"No {RELAY_PACKAGE} release matches {APP_PACKAGE} "
            f"{target.version}'s pin {pin}"
        )
    return ReleaseTarget(
        app=str(target.version),
        relay=str(relays[0].version),
        newer_needs_python=newer_needs_python,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _Release:
    """One stable release that has at least one file that isn't yanked."""

    def __init__(self, version: Version, requires_python: str | None) -> None:
        self.version = version
        self.requires_python = requires_python


def _index_url(package: str, version: str | None = None) -> str:
    if version is None:
        return f"{PYPI_BASE_URL}/{package}/json"
    return f"{PYPI_BASE_URL}/{package}/{version}/json"


def _get_json(fetch: Fetch, url: str) -> dict[str, Any]:
    try:
        body = fetch(url)
    except Exception as exc:
        raise ReleaseCheckError(f"Couldn't fetch {url}: {exc}") from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ReleaseCheckError(f"{url} didn't return JSON") from exc
    if not isinstance(data, dict):
        raise ReleaseCheckError(f"{url} returned an unexpected document")
    return cast("dict[str, Any]", data)


def _stable_releases(index: dict[str, Any]) -> list[_Release]:
    """Every stable release with a live file, newest first."""
    raw = index.get("releases")
    if not isinstance(raw, dict):
        raise ReleaseCheckError("PyPI's answer has no release list")
    releases = cast("dict[str, object]", raw)
    found: list[_Release] = []
    for raw_version, files in releases.items():
        try:
            version = Version(raw_version)
        except InvalidVersion:
            continue
        if version.is_prerelease or version.is_devrelease:
            continue
        live = [f for f in _files(files) if not f.get("yanked")]
        if not live:
            continue
        found.append(_Release(version, _requires_python(live)))
    found.sort(key=lambda r: r.version, reverse=True)
    return found


def _files(files: object) -> list[dict[str, Any]]:
    if not isinstance(files, list):
        return []
    items = cast("list[object]", files)
    return [cast("dict[str, Any]", f) for f in items if isinstance(f, dict)]


def _requires_python(files: Iterable[dict[str, Any]]) -> str | None:
    for f in files:
        value = f.get("requires_python")
        if value:
            return str(value)
    return None


def _admits(requires_python: str | None, running: Version) -> bool:
    if not requires_python:
        return True
    try:
        return SpecifierSet(requires_python).contains(running)
    except InvalidSpecifier:
        return False


def _oldest_python(requires_python: str | None, running: Version) -> str | None:
    """The oldest Python newer than ``running`` that the specifier admits.

    ``A.B`` when its first release fits, else ``A.B.C``.
    """
    major = running.major
    for minor in range(running.minor, 100):
        # Skip a minor quickly unless its first or a late patch fits.
        ends = (Version(f"{major}.{minor}.0"), Version(f"{major}.{minor}.99"))
        if not any(_admits(requires_python, v) for v in ends):
            continue
        for patch in range(100):
            candidate = Version(f"{major}.{minor}.{patch}")
            if candidate > running and _admits(requires_python, candidate):
                return f"{major}.{minor}" + (f".{patch}" if patch else "")
    return None


def _relay_pin(fetch: Fetch, app_version: str) -> SpecifierSet:
    """The Relay specifier in an SP-Base release's ``requires_dist``."""
    doc = _get_json(fetch, _index_url(APP_PACKAGE, app_version))
    info = doc.get("info")
    requires: object = (
        cast("dict[str, object]", info).get("requires_dist")
        if isinstance(info, dict)
        else None
    )
    lines = cast("list[object]", requires) if isinstance(requires, list) else []
    for line in lines:
        try:
            req = Requirement(str(line))
        except InvalidRequirement:
            continue
        if canonicalize_name(req.name) != RELAY_PACKAGE:
            continue
        if req.marker is not None and not req.marker.evaluate({"extra": ""}):
            continue
        return req.specifier
    raise ReleaseCheckError(
        f"{APP_PACKAGE} {app_version} doesn't name a {RELAY_PACKAGE} version"
    )
