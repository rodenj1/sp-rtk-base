"""Tests for the WiFi-picker captive portal (issue #10).

``render_index``/``render_success`` are pure HTML builders, tested
directly on their output. The request handler is exercised over a real
socket bound to an OS-assigned ephemeral port (127.0.0.1:0) — there's
no established pattern in this repo for testing a stdlib
``BaseHTTPRequestHandler`` in isolation, and driving it with real HTTP
requests is simpler and more faithful than hand-rolling a fake
transport. The nmcli boundary is mocked via a hand-rolled
``FakeAdapter``, per the issue's own acceptance criterion and matching
the fake-over-``MagicMock`` convention in ``test_net_provision_nmcli_adapter``.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request

import pytest

from sp_rtk_base.models.net_provision_models import WifiNetwork
from sp_rtk_base.services.net_provision.nmcli_adapter import WifiConnectError
from sp_rtk_base.services.net_provision.portal import (
    Portal,
    _PortalHTTPServer,
    render_index,
    render_success,
)

_GATEWAY_IP = "10.42.0.1"


class FakeAdapter:
    """Stands in for NmcliAdapter's portal-facing surface."""

    def __init__(self, networks: list[WifiNetwork] | None = None) -> None:
        self._networks = networks or []
        self.connect_calls: list[tuple[str, str]] = []
        self.fail_with: WifiConnectError | None = None

    def latest_scan(self) -> list[WifiNetwork]:
        return list(self._networks)

    def connect_to_network(self, ssid: str, password: str) -> None:
        self.connect_calls.append((ssid, password))
        if self.fail_with is not None:
            raise self.fail_with


# ---------------------------------------------------------------------------
# Pure HTML builders
# ---------------------------------------------------------------------------


class TestRenderIndex:
    def test_lists_each_scanned_network(self) -> None:
        networks = [
            WifiNetwork(ssid="SiteWiFi", signal=80, security="WPA2"),
            WifiNetwork(ssid="Guest", signal=40, security=""),
        ]
        body = render_index(networks, ap_gateway_ip=_GATEWAY_IP).decode()
        assert "SiteWiFi" in body
        assert "Guest" in body

    def test_escapes_ssid_html_special_characters(self) -> None:
        """A malicious/odd nearby SSID must not inject markup into the page."""
        networks = [WifiNetwork(ssid="<script>evil()</script>", signal=50, security="")]
        body = render_index(networks, ap_gateway_ip=_GATEWAY_IP).decode()
        assert "<script>evil()</script>" not in body
        assert "&lt;script&gt;" in body

    def test_renders_an_error_banner_when_given(self) -> None:
        body = render_index(
            [], ap_gateway_ip=_GATEWAY_IP, error="Wrong password"
        ).decode()
        assert "Wrong password" in body

    def test_no_error_banner_when_none_given(self) -> None:
        body = render_index([], ap_gateway_ip=_GATEWAY_IP).decode()
        assert "error" not in body.lower()

    def test_documents_the_manual_url_fallback(self) -> None:
        """Acceptance criterion: a documented fallback for phones whose
        captive-portal prompt doesn't auto-pop."""
        body = render_index([], ap_gateway_ip=_GATEWAY_IP).decode()
        assert _GATEWAY_IP in body

    def test_empty_scan_still_renders_a_usable_page(self) -> None:
        body = render_index([], ap_gateway_ip=_GATEWAY_IP).decode()
        assert "<form" in body


class TestRenderSuccess:
    def test_mentions_the_connected_network(self) -> None:
        body = render_success("SiteWiFi").decode()
        assert "SiteWiFi" in body

    def test_escapes_the_ssid(self) -> None:
        body = render_success("<b>x</b>").decode()
        assert "<b>x</b>" not in body


# ---------------------------------------------------------------------------
# Request handler, over a real ephemeral-port socket
# ---------------------------------------------------------------------------


@pytest.fixture
def running_server():  # type: ignore[no-untyped-def]
    adapter = FakeAdapter([WifiNetwork(ssid="SiteWiFi", signal=80, security="WPA2")])
    server = _PortalHTTPServer(
        bind_host="127.0.0.1", port=0, adapter=adapter, ap_gateway_ip=_GATEWAY_IP
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, adapter
    finally:
        server.shutdown()
        server.server_close()


def _url(server: _PortalHTTPServer, path: str) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}{path}"


class TestGet:
    def test_renders_the_network_list_for_any_path(self, running_server) -> None:  # type: ignore[no-untyped-def]
        """OS captive-portal probes hit arbitrary hostnames/paths (wildcard
        DNS sends them all here) — every one must get *some* page back
        that differs from what the probe expected, to trigger the prompt."""
        server, _ = running_server
        with urllib.request.urlopen(_url(server, "/generate_204"), timeout=2) as resp:
            body = resp.read().decode()
        assert resp.status == 200
        assert "SiteWiFi" in body


class TestPostConnect:
    def test_successful_connect_calls_the_adapter_and_renders_success(
        self,
        running_server,  # type: ignore[no-untyped-def]
    ) -> None:
        server, adapter = running_server
        data = b"ssid=SiteWiFi&password=hunter22"
        request = urllib.request.Request(
            _url(server, "/connect"), data=data, method="POST"
        )
        with urllib.request.urlopen(request, timeout=2) as resp:
            body = resp.read().decode()
        assert resp.status == 200
        assert adapter.connect_calls == [("SiteWiFi", "hunter22")]
        assert "SiteWiFi" in body

    def test_wrong_password_shows_error_with_retry_form(
        self,
        running_server,  # type: ignore[no-untyped-def]
    ) -> None:
        server, adapter = running_server
        adapter.fail_with = WifiConnectError("SiteWiFi", "Secrets were required")
        data = b"ssid=SiteWiFi&password=wrong"
        request = urllib.request.Request(
            _url(server, "/connect"), data=data, method="POST"
        )
        with urllib.request.urlopen(request, timeout=2) as resp:
            body = resp.read().decode()
        assert resp.status == 200
        assert "<form" in body  # retry path: the form is still there
        assert "SiteWiFi" in body

    def test_missing_ssid_re_renders_the_form_without_calling_the_adapter(
        self,
        running_server,  # type: ignore[no-untyped-def]
    ) -> None:
        server, adapter = running_server
        data = b"password=hunter22"
        request = urllib.request.Request(
            _url(server, "/connect"), data=data, method="POST"
        )
        with urllib.request.urlopen(request, timeout=2):
            pass
        assert adapter.connect_calls == []

    def test_unknown_post_path_is_not_found(self, running_server) -> None:  # type: ignore[no-untyped-def]
        server, _ = running_server
        request = urllib.request.Request(
            _url(server, "/other"), data=b"", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(request, timeout=2)
        assert exc_info.value.code == 404


# ---------------------------------------------------------------------------
# Portal lifecycle — idempotent start()/stop() (issue #10)
# ---------------------------------------------------------------------------


class TestPortalLifecycle:
    def _portal(self) -> Portal:
        from sp_rtk_base.models.net_provision_models import NetProvisionConfig

        config = NetProvisionConfig(
            ap_password="sticker-secret",
            ap_gateway_ip=_GATEWAY_IP,
            portal_http_port=0,
        )
        return Portal(adapter=FakeAdapter(), config=config)

    def test_not_running_before_start(self) -> None:
        assert self._portal().is_running is False

    def test_running_after_start(self) -> None:
        portal = self._portal()
        try:
            portal.start()
            assert portal.is_running is True
        finally:
            portal.stop()

    def test_start_is_idempotent(self) -> None:
        portal = self._portal()
        try:
            portal.start()
            portal.start()  # must not raise (e.g. "address already in use")
            assert portal.is_running is True
        finally:
            portal.stop()

    def test_stop_is_idempotent(self) -> None:
        portal = self._portal()
        portal.start()
        portal.stop()
        portal.stop()  # must not raise
        assert portal.is_running is False

    def test_stop_before_start_is_a_no_op(self) -> None:
        portal = self._portal()
        portal.stop()
        assert portal.is_running is False

    def test_restarts_cleanly_after_stop(self) -> None:
        portal = self._portal()
        try:
            portal.start()
            portal.stop()
            portal.start()
            assert portal.is_running is True
        finally:
            portal.stop()
