# pyright: reportUnknownMemberType=false
"""Integration tests for destination management via the REST API.

Tests adding, editing, deleting, and toggling destinations through
the API, both while the relay is stopped and while it is running.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from tests.fixtures.tcp_source_simulator import TCPSourceSimulator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TCP_DEST_PORT_A = 19901
TCP_DEST_PORT_B = 19902


def _configure_input(client: TestClient, host: str, port: int) -> None:
    """Configure the TCP input source via API."""
    resp = client.put(
        "/api/input",
        json={"source": "tcp", "config": {"host": host, "port": port}},
    )
    assert resp.status_code == 200


def _add_tcp_destination(
    client: TestClient, name: str, port: int, enabled: bool = True
) -> dict[str, Any]:
    """Add a tcp_server destination via API and return the response body."""
    resp = client.post(
        "/api/destinations",
        json={
            "name": name,
            "type": "tcp_server",
            "enabled": enabled,
            "config": {"port": port, "host": "127.0.0.1"},
        },
    )
    assert resp.status_code == 201, f"Failed to add destination: {resp.text}"
    return resp.json()


def _get_destinations(client: TestClient) -> list[dict[str, Any]]:
    """List all destinations."""
    resp = client.get("/api/destinations")
    assert resp.status_code == 200
    return resp.json()


def _get_destination(client: TestClient, name: str) -> dict[str, Any]:
    """Get a single destination by name."""
    resp = client.get(f"/api/destinations/{name}")
    assert resp.status_code == 200
    return resp.json()


def _update_destination(
    client: TestClient, name: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Update a destination via PUT."""
    resp = client.put(f"/api/destinations/{name}", json=payload)
    assert resp.status_code == 200, f"Failed to update: {resp.text}"
    return resp.json()


def _delete_destination(client: TestClient, name: str) -> None:
    """Delete a destination."""
    resp = client.delete(f"/api/destinations/{name}")
    assert resp.status_code in (200, 204), f"Failed to delete: {resp.text}"


def _start_relay(client: TestClient) -> None:
    """Start the relay."""
    resp = client.post("/api/relay/start")
    assert resp.status_code == 200, f"Failed to start: {resp.text}"


def _stop_relay(client: TestClient) -> None:
    """Stop the relay."""
    resp = client.post("/api/relay/stop")
    assert resp.status_code in (200, 409)


def _get_status(client: TestClient) -> dict[str, Any]:
    """Get relay status."""
    resp = client.get("/api/relay/status")
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Tests: CRUD while relay is stopped
# ---------------------------------------------------------------------------


class TestDestinationCRUD:
    """Tests for destination add/edit/delete while relay is stopped."""

    def test_add_destination(self, api_client: TestClient) -> None:
        """Add a destination and verify it appears in the list."""
        _add_tcp_destination(api_client, "dest-a", TCP_DEST_PORT_A)

        dests = _get_destinations(api_client)
        names = [d["name"] for d in dests]
        assert "dest-a" in names

    def test_add_duplicate_name_rejected(self, api_client: TestClient) -> None:
        """Adding a destination with a duplicate name returns 409."""
        _add_tcp_destination(api_client, "dup-test", TCP_DEST_PORT_A)
        resp = api_client.post(
            "/api/destinations",
            json={
                "name": "dup-test",
                "type": "tcp_server",
                "enabled": True,
                "config": {"port": TCP_DEST_PORT_B, "host": "127.0.0.1"},
            },
        )
        assert resp.status_code == 409

    def test_get_single_destination(self, api_client: TestClient) -> None:
        """Get a single destination by name."""
        _add_tcp_destination(api_client, "single-get", TCP_DEST_PORT_A)
        dest = _get_destination(api_client, "single-get")
        assert dest["name"] == "single-get"
        assert dest["type"] == "tcp_server"

    def test_get_nonexistent_returns_404(self, api_client: TestClient) -> None:
        """Getting a nonexistent destination returns 404."""
        resp = api_client.get("/api/destinations/no-such-dest")
        assert resp.status_code == 404

    def test_update_destination(self, api_client: TestClient) -> None:
        """Update a destination's config."""
        _add_tcp_destination(api_client, "update-me", TCP_DEST_PORT_A)

        _update_destination(
            api_client,
            "update-me",
            {
                "name": "update-me",
                "type": "tcp_server",
                "enabled": False,
                "config": {"port": TCP_DEST_PORT_B, "host": "127.0.0.1"},
            },
        )

        dest = _get_destination(api_client, "update-me")
        assert dest["enabled"] is False

    def test_delete_destination(self, api_client: TestClient) -> None:
        """Delete a destination and verify it's gone."""
        _add_tcp_destination(api_client, "del-me", TCP_DEST_PORT_A)
        _delete_destination(api_client, "del-me")

        resp = api_client.get("/api/destinations/del-me")
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, api_client: TestClient) -> None:
        """Deleting a nonexistent destination returns 404."""
        resp = api_client.delete("/api/destinations/no-such")
        assert resp.status_code == 404

    def test_add_multiple_destinations(self, api_client: TestClient) -> None:
        """Add multiple destinations and list them."""
        _add_tcp_destination(api_client, "multi-a", TCP_DEST_PORT_A)
        _add_tcp_destination(api_client, "multi-b", TCP_DEST_PORT_B)

        dests = _get_destinations(api_client)
        names = [d["name"] for d in dests]
        assert "multi-a" in names
        assert "multi-b" in names


# ---------------------------------------------------------------------------
# Tests: destination management while relay is running
# ---------------------------------------------------------------------------


class TestDestinationWhileRunning:
    """Tests for destination changes while the relay is actively running."""

    def test_add_dest_while_running(
        self,
        api_client: TestClient,
        source_sim: TCPSourceSimulator,
    ) -> None:
        """Add a destination while the relay is running.

        The new destination should appear in the config.
        The relay continues running.
        """
        _configure_input(api_client, source_sim.host, source_sim.port)
        _add_tcp_destination(api_client, "initial", TCP_DEST_PORT_A)
        _start_relay(api_client)
        time.sleep(2.0)

        try:
            # Add a second destination while relay is running
            _add_tcp_destination(api_client, "hot-added", TCP_DEST_PORT_B)

            # Relay should still be running
            status = _get_status(api_client)
            assert status["running"] is True

            # New destination should be in config
            dests = _get_destinations(api_client)
            names = [d["name"] for d in dests]
            assert "hot-added" in names

        finally:
            _stop_relay(api_client)

    def test_remove_dest_while_running(
        self,
        api_client: TestClient,
        source_sim: TCPSourceSimulator,
    ) -> None:
        """Remove a destination while the relay is running.

        The relay should continue running without the removed destination.
        """
        _configure_input(api_client, source_sim.host, source_sim.port)
        _add_tcp_destination(api_client, "keep", TCP_DEST_PORT_A)
        _add_tcp_destination(api_client, "remove-me", TCP_DEST_PORT_B)
        _start_relay(api_client)
        time.sleep(2.0)

        try:
            # Remove while running
            _delete_destination(api_client, "remove-me")

            # Relay should still be running
            status = _get_status(api_client)
            assert status["running"] is True

            # Removed destination should be gone
            dests = _get_destinations(api_client)
            names = [d["name"] for d in dests]
            assert "remove-me" not in names
            assert "keep" in names

        finally:
            _stop_relay(api_client)

    def test_toggle_enabled_while_running(
        self,
        api_client: TestClient,
        source_sim: TCPSourceSimulator,
    ) -> None:
        """Toggle a destination's enabled state while running.

        Config should be updated. Relay continues running.
        """
        _configure_input(api_client, source_sim.host, source_sim.port)
        _add_tcp_destination(api_client, "toggle-me", TCP_DEST_PORT_A)
        _start_relay(api_client)
        time.sleep(2.0)

        try:
            # Disable
            _update_destination(
                api_client,
                "toggle-me",
                {
                    "name": "toggle-me",
                    "type": "tcp_server",
                    "enabled": False,
                    "config": {"port": TCP_DEST_PORT_A, "host": "127.0.0.1"},
                },
            )

            dest = _get_destination(api_client, "toggle-me")
            assert dest["enabled"] is False

            # Re-enable
            _update_destination(
                api_client,
                "toggle-me",
                {
                    "name": "toggle-me",
                    "type": "tcp_server",
                    "enabled": True,
                    "config": {"port": TCP_DEST_PORT_A, "host": "127.0.0.1"},
                },
            )

            dest = _get_destination(api_client, "toggle-me")
            assert dest["enabled"] is True

            # Relay still up
            status = _get_status(api_client)
            assert status["running"] is True

        finally:
            _stop_relay(api_client)
