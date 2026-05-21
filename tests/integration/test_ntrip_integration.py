# pyright: reportUnknownMemberType=false
"""NTRIP destination integration tests for sp-rtk-base.

Tests the full pipeline:
  TCP Source Simulator → RelayEngine (via API) → NTRIP Destination → Mock Caster

Uses the MockNtripCaster fixture to verify that RTCM data flows
from the relay engine through an NTRIP destination to a caster.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from tests.fixtures.mock_ntrip_caster import MockNtripCaster
from tests.fixtures.tcp_source_simulator import TCPSourceSimulator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _add_ntrip_destination(
    client: TestClient,
    *,
    name: str,
    caster_host: str,
    caster_port: int,
    mountpoint: str = "TEST_MOUNT",
    password: str = "test_password",
    version: str = "1.0",
) -> None:
    """Add an NTRIP destination via API."""
    resp = client.post(
        "/api/destinations",
        json={
            "name": name,
            "type": "ntrip",
            "enabled": True,
            "config": {
                "caster": caster_host,
                "port": caster_port,
                "mountpoint": mountpoint,
                "password": password,
                "version": version,
                "connection_timeout": 5,
                "retry_initial_delay": 2,
                "retry_max_delay": 10,
            },
        },
    )
    assert resp.status_code == 201, f"Failed to add NTRIP destination: {resp.text}"


def _start_relay(client: TestClient) -> None:
    """Start the relay via API."""
    resp = client.post("/api/relay/start")
    assert resp.status_code == 200, f"Failed to start relay: {resp.text}"


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


class TestNtripDestinationPipeline:
    """Integration tests: TCP source → relay → NTRIP destination → mock caster."""

    def test_ntrip_v1_data_flow(
        self,
        api_client: TestClient,
        source_sim: TCPSourceSimulator,
    ) -> None:
        """Verify RTCM data flows from source through NTRIP v1.0 to mock caster.

        Steps:
        1. Start mock NTRIP caster on ephemeral port
        2. Configure TCP input pointing to source simulator
        3. Add NTRIP destination pointing to mock caster
        4. Start relay
        5. Wait for caster to receive data
        6. Assert RTCM data received
        7. Stop relay
        """
        with MockNtripCaster(port=0, password="test_password") as caster:
            # Configure input + NTRIP destination
            _configure_input(api_client, source_sim.host, source_sim.port)
            _add_ntrip_destination(
                api_client,
                name="test-ntrip-v1",
                caster_host="127.0.0.1",
                caster_port=caster.port,
                version="1.0",
            )

            # Start relay
            _start_relay(api_client)

            try:
                # Wait for the caster to accept a connection
                connected = caster.wait_for_connection(timeout=10.0)
                assert connected, "Mock caster did not receive a connection"

                # Wait for data to flow
                deadline = time.time() + 15.0
                while time.time() < deadline and caster.received_bytes < 100:
                    time.sleep(0.5)

                # Assertions
                assert caster.received_bytes > 0, (
                    "Expected RTCM data from NTRIP destination"
                )
                assert caster.detected_version == "1.0", (
                    f"Expected NTRIP v1.0, got: {caster.detected_version}"
                )
                assert caster.connection_count >= 1
                assert b"\xd3" in caster.get_received_data(), (
                    "Expected RTCM preamble 0xD3 in received data"
                )

            finally:
                _stop_relay(api_client)

    def test_ntrip_v2_data_flow(
        self,
        api_client: TestClient,
        source_sim: TCPSourceSimulator,
    ) -> None:
        """Verify RTCM data flows via NTRIP v2.0 (HTTP POST/chunked)."""
        with MockNtripCaster(port=0, password="test_password") as caster:
            _configure_input(api_client, source_sim.host, source_sim.port)
            _add_ntrip_destination(
                api_client,
                name="test-ntrip-v2",
                caster_host="127.0.0.1",
                caster_port=caster.port,
                version="2.0",
            )

            _start_relay(api_client)

            try:
                connected = caster.wait_for_connection(timeout=10.0)
                assert connected, "Mock caster did not receive a connection"

                deadline = time.time() + 15.0
                while time.time() < deadline and caster.received_bytes < 100:
                    time.sleep(0.5)

                assert caster.received_bytes > 0
                assert caster.detected_version == "2.0"

            finally:
                _stop_relay(api_client)

    def test_ntrip_auth_failure(
        self,
        api_client: TestClient,
        source_sim: TCPSourceSimulator,
    ) -> None:
        """Verify NTRIP destination handles authentication rejection gracefully.

        The mock caster rejects auth → destination should report errors
        but the relay should not crash.
        """
        with MockNtripCaster(
            port=0, password="test_password", accept_auth=False
        ) as caster:
            _configure_input(api_client, source_sim.host, source_sim.port)
            _add_ntrip_destination(
                api_client,
                name="test-ntrip-reject",
                caster_host="127.0.0.1",
                caster_port=caster.port,
                version="1.0",
            )

            _start_relay(api_client)

            try:
                # Give the destination time to attempt connection and get rejected
                time.sleep(5.0)

                # Caster should have seen at least one connection attempt
                assert caster.connection_count >= 1, (
                    "Expected at least one connection attempt"
                )

                # Relay should still be running (not crashed)
                status = _get_status(api_client)
                assert status["running"] is True

                # No data should have been received (auth was rejected)
                assert caster.received_bytes == 0

            finally:
                _stop_relay(api_client)

    def test_ntrip_with_tcp_server_dual_destination(
        self,
        api_client: TestClient,
        source_sim: TCPSourceSimulator,
    ) -> None:
        """Verify NTRIP works alongside a TCP server destination (fan-out).

        Both destinations should receive data independently.
        """
        with MockNtripCaster(port=0, password="test_password") as caster:
            _configure_input(api_client, source_sim.host, source_sim.port)

            # Add both NTRIP and TCP server destinations
            _add_ntrip_destination(
                api_client,
                name="test-ntrip-dual",
                caster_host="127.0.0.1",
                caster_port=caster.port,
            )

            # Also add a TCP server destination
            resp = api_client.post(
                "/api/destinations",
                json={
                    "name": "test-tcp-dual",
                    "type": "tcp_server",
                    "enabled": True,
                    "config": {"port": 19877, "bind_address": "127.0.0.1"},
                },
            )
            assert resp.status_code == 201

            _start_relay(api_client)

            try:
                connected = caster.wait_for_connection(timeout=10.0)
                assert connected, "Mock caster did not receive a connection"

                deadline = time.time() + 15.0
                while time.time() < deadline and caster.received_bytes < 100:
                    time.sleep(0.5)

                # NTRIP destination should have received data
                assert caster.received_bytes > 0

                # Check relay status shows both destinations
                status = _get_status(api_client)
                assert status["total_destination_count"] >= 2

            finally:
                _stop_relay(api_client)
