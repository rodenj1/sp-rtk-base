"""Release notes: what changes between the running release and the target.

For each package the notes cover every release after the running one up to
and including the target, newest first. They come from ``CHANGELOG.md`` at
the target's tag, one raw fetch per repository: the file is cumulative, so
it holds every section in the range. A release without a section falls
back to its GitHub Release body; without that either, it has no notes.
Pre-release sections inside the range fold under the stable release they
led to (0.4.0's changes were mostly written up under its betas).

GitHub failing (a 403 or 429 rate limit included) never fails anything:
that package's notes are marked as not loaded, and Update still works.

Like :mod:`sp_rtk_base.update.release`, this module imports neither NiceGUI,
FastAPI nor the app's services. Notes are Markdown, exactly as GitHub holds
them; whoever shows them must escape raw HTML.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
from typing import Any, Literal, cast

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict

from sp_rtk_base.update.release import (
    APP_PACKAGE,
    GITHUB_API_BASE_URL,
    GITHUB_REPOS,
    RELAY_PACKAGE,
    Fetch,
    ReleaseCheckError,
    ReleaseTarget,
    github_raw_url,
    stable_versions,
)

logger = logging.getLogger(__name__)

NoteSource = Literal["changelog", "release", "none", "unavailable"]
"""Where a release's notes came from.

``none``: neither a changelog section nor a Release body exists.
``unavailable``: its Release body was needed but GitHub didn't answer.
"""


class PreReleaseNote(BaseModel):
    """A pre-release section folded under the stable release it led to."""

    model_config = ConfigDict(frozen=True)

    version: str
    """As the changelog writes it, e.g. ``0.4.0-beta.1``."""
    date: str | None
    body: str


class ReleaseNote(BaseModel):
    """One release's notes."""

    model_config = ConfigDict(frozen=True)

    version: str
    date: str | None
    """``YYYY-MM-DD``, when known."""
    source: NoteSource
    body: str = ""
    """Markdown as written; empty unless ``source`` is changelog or release."""
    includes: tuple[PreReleaseNote, ...] = ()
    """Its pre-releases inside the range, newest first."""


class PackageNotes(BaseModel):
    """One package's notes from the running release to the target."""

    model_config = ConfigDict(frozen=True)

    loaded: bool = True
    """False when GitHub failed: the notes "couldn't be loaded"."""
    releases: tuple[ReleaseNote, ...] = ()
    """Newest first; empty when the package doesn't change."""
    error: str | None = None


class ReleaseNotes(BaseModel):
    """The notes for an Update: SP-Base and the Relay."""

    model_config = ConfigDict(frozen=True)

    app: PackageNotes
    relay: PackageNotes


def release_notes(
    fetch: Fetch, running_app: str, running_relay: str, target: ReleaseTarget
) -> ReleaseNotes:
    """The notes for an Update from the running releases to ``target``."""
    return ReleaseNotes(
        app=package_notes(fetch, APP_PACKAGE, running=running_app, target=target.app),
        relay=package_notes(
            fetch, RELAY_PACKAGE, running=running_relay, target=target.relay
        ),
    )


def package_notes(
    fetch: Fetch, package: str, *, running: str, target: str
) -> PackageNotes:
    """``package``'s notes for every release in ``running < v <= target``.

    Never raises: a GitHub failure is reported in the result.
    """
    try:
        low, high = Version(running), Version(target)
    except InvalidVersion as exc:
        return PackageNotes(loaded=False, error=str(exc))
    if high <= low:
        return PackageNotes()

    url = github_raw_url(package, target, "CHANGELOG.md")
    try:
        changelog = fetch(url).decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Couldn't load the release notes at %s: %s", url, exc)
        return PackageNotes(loaded=False, error=f"Couldn't fetch {url}: {exc}")

    sections = [s for s in _sections(changelog) if low < s.version <= high]
    stable = {s.version: s for s in sections if not s.version.is_prerelease}
    in_range = set(stable) | {high}
    try:
        in_range |= {v for v in stable_versions(fetch, package) if low < v <= high}
    except ReleaseCheckError as exc:
        # PyPI just answered the check; if it fails now, the changelog alone
        # still names the releases (only a gap would go unnoticed).
        logger.warning("Couldn't list %s releases for the notes: %s", package, exc)

    fallback = _ReleaseBodies(fetch, package)
    releases: list[ReleaseNote] = []
    for version in sorted(in_range, reverse=True):
        section = stable.get(version)
        if section is not None and section.body:
            note = ReleaseNote(
                version=str(version),
                date=section.date,
                source="changelog",
                body=section.body,
            )
        else:
            note = fallback.note(version)
        releases.append(note)

    # Fold each pre-release under the oldest release in range above it.
    folded: dict[str, list[PreReleaseNote]] = {}
    ascending = sorted(in_range)
    for section in sections:
        if not section.version.is_prerelease:
            continue
        owner = next(v for v in ascending if v > section.version)
        folded.setdefault(str(owner), []).append(
            PreReleaseNote(version=section.label, date=section.date, body=section.body)
        )
    releases = [
        r.model_copy(update={"includes": tuple(folded[r.version])})
        if r.version in folded
        else r
        for r in releases
    ]
    return PackageNotes(releases=tuple(releases))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^## +(?P<label>v?\S+) +\((?P<date>[^)]*)\)[ \t]*$", re.M)
_ANY_H2 = re.compile(r"^## ", re.M)


class _Section:
    def __init__(self, label: str, version: Version, date: str, body: str) -> None:
        self.label = label
        self.version = version
        self.date = date or None
        self.body = body


def _sections(changelog: str) -> list[_Section]:
    """Each ``## vX.Y.Z (date)`` section, up to the next level-2 heading."""
    found: list[_Section] = []
    for match in _HEADING.finditer(changelog):
        label = match["label"].removeprefix("v")
        try:
            version = Version(label)
        except InvalidVersion:
            continue
        start = match.end()
        end_match = _ANY_H2.search(changelog, start)
        end = end_match.start() if end_match else len(changelog)
        found.append(
            _Section(
                label, version, match["date"].strip(), changelog[start:end].strip()
            )
        )
    return found


class _ReleaseBodies:
    """The GitHub Release fallback, one REST call per release that needs it.

    After a failure other than 404 (the rate limit, GitHub down), no more
    calls are made: the remaining gaps are unavailable too.
    """

    def __init__(self, fetch: Fetch, package: str) -> None:
        self._fetch = fetch
        self._repo = GITHUB_REPOS[package]
        self._failed = False

    def note(self, version: Version) -> ReleaseNote:
        if self._failed:
            return ReleaseNote(version=str(version), date=None, source="unavailable")
        url = f"{GITHUB_API_BASE_URL}/repos/{self._repo}/releases/tags/v{version}"
        try:
            doc = json.loads(self._fetch(url))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return ReleaseNote(version=str(version), date=None, source="none")
            return self._unavailable(version, url, exc)
        except Exception as exc:
            return self._unavailable(version, url, exc)
        if not isinstance(doc, dict):
            return ReleaseNote(version=str(version), date=None, source="none")
        release = cast("dict[str, Any]", doc)
        body = release.get("body")
        published = release.get("published_at")
        date = published[:10] if isinstance(published, str) and published else None
        if not isinstance(body, str) or not body.strip():
            return ReleaseNote(version=str(version), date=date, source="none")
        return ReleaseNote(
            version=str(version), date=date, source="release", body=body.strip()
        )

    def _unavailable(self, version: Version, url: str, exc: Exception) -> ReleaseNote:
        logger.warning("Couldn't load the Release at %s: %s", url, exc)
        self._failed = True
        return ReleaseNote(version=str(version), date=None, source="unavailable")
