"""Tunnel opens and closes in the Dashboard's Event log (ticket #92).

The app reads them from the Portal agent's status (the socket is faked
here) and raises them in the backfill and live, with the Relay stopped.

These entries stay in the app's Event log for good, so this module runs its
own server (and fake agent) rather than leave them in the shared one, where
"No events yet" is expected.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import running_server
from tests.fixtures.fake_agent_socket import FakeAgentSocket


@pytest.fixture(scope="module")
def portal_agent() -> Iterator[FakeAgentSocket]:
    with tempfile.TemporaryDirectory(prefix="e2e-agent-") as d:
        with FakeAgentSocket(Path(d) / "control.sock") as fake:
            yield fake


@pytest.fixture(scope="module")
def own_server(
    tmp_path_factory: pytest.TempPathFactory,
    fake_pypi_dir: Path,
    update_dir: Path,
    host_setup_file: Path,
    portal_agent: FakeAgentSocket,
) -> Iterator[str]:
    with running_server(
        tmp_path_factory, fake_pypi_dir, update_dir, host_setup_file, portal_agent.path
    ) as url:
        yield url


def _connected(*events: dict[str, str]) -> dict[str, object]:
    return {
        "state": "connected",
        "base_id": "k3mnp4qrst",
        "account": "op@example.com",
        "tunnel_events": list(events),
    }


def _event(event_id: str, event: str) -> dict[str, str]:
    # Newer than anything else in the log, so it shows at the top.
    at = datetime.now(timezone.utc) + timedelta(hours=1)
    return {"id": event_id, "event": event, "at": at.isoformat()}


@pytest.mark.e2e
def test_a_tunnel_open_is_in_the_event_log_when_the_page_loads(
    page: Page, own_server: str, portal_agent: FakeAgentSocket
) -> None:
    portal_agent.status = _connected(_event("e2e-open-1", "opened"))
    page.wait_for_timeout(3_000)  # the app reads the agent every 2 s

    page.goto(f"{own_server}/")

    log = page.get_by_test_id("event-log")
    expect(log.get_by_text("portal.tunnel_opened", exact=True)).to_be_visible(
        timeout=15_000
    )
    expect(
        log.get_by_text("Remote access tunnel to the Portal opened", exact=True)
    ).to_be_visible()


@pytest.mark.e2e
def test_a_tunnel_close_shows_up_live(
    page: Page, own_server: str, portal_agent: FakeAgentSocket
) -> None:
    page.goto(f"{own_server}/")
    log = page.get_by_test_id("event-log")
    expect(log).to_be_visible(timeout=15_000)
    page.wait_for_timeout(1_000)  # the live websocket connects

    portal_agent.status = _connected(_event("e2e-close-1", "closed"))

    expect(
        log.get_by_text("Remote access tunnel to the Portal closed", exact=True)
    ).to_be_visible(timeout=10_000)
