"""End-to-end: a dashboard refresh updates the page in place (rtk_development#177).

Each element NiceGUI creates and later deletes leaves a little memory
behind in the browser. A dashboard that rebuilt its cards on every
status refresh therefore grew without bound while it stayed on screen
(about 8–10 MB/h on a base station), until the Pi was short of memory and
the page reloaded itself. So, once the page has loaded, a refresh changes
text, colours and visibility, and creates or deletes elements only when the
page's structure changes (a destination row appears or goes).

The Relay runs from a TCP input that streams bytes, so the rates, totals
and Uptime change on every refresh. The refresh interval is shortened so
that 100 refreshes take seconds. The module runs its own server, because
it saves an input and outputs and starts the Relay.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import running_server

#: The shortened status refresh interval, in seconds.
POLL_S = 0.2

#: Every NiceGUI element's id on the page (``c<N>``).
_ELEMENT_IDS_JS = """() => Array.from(document.querySelectorAll('[id]'))
    .map(e => e.id).filter(i => /^c\\d+$/.test(i))"""


class StreamingSource:
    """A TCP input for the Relay: streams bytes while it is up.

    ``drop()`` closes the connection and refuses new ones (the Relay's
    input disconnects); ``restore()`` accepts again.
    """

    def __init__(self) -> None:
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.settimeout(0.2)
        self.port: int = self._server.getsockname()[1]
        self._up = threading.Event()
        self._up.set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                continue
            with conn:
                if not self._up.is_set():
                    continue
                while self._up.is_set() and not self._stop.is_set():
                    try:
                        conn.sendall(os.urandom(200))
                    except OSError:
                        break
                    self._stop.wait(0.05)

    def drop(self) -> None:
        self._up.clear()

    def restore(self) -> None:
        self._up.set()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._server.close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _add_output(api: str, name: str) -> None:
    resp = httpx.post(
        f"{api}/api/destinations",
        json={
            "name": name,
            "type": "tcp_server",
            "enabled": True,
            "filter": {"mode": "pass_all", "message_ids": []},
            "config": {"host": "127.0.0.1", "port": _free_port(), "max_clients": 1},
        },
        timeout=5.0,
    )
    assert resp.status_code == 201, resp.text


def _relay(api: str, action: str) -> None:
    resp = httpx.post(f"{api}/api/relay/{action}", timeout=15.0)
    assert resp.status_code == 200, resp.text


@pytest.fixture(scope="module")
def source() -> Iterator[StreamingSource]:
    src = StreamingSource()
    yield src
    src.close()


@pytest.fixture(scope="module")
def own_server(
    tmp_path_factory: pytest.TempPathFactory,
    fake_pypi_dir: Path,
    update_dir: Path,
    host_setup_file: Path,
    portal_agent_socket: Path,
    source: StreamingSource,
) -> Iterator[str]:
    with running_server(
        tmp_path_factory,
        fake_pypi_dir,
        update_dir,
        host_setup_file,
        portal_agent_socket,
    ) as url:
        resp = httpx.put(
            f"{url}/api/settings", json={"status_poll_interval": POLL_S}, timeout=5.0
        )
        assert resp.status_code == 200, resp.text
        resp = httpx.put(
            f"{url}/api/input",
            json={
                "source": "tcp",
                "config": {"host": "127.0.0.1", "port": source.port},
            },
            timeout=5.0,
        )
        assert resp.status_code in (200, 201), resp.text
        _add_output(url, "e2e-out-a")
        yield url


@pytest.fixture()
def relay_running(own_server: str, source: StreamingSource) -> Iterator[str]:
    source.restore()
    _relay(own_server, "start")
    try:
        yield own_server
    finally:
        # Not asserted: a test that failed midway may have left it stopped.
        httpx.post(f"{own_server}/api/relay/stop", timeout=15.0)


def _element_ids(page: Page) -> set[str]:
    return set(page.evaluate(_ELEMENT_IDS_JS))


def _settled_ids(page: Page) -> set[str]:
    """The page's element ids once two refreshes in a row agree."""
    previous = _element_ids(page)
    for _ in range(50):
        page.wait_for_timeout(int(POLL_S * 1000 * 2))
        current = _element_ids(page)
        if current == previous:
            return current
        previous = current
    return previous


def _assert_stable_for(page: Page, refreshes: int) -> None:
    baseline = _settled_ids(page)
    for i in range(refreshes):
        page.wait_for_timeout(int(POLL_S * 1000))
        current = _element_ids(page)
        assert current == baseline, (
            f"refresh {i + 1}: {len(current - baseline)} elements created, "
            f"{len(baseline - current)} deleted"
        )


@pytest.mark.e2e
def test_refreshes_keep_the_same_elements_while_the_values_change(
    page: Page, relay_running: str
) -> None:
    page.goto(f"{relay_running}/")
    expect(page.get_by_text("Running", exact=True)).to_be_visible()
    expect(page.get_by_text("e2e-out-a", exact=True)).to_be_visible()
    received = page.get_by_text("total: ").first
    expect(received).to_be_visible()
    before = received.inner_text()

    _assert_stable_for(page, refreshes=100)

    # The values did change: the page was updated, not frozen.
    expect(received).not_to_have_text(before)
    nav = page.evaluate("performance.getEntriesByType('navigation')[0].type")
    assert nav != "reload"


@pytest.mark.e2e
def test_stopping_and_starting_the_relay_shows_without_a_reload(
    page: Page, relay_running: str
) -> None:
    page.goto(f"{relay_running}/")
    expect(page.get_by_text("Running", exact=True)).to_be_visible()
    expect(page.get_by_text(r"Uptime: ").first).to_be_visible()

    _relay(relay_running, "stop")
    expect(page.get_by_text("Stopped", exact=True)).to_be_visible()
    expect(page.get_by_text("Not running").filter(visible=True)).to_have_count(3)
    expect(page.get_by_text("Uptime: ").filter(visible=True)).to_have_count(0)
    expect(page.get_by_text("Destination Details")).to_be_hidden()
    _assert_stable_for(page, refreshes=10)

    _relay(relay_running, "start")
    expect(page.get_by_text("Running", exact=True)).to_be_visible()
    expect(page.get_by_text("Not running").filter(visible=True)).to_have_count(0)
    expect(page.get_by_text("e2e-out-a", exact=True)).to_be_visible()
    _assert_stable_for(page, refreshes=10)


@pytest.mark.e2e
def test_an_added_destination_gets_a_row_and_the_rest_stay(
    page: Page, relay_running: str
) -> None:
    page.goto(f"{relay_running}/")
    row_a = page.get_by_test_id("destination-row-e2e-out-a")
    expect(row_a).to_be_visible()
    row_a_id = row_a.get_attribute("id")

    # The new destination joins the Relay on its next start. A refresh
    # while it is stopped hides Destination details; the rows stay.
    _relay(relay_running, "stop")
    expect(page.get_by_text("Stopped", exact=True)).to_be_visible()
    expect(page.get_by_text("Destination Details")).to_be_hidden()
    _add_output(relay_running, "e2e-out-b")
    try:
        _relay(relay_running, "start")
        expect(page.get_by_test_id("destination-row-e2e-out-b")).to_be_visible()
        expect(page.get_by_text("Active").locator("..")).to_contain_text("2/2")
        # The row that was already there is the same element.
        expect(page.get_by_test_id("destination-row-e2e-out-a")).to_have_attribute(
            "id", row_a_id or ""
        )
        _assert_stable_for(page, refreshes=10)
    finally:
        _relay(relay_running, "stop")
        resp = httpx.delete(f"{relay_running}/api/destinations/e2e-out-b", timeout=5.0)
        assert resp.status_code in (200, 204), resp.text
        _relay(relay_running, "start")


@pytest.mark.e2e
def test_the_input_dropping_and_returning_shows_in_the_card_and_the_banner(
    page: Page, relay_running: str, source: StreamingSource
) -> None:
    page.goto(f"{relay_running}/")
    expect(page.get_by_text("Connected", exact=True)).to_be_visible()
    expect(page.get_by_text("⚠ Issues Detected")).to_be_hidden()

    source.drop()
    expect(page.get_by_text("Disconnected", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_text("• Input source is disconnected")).to_be_visible()
    expect(page.get_by_text("⚠ Issues Detected")).to_be_visible()

    source.restore()
    expect(page.get_by_text("Connected", exact=True)).to_be_visible(timeout=30_000)
    expect(page.get_by_text("⚠ Issues Detected")).to_be_hidden()
    _assert_stable_for(page, refreshes=10)


@pytest.mark.e2e
@pytest.mark.parametrize("display", ["detailed", "compact"])
def test_signal_quality_refreshes_keep_the_same_elements(
    page: Page, own_server: str, display: str
) -> None:
    resp = httpx.put(
        f"{own_server}/api/settings",
        json={"dashboard_signal_display": display},
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text
    try:
        page.goto(f"{own_server}/")
        testid = (
            "signal-quality-card" if display == "detailed" else "signal-quality-chip"
        )
        expect(page.get_by_test_id(testid)).to_be_visible()
        # Signal Quality refreshes every 2 s on its own timer: cover three.
        baseline = _settled_ids(page)
        for _ in range(3):
            page.wait_for_timeout(2_100)
            assert _element_ids(page) == baseline
    finally:
        httpx.put(
            f"{own_server}/api/settings",
            json={"dashboard_signal_display": "detailed"},
            timeout=5.0,
        )
