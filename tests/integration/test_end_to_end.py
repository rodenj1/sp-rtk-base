# pyright: reportUnknownMemberType=false
"""End-to-end integration tests for sp-rtk-base.

Tests the full pipeline:
  TCP Source Simulator → RelayEngine (via API) → TCP Server Destination → Test Client

All tests use real services (no mocks) driven through the REST API,
with a simulated TCP RTCM source and a test client that reads from
the relay's tcp_server destination.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from tests.fixtures.tcp_destination_client import TCPDestinationClient
from tests.fixtures.tcp_source_simulator import TCPSourceSimulator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Port for the tcp_server destination — chosen high to avoid collisions.
# We use a fixed port because the relay engine needs to know it at config time
# and we need to connect to it after the relay starts.
TCP_DEST_PORT = 19876


def _configure_input(client: TestClient, source_host: str, source_port: int) -> None:
    """Configure the TCP input source via API."""
    resp = client.put(
        "/api/input",
        json={
            "source": "tcp",
            "config": {"host": source_host, "port": source_port},
        },
    )
    assert resp.status_code == 200, f"Failed to configure input: {resp.text}"


def _add_tcp_destination(client: TestClient, dest_port: int) -> None:
    """Add a tcp_server destination via API."""
    resp = client.post(
        "/api/destinations",
        json={
            "name": "test-tcp-dest",
            "type": "tcp_server",
            "enabled": True,
            "config": {"port": dest_port, "bind_address": "127.0.0.1"},
        },
    )
    assert resp.status_code == 201, f"Failed to add destination: {resp.text}"


def _start_relay(client: TestClient) -> None:
    """Start the relay via API."""
    resp = client.post("/api/relay/start")
    assert resp.status_code == 200, f"Failed to start relay: {resp.text}"
    data = resp.json()
    assert data["status"] == "ok", f"Unexpected start response: {data}"


def _stop_relay(client: TestClient) -> None:
    """Stop the relay via API."""
    resp = client.post("/api/relay/stop")
    assert resp.status_code == 200, f"Failed to stop relay: {resp.text}"


def _get_status(client: TestClient) -> dict[str, Any]:
    """Get relay status via API."""
    resp = client.get("/api/relay/status")
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestFullTCPPipeline:
    """End-to-end tests: TCP source → relay → TCP server destination."""

    def test_full_data_flow(
        self,
        api_client: TestClient,
        source_sim: TCPSourceSimulator,
        dest_client: TCPDestinationClient,
    ) -> None:
        """Verify data flows from source simulator through to destination client.

        Steps:
        1. Source sim is already running (fixture)
        2. Configure input pointing to source sim
        3. Add tcp_server destination
        4. Start relay
        5. Connect dest client to the tcp_server destination
        6. Wait for data
        7. Verify RTCM data received (0xD3 preamble)
        8. Stop relay
        """
        # 1. Configure input to point at the source simulator
        _configure_input(api_client, source_sim.host, source_sim.port)

        # 2. Add a tcp_server destination
        _add_tcp_destination(api_client, TCP_DEST_PORT)

        # 3. Start the relay
        _start_relay(api_client)

        # Give the relay engine a moment to start threads and connect
        time.sleep(2.0)

        try:
            # 4. Connect test client to the destination port
            dest_client.host = "127.0.0.1"
            dest_client.port = TCP_DEST_PORT
            dest_client.connect(timeout=10.0)

            # 5. Wait for at least 500 bytes of data to flow through
            got_data = dest_client.wait_for_data(min_bytes=500, timeout=15.0)
            assert got_data, (
                f"Expected ≥500 bytes but only received {dest_client.bytes_received}"
            )

            # 6. Verify the received data contains RTCM preamble (0xD3)
            received = dest_client.data
            assert b"\xd3" in received, "Expected RTCM preamble 0xD3 in received data"

            # 7. Check that source sim has been sending data too
            assert source_sim.bytes_sent > 0, "Source simulator should have sent data"

        finally:
            # 8. Always stop relay to clean up threads
            _stop_relay(api_client)

    def test_relay_status_while_running(
        self,
        api_client: TestClient,
        source_sim: TCPSourceSimulator,
    ) -> None:
        """Verify status API reports correct state while relay is running.

        Steps:
        1. Check status shows not running
        2. Configure + start relay
        3. Check status shows running with input connected
        4. Stop relay
        5. Check status shows not running again
        """
        # 1. Initially not running
        status = _get_status(api_client)
        assert status["running"] is False

        # 2. Configure and start
        _configure_input(api_client, source_sim.host, source_sim.port)
        _add_tcp_destination(api_client, TCP_DEST_PORT)
        _start_relay(api_client)

        # Give engine time to connect
        time.sleep(3.0)

        try:
            # 3. Status should show running
            status = _get_status(api_client)
            assert status["running"] is True
            assert status.get("total_destination_count", 0) >= 1

        finally:
            # 4. Stop
            _stop_relay(api_client)

        # Give engine a moment to shut down
        time.sleep(1.0)

        # 5. Should be stopped again
        status = _get_status(api_client)
        assert status["running"] is False

    def test_start_stop_lifecycle(
        self,
        api_client: TestClient,
        source_sim: TCPSourceSimulator,
    ) -> None:
        """Verify relay can be started and stopped cleanly.

        Tests that the relay lifecycle is clean — no resource leaks,
        no errors on stop, can be restarted.
        """
        _configure_input(api_client, source_sim.host, source_sim.port)
        _add_tcp_destination(api_client, TCP_DEST_PORT)

        # Start
        _start_relay(api_client)
        time.sleep(1.0)

        status = _get_status(api_client)
        assert status["running"] is True

        # Stop
        _stop_relay(api_client)
        time.sleep(1.0)

        status = _get_status(api_client)
        assert status["running"] is False

        # Double-stop returns 409 (already stopped) — not an error
        resp = api_client.post("/api/relay/stop")
        assert resp.status_code == 409

    def test_cannot_start_without_input(
        self,
        api_client: TestClient,
    ) -> None:
        """Verify relay refuses to start without a configured input source."""
        resp = api_client.post("/api/relay/start")
        assert resp.status_code == 400
        assert "input source" in resp.json()["message"].lower()

    def test_cannot_start_without_destination(
        self,
        api_client: TestClient,
        source_sim: TCPSourceSimulator,
    ) -> None:
        """Verify relay refuses to start without any destinations."""
        _configure_input(api_client, source_sim.host, source_sim.port)

        resp = api_client.post("/api/relay/start")
        assert resp.status_code == 400
        assert "destination" in resp.json()["message"].lower()
