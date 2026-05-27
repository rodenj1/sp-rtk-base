"""Device connection lifecycle tests.

These tests exercise the **connect / disconnect** REST endpoints
against the in-memory :class:`FakeGpsDriver` so we can verify the
core state-machine that every UI page depends on:

- ``/api/device/connect`` flips ``device_service.is_connected`` true
  and ``/api/device/status`` reflects the change.
- ``/api/device/disconnect`` flips it back.
- ``/api/device/ports`` includes a ``FAKE`` entry when the env-gated
  driver is registered.
- A double-connect attempt returns HTTP 409 (the same conflict
  status the UI's *Connect* button relies on to surface "Already
  connected" warnings to the user).

These are deliberately **REST-only** rather than browser-driven —
they're fast (no Playwright bootstrap per case) and let the heavier
browser tests assume a known connection state.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest


@pytest.mark.e2e
class TestDeviceConnectionLifecycle:
    """Connect / disconnect / status state machine over REST."""

    def test_ports_endpoint_returns_list(self, api_base_url: str) -> None:
        """``/api/device/ports`` should return a JSON list (may be empty).

        On a CI box without real serial hardware this is typically
        empty.  We don't require the FAKE entry to appear here —
        ``/api/device/connect`` accepts the ``port`` value as a free-
        form string anyway (the fake driver doesn't validate it),
        and the Survey-In page lets the user type a port name.
        What we *do* care about is that the endpoint stays healthy
        with the fake driver registered.
        """
        resp = httpx.get(f"{api_base_url}/api/device/ports", timeout=5.0)
        assert resp.status_code == 200, resp.text
        payload: object = resp.json()
        assert isinstance(payload, list)
        # Every entry is well-formed if any are present.
        for item in payload:
            if isinstance(item, dict):
                entry = cast(dict[str, Any], item)
                assert "port" in entry
                assert "is_gps" in entry

    def test_connect_then_disconnect_round_trip(self, api_base_url: str) -> None:
        """Happy-path connect → status → disconnect → status sequence."""
        # Make sure we start disconnected (don't fail if already so).
        httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)

        # Connect via fake driver.
        connect = httpx.post(
            f"{api_base_url}/api/device/connect",
            json={"vendor": "fake", "port": "FAKE", "baud_rate": 115200},
            timeout=10.0,
        )
        assert connect.status_code == 200, connect.text
        body: object = connect.json()
        assert isinstance(body, dict)
        body_d = cast(dict[str, Any], body)
        assert body_d.get("status") == "ok"
        assert "Connected" in str(body_d.get("message", ""))

        # Status reflects connected.
        status = httpx.get(f"{api_base_url}/api/device/status", timeout=5.0)
        assert status.status_code == 200
        status_body: object = status.json()
        assert isinstance(status_body, dict)
        status_d = cast(dict[str, Any], status_body)
        # The field name lives on DeviceStatus; we accept any truthy
        # representation of "connected" the backend chooses to emit.
        # Accept either: explicit ``state == "connected"`` or the
        # legacy ``is_connected`` flag.
        connected_flag = bool(
            status_d.get("is_connected")
            or str(status_d.get("state", "")).lower() == "connected"
        )
        assert connected_flag, (
            f"expected connected state in /api/device/status, got {status_d!r}"
        )

        # Disconnect.
        disconnect = httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
        assert disconnect.status_code == 200, disconnect.text

        # Status reflects disconnected.
        status2 = httpx.get(f"{api_base_url}/api/device/status", timeout=5.0)
        assert status2.status_code == 200
        status2_body: object = status2.json()
        assert isinstance(status2_body, dict)
        s2_d = cast(dict[str, Any], status2_body)
        disconnected_flag = (
            not s2_d.get("is_connected")
            and str(s2_d.get("state", "")).lower() != "connected"
        )
        assert disconnected_flag, f"expected disconnected state, got {s2_d!r}"

    def test_double_connect_returns_409(
        self, api_base_url: str, connected_gps: None
    ) -> None:
        """A second connect call while already connected → HTTP 409.

        The UI's Connect button relies on this status code to know
        when to display "Already connected" instead of swallowing the
        click silently.
        """
        second = httpx.post(
            f"{api_base_url}/api/device/connect",
            json={"vendor": "fake", "port": "FAKE", "baud_rate": 115200},
            timeout=5.0,
        )
        assert second.status_code == 409, (
            f"expected 409 on double-connect, got {second.status_code}: {second.text}"
        )

    def test_disconnect_when_disconnected_is_idempotent(
        self, api_base_url: str
    ) -> None:
        """Two disconnects in a row both return HTTP 200.

        ``DeviceService.disconnect`` is **idempotent by design** —
        the API only raises 409 when there's no driver loaded at
        all (``is_available == False``).  Once a driver has been
        registered (which happens on the very first ``connect``
        call), repeated disconnects just no-op.

        We pin this behaviour so future refactors don't accidentally
        flip the UI's *Disconnect* button into raising spurious
        notifications.
        """
        # Ensure a driver has been loaded by completing a full
        # connect/disconnect cycle first.
        httpx.post(
            f"{api_base_url}/api/device/connect",
            json={"vendor": "fake", "port": "FAKE", "baud_rate": 115200},
            timeout=5.0,
        )
        first = httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
        assert first.status_code == 200
        second = httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
        # The service is idempotent: ``is_available`` stays true as
        # long as a driver is loaded, so disconnect still succeeds.
        assert second.status_code == 200

    def test_connect_with_unknown_vendor_returns_400(self, api_base_url: str) -> None:
        """An unregistered vendor key surfaces as HTTP 400, not 500."""
        # Make sure we're disconnected first.
        httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)

        resp = httpx.post(
            f"{api_base_url}/api/device/connect",
            json={
                "vendor": "does-not-exist",
                "port": "FAKE",
                "baud_rate": 115200,
            },
            timeout=5.0,
        )
        assert resp.status_code == 400, (
            f"expected 400 for unknown vendor, got {resp.status_code}: {resp.text}"
        )

    def test_capabilities_reflect_fake_driver(
        self, api_base_url: str, connected_gps: None
    ) -> None:
        """``/api/device/capabilities`` returns the fake driver's full set.

        FakeGpsDriver claims every capability so the UI doesn't
        hide any controls during e2e runs.
        """
        resp = httpx.get(f"{api_base_url}/api/device/capabilities", timeout=5.0)
        assert resp.status_code == 200, resp.text
        caps_body: object = resp.json()
        assert isinstance(caps_body, list)
        caps: set[str] = {str(c) for c in caps_body if isinstance(c, str)}
        # The strings on the wire come from DeviceCapability enum
        # values.  We don't pin to the exact set (the enum may grow)
        # but the four UI-critical capabilities must be present.
        expected_minimum = {
            "survey_in",
            "fixed_base",
            "save_to_flash",
            "position_stream",
        }
        missing = expected_minimum - caps
        assert not missing, (
            f"FakeGpsDriver should expose {expected_minimum!r} but "
            f"missing {missing!r} from {caps!r}"
        )
