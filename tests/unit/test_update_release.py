"""Release resolution: which SP-Base and Relay an Update would install.

Runs against PyPI responses recorded on 2026-10-07 (``tests/fixtures/pypi``),
then against that same index after further releases are "published" into it,
to cover what PyPI didn't happen to hold that day: pre-releases, yanked
releases, versions without files, a Relay pin move, a newer Python floor.
"""

from __future__ import annotations

import pytest

from sp_rtk_base.update.release import (
    NewerNeedsPython,
    ReleaseCheckError,
    ReleaseTarget,
    resolve_release,
    urllib_fetch,
)
from tests.fixtures.fake_pypi import APP_INDEX, RELAY_INDEX, FakePyPI


@pytest.fixture()
def pypi() -> FakePyPI:
    return FakePyPI()


PY311 = (3, 11)


class TestAsRecorded:
    def test_newest_release_and_the_newest_relay_its_pin_allows(
        self, pypi: FakePyPI
    ) -> None:
        assert resolve_release(pypi, PY311) == ReleaseTarget(app="0.9.0", relay="4.1.0")

    def test_resolution_reads_pypi_not_a_cache(self, pypi: FakePyPI) -> None:
        resolve_release(pypi, PY311)

        assert pypi.fetched == [
            APP_INDEX,
            "https://pypi.org/pypi/sp-rtk-base/0.9.0/json",
            RELAY_INDEX,
        ]


class TestNeverOffered:
    """Releases that are never a target, even when ``info.version`` says so."""

    @pytest.mark.parametrize("version", ["0.10.0rc1", "0.10.0b2", "0.10.0.dev3"])
    def test_pre_and_dev_releases(self, pypi: FakePyPI, version: str) -> None:
        pypi.publish_app(version)
        pypi.point_info_version_at(APP_INDEX, version)

        assert resolve_release(pypi, PY311).app == "0.9.0"

    def test_a_release_with_every_file_yanked(self, pypi: FakePyPI) -> None:
        pypi.publish_app("0.10.0", yanked=True)
        pypi.point_info_version_at(APP_INDEX, "0.10.0")

        assert resolve_release(pypi, PY311).app == "0.9.0"

    def test_a_release_with_one_file_yanked_is_still_offered(
        self, pypi: FakePyPI
    ) -> None:
        pypi.publish_app("0.10.0", yanked=[True, False])

        assert resolve_release(pypi, PY311).app == "0.10.0"

    def test_a_release_without_files(self, pypi: FakePyPI) -> None:
        pypi.publish_app("0.10.0", files=0)
        pypi.point_info_version_at(APP_INDEX, "0.10.0")

        assert resolve_release(pypi, PY311).app == "0.9.0"

    def test_versions_compare_as_versions_not_strings(self, pypi: FakePyPI) -> None:
        # "0.10.0" sorts before "0.9.0" as text.
        pypi.publish_app("0.10.0")

        assert resolve_release(pypi, PY311).app == "0.10.0"


class TestRelayTarget:
    def test_newest_relay_inside_the_new_releases_pin(self, pypi: FakePyPI) -> None:
        pypi.publish_relay("4.2.0")
        pypi.publish_relay("5.0.0")
        pypi.publish_app("0.10.0", relay_pin="<5,>=4.2.0")

        assert resolve_release(pypi, PY311) == ReleaseTarget(
            app="0.10.0", relay="4.2.0"
        )

    def test_the_pin_comes_from_the_new_release_not_the_running_one(
        self, pypi: FakePyPI
    ) -> None:
        pypi.publish_relay("5.0.0")
        pypi.publish_app("0.10.0", relay_pin="<6,>=5.0.0")

        assert resolve_release(pypi, PY311).relay == "5.0.0"

    def test_pre_release_and_yanked_relays_are_skipped(self, pypi: FakePyPI) -> None:
        pypi.publish_relay("4.2.0rc1")
        pypi.publish_relay("4.3.0", yanked=True)
        pypi.publish_relay("4.4.0", files=0)
        pypi.point_info_version_at(RELAY_INDEX, "4.3.0")

        assert resolve_release(pypi, PY311).relay == "4.1.0"

    def test_no_relay_inside_the_pin_fails_the_check(self, pypi: FakePyPI) -> None:
        pypi.publish_app("0.10.0", relay_pin="<6,>=5.0.0")

        with pytest.raises(ReleaseCheckError):
            resolve_release(pypi, PY311)

    def test_a_release_naming_no_relay_fails_the_check(self, pypi: FakePyPI) -> None:
        pypi.publish_app("0.10.0")
        doc = pypi.docs["https://pypi.org/pypi/sp-rtk-base/0.10.0/json"]
        doc["info"]["requires_dist"] = ["fastapi>=0.135.3", "not a requirement !!"]

        with pytest.raises(ReleaseCheckError):
            resolve_release(pypi, PY311)

    def test_a_relay_requirement_for_an_extra_is_not_the_pin(
        self, pypi: FakePyPI
    ) -> None:
        pypi.publish_relay("5.0.0")
        pypi.publish_app("0.10.0")
        doc = pypi.docs["https://pypi.org/pypi/sp-rtk-base/0.10.0/json"]
        doc["info"]["requires_dist"].insert(0, 'sp-rtk-base-relay>=5; extra == "next"')

        assert resolve_release(pypi, PY311).relay == "4.1.0"


class TestRequiresPython:
    def test_newest_release_that_runs_here_is_offered_with_a_note(
        self, pypi: FakePyPI
    ) -> None:
        pypi.publish_app("0.10.0")
        pypi.publish_app("0.11.0", requires_python=">=3.12")
        pypi.publish_app("0.11.1", requires_python=">=3.12")

        assert resolve_release(pypi, PY311) == ReleaseTarget(
            app="0.10.0",
            relay="4.1.0",
            newer_needs_python=NewerNeedsPython(version="0.11.1", python="3.12"),
        )

    def test_a_far_newer_python_is_still_named(self, pypi: FakePyPI) -> None:
        pypi.publish_app("0.10.0", requires_python=">=3.99")

        target = resolve_release(pypi, (3, 10, 4))

        assert target.newer_needs_python == NewerNeedsPython(
            version="0.10.0", python="3.99"
        )

    def test_a_python_that_fits_gets_no_note(self, pypi: FakePyPI) -> None:
        pypi.publish_app("0.11.0", requires_python=">=3.12")

        assert resolve_release(pypi, (3, 12)) == ReleaseTarget(
            app="0.11.0", relay="4.1.0"
        )

    def test_a_patch_level_floor_is_checked_against_the_running_patch(
        self, pypi: FakePyPI
    ) -> None:
        pypi.publish_app("0.10.0", requires_python=">=3.11.4")

        assert resolve_release(pypi, (3, 11, 7)).app == "0.10.0"
        assert resolve_release(pypi, (3, 11, 2)) == ReleaseTarget(
            app="0.9.0",
            relay="4.1.0",
            newer_needs_python=NewerNeedsPython(version="0.10.0", python="3.11.4"),
        )

    def test_no_release_runs_here(self, pypi: FakePyPI) -> None:
        with pytest.raises(ReleaseCheckError):
            resolve_release(pypi, (3, 9))

    def test_an_unreadable_requires_python_is_never_offered(
        self, pypi: FakePyPI
    ) -> None:
        pypi.publish_app("0.10.0", requires_python="three point twelve")

        target = resolve_release(pypi, PY311)

        assert target.app == "0.9.0"
        assert target.newer_needs_python == NewerNeedsPython(
            version="0.10.0", python=None
        )


class TestPyPIFails:
    @pytest.mark.parametrize(
        "url",
        [APP_INDEX, RELAY_INDEX, "https://pypi.org/pypi/sp-rtk-base/0.9.0/json"],
    )
    def test_any_pypi_failure_fails_the_check(self, pypi: FakePyPI, url: str) -> None:
        pypi.down.add(url)

        with pytest.raises(ReleaseCheckError):
            resolve_release(pypi, PY311)

    @pytest.mark.parametrize("body", [b"<html>503</html>", b"[]", b'{"info": {}}'])
    def test_an_answer_that_isnt_a_release_list_fails_the_check(
        self, body: bytes
    ) -> None:
        with pytest.raises(ReleaseCheckError):
            resolve_release(lambda _url: body, PY311)


def test_the_updater_can_import_it_without_the_web_app() -> None:
    """The updater console script runs resolution with no NiceGUI or FastAPI."""
    import subprocess
    import sys

    code = (
        "import sys, sp_rtk_base.update.release\n"
        "loaded = [m for m in ('nicegui', 'fastapi', 'sp_rtk_base.services')"
        " if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


class TestOddIndexes:
    def test_a_release_without_requires_python_runs_anywhere(
        self, pypi: FakePyPI
    ) -> None:
        pypi.publish_app("0.10.0", requires_python=None)
        pypi.publish_relay("4.2.0", requires_python=None)

        assert resolve_release(pypi, (3, 9)) == ReleaseTarget(
            app="0.10.0", relay="4.2.0"
        )

    def test_unreadable_version_keys_and_file_lists_are_skipped(
        self, pypi: FakePyPI
    ) -> None:
        releases = pypi.docs[APP_INDEX]["releases"]
        releases["not-a-version"] = releases["0.9.0"]
        releases["0.99.0"] = "not a file list"

        assert resolve_release(pypi, PY311).app == "0.9.0"

    def test_no_stable_release_at_all(self, pypi: FakePyPI) -> None:
        pypi.docs[APP_INDEX]["releases"] = {}
        pypi.publish_app("1.0.0rc1")

        with pytest.raises(ReleaseCheckError):
            resolve_release(pypi, PY311)


class TestUrllibFetch:
    def test_reads_the_body_over_http(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *_args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            body = urllib_fetch(f"http://127.0.0.1:{server.server_port}/x/json")
        finally:
            thread.join(5)
            server.server_close()

        assert body == b'{"ok": true}'
