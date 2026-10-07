"""PyPI as recorded on 2026-10-07 (``tests/fixtures/pypi``), plus releases a
test publishes into it: a :data:`~sp_rtk_base.update.release.Fetch` that
serves those documents.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

PYPI_DIR = Path(__file__).parent / "pypi"
APP_INDEX = "https://pypi.org/pypi/sp-rtk-base/json"
RELAY_INDEX = "https://pypi.org/pypi/sp-rtk-base-relay/json"


def _recorded(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((PYPI_DIR / f"{name}.json").read_text())
    return data


class FakePyPI:
    """PyPI as recorded, plus whatever a test publishes into it."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {
            APP_INDEX: _recorded("sp-rtk-base"),
            RELAY_INDEX: _recorded("sp-rtk-base-relay"),
            "https://pypi.org/pypi/sp-rtk-base/0.9.0/json": _recorded(
                "sp-rtk-base-0.9.0"
            ),
        }
        self.down: set[str] = set()
        self.fetched: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.fetched.append(url)
        if url in self.down or url not in self.docs:
            raise OSError(f"HTTP 404 for {url}")
        return json.dumps(self.docs[url]).encode()

    def publish_app(
        self,
        version: str,
        *,
        requires_python: str | None = ">=3.10",
        relay_pin: str = "<5,>=4.1.0",
        yanked: bool | list[bool] = False,
        files: int = 2,
    ) -> None:
        """Publish an SP-Base release, with its own per-version document."""
        self._publish(APP_INDEX, "sp_rtk_base", version, requires_python, yanked, files)
        doc = copy.deepcopy(_recorded("sp-rtk-base-0.9.0"))
        doc["info"]["version"] = version
        doc["info"]["requires_python"] = requires_python
        doc["info"]["requires_dist"] = [
            r
            if not r.startswith("sp-rtk-base-relay")
            else f"sp-rtk-base-relay{relay_pin}"
            for r in doc["info"]["requires_dist"]
        ]
        self.docs[f"https://pypi.org/pypi/sp-rtk-base/{version}/json"] = doc

    def publish_relay(
        self,
        version: str,
        *,
        requires_python: str | None = ">=3.10",
        yanked: bool | list[bool] = False,
        files: int = 2,
    ) -> None:
        self._publish(
            RELAY_INDEX, "sp_rtk_base_relay", version, requires_python, yanked, files
        )

    def write_to(self, directory: Path) -> None:
        """Lay the documents out for ``SP_RTK_BASE_FAKE_PYPI_DIR`` (e2e)."""
        directory.mkdir(parents=True, exist_ok=True)
        for url, doc in self.docs.items():
            name = (
                url.removeprefix("https://pypi.org/pypi/")
                .removesuffix("/json")
                .replace("/", "-")
            )
            (directory / f"{name}.json").write_text(json.dumps(doc))

    def point_info_version_at(self, url: str, version: str) -> None:
        self.docs[url]["info"]["version"] = version

    def _publish(
        self,
        index: str,
        stem: str,
        version: str,
        requires_python: str | None,
        yanked: bool | list[bool],
        files: int,
    ) -> None:
        flags = yanked if isinstance(yanked, list) else [yanked] * files
        names = [f"{stem}-{version}-py3-none-any.whl", f"{stem}-{version}.tar.gz"]
        self.docs[index]["releases"][version] = [
            {
                "filename": names[i % 2],
                "packagetype": "bdist_wheel" if i % 2 == 0 else "sdist",
                "requires_python": requires_python,
                "yanked": flag,
                "yanked_reason": "withdrawn" if flag else None,
            }
            for i, flag in enumerate(flags)
        ]
