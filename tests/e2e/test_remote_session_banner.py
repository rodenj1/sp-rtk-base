"""The "Remote session active" banner on every base page (ticket #210,
spec #199).

Someone at the base sees who is using it remotely, on any page opened
directly on ``:8080``. The app reads the live Remote sessions from the
Portal agent's status (the socket is faked here, ``portal_agent`` in
conftest).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.fixtures.fake_agent_socket import FakeAgentSocket

CONNECTED = {
    "state": "connected",
    "base_id": "k3mnp4qrst",
    "account": "op@example.com",
}


def _session(who: str, *, shared: bool = False) -> dict[str, object]:
    return {"who": who, "since": "2026-01-01T12:01:00+00:00", "shared": shared}


@pytest.mark.e2e
def test_no_banner_while_nobody_uses_the_base_remotely(
    page: Page, base_url: str, portal_agent: FakeAgentSocket
) -> None:
    portal_agent.status = dict(CONNECTED)
    page.goto(f"{base_url}/settings")
    expect(page.locator("text=SP-Base").first).to_be_visible(timeout=15_000)
    page.wait_for_timeout(3_000)  # the app reads the agent every 2 s

    expect(page.get_by_test_id("remote-session-banner")).to_have_count(0)


@pytest.mark.e2e
def test_the_banner_names_who_is_on_a_remote_session_until_it_ends(
    page: Page, base_url: str, portal_agent: FakeAgentSocket
) -> None:
    portal_agent.status = CONNECTED | {
        "sessions": [
            _session("op@example.com"),
            _session("sam@example.com", shared=True),
        ]
    }
    page.goto(f"{base_url}/survey")

    expect(page.get_by_test_id("remote-session-banner")).to_have_text(
        "Remote session active: op@example.com, sam@example.com via the Portal",
        timeout=15_000,
    )

    portal_agent.status = CONNECTED | {"sessions": [_session("op@example.com")]}
    expect(page.get_by_test_id("remote-session-banner")).to_have_text(
        "Remote session active: op@example.com via the Portal", timeout=5_000
    )

    portal_agent.status = dict(CONNECTED)
    expect(page.get_by_test_id("remote-session-banner")).to_have_count(0, timeout=5_000)
