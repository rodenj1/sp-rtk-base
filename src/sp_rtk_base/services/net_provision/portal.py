"""The WiFi-picker captive portal (issue #10).

A minimal HTTP server that runs only while the setup AP is active: it
renders the nmcli adapter's cached scan (:meth:`NmcliAdapter.latest_scan`)
as a network-picker form, hands a submitted SSID+password to
:meth:`NmcliAdapter.connect_to_network`, and surfaces a wrong-password
failure with a retry path. Deliberately built on stdlib ``http.server``
rather than FastAPI: the rest of ``services/net_provision`` is
sync/threading by design (issue #9), and this is meant to be, per the
issue, "a minimal HTTP server" — not a second ASGI stack inside an
otherwise blocking systemd service.

Every GET, regardless of path, renders the same picker page. That is
what makes wildcard DNS (:mod:`~.dns_responder`) work: an OS
captive-portal probe requests some fixed hostname/path (e.g.
``/generate_204``, ``/hotspot-detect.html``) expecting a specific,
narrow response; getting this page back instead is the mismatch that
triggers the "Sign in to network" prompt. It's also the manual-URL
fallback the issue asks for — visiting ``http://<ap_gateway_ip>/``
by hand hits the same handler.
"""

from __future__ import annotations

import html
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol, cast
from urllib.parse import parse_qs

from sp_rtk_base.models.net_provision_models import NetProvisionConfig, WifiNetwork
from sp_rtk_base.services.net_provision.dns_responder import WildcardDnsServer
from sp_rtk_base.services.net_provision.nmcli_adapter import (
    NmcliAdapter,
    WifiConnectError,
)

logger = logging.getLogger(__name__)

_CONNECT_PATH = "/connect"


class PortalAdapter(Protocol):
    """The slice of :class:`NmcliAdapter` the portal depends on."""

    def latest_scan(self) -> list[WifiNetwork]: ...

    def connect_to_network(self, ssid: str, password: str) -> None: ...


# ---------------------------------------------------------------------------
# Pure HTML builders
# ---------------------------------------------------------------------------


def _page(title: str, body_html: str) -> bytes:
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
</head>
<body>
{body_html}
</body>
</html>
""".encode()


def render_index(
    networks: list[WifiNetwork], *, ap_gateway_ip: str, error: str | None = None
) -> bytes:
    """Render the network-picker page.

    Args:
        networks: The adapter's cached scan
            (:meth:`NmcliAdapter.latest_scan`).
        ap_gateway_ip: The setup AP's own address — printed as the
            manual-URL fallback for a phone whose captive-portal prompt
            didn't auto-pop.
        error: A connect-failure message from a previous submission, if
            any, rendered as a retry banner.
    """
    error_html = (
        f'<p class="error">{html.escape(error)}</p>' if error is not None else ""
    )
    options = "".join(
        '<option value="{ssid}">{ssid} ({signal}%, {security})</option>'.format(
            ssid=html.escape(network.ssid),
            signal=network.signal,
            security=html.escape(network.security) or "open",
        )
        for network in networks
    )
    body = f"""
<h1>Choose a WiFi network</h1>
{error_html}
<form method="post" action="{_CONNECT_PATH}">
  <select name="ssid" required>
    <option value="">Select a network&hellip;</option>
    {options}
  </select>
  <input type="password" name="password" placeholder="Password" autocomplete="off">
  <button type="submit">Connect</button>
</form>
<p>If this page didn't open automatically, visit
<code>http://{html.escape(ap_gateway_ip)}/</code> from your phone's browser.</p>
"""
    return _page("Network Setup", body)


def render_success(ssid: str) -> bytes:
    """Render the post-connect confirmation page."""
    body = f"""
<h1>Connected</h1>
<p>Connected to {html.escape(ssid)}. You can close this page.</p>
"""
    return _page("Network Setup", body)


# ---------------------------------------------------------------------------
# Request handler + HTTP server
# ---------------------------------------------------------------------------


class _PortalRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        logger.info("portal %s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        self._respond_index()

    def do_POST(self) -> None:
        if self.path != _CONNECT_PATH:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ssid, password = self._read_connect_form()
        if not ssid:
            self._respond_index(error="Choose a network.")
            return
        server = self._server()
        try:
            server.adapter.connect_to_network(ssid, password)
        except WifiConnectError:
            self._respond_index(
                error=f"Could not connect to {ssid!r} — check the password and try again."
            )
            return
        self._respond_html(HTTPStatus.OK, render_success(ssid))

    def _read_connect_form(self) -> tuple[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw_body = self.rfile.read(length) if length > 0 else b""
        fields = parse_qs(raw_body.decode("utf-8", errors="replace"))
        ssid = fields.get("ssid", [""])[0].strip()
        password = fields.get("password", [""])[0]
        return ssid, password

    def _respond_index(self, *, error: str | None = None) -> None:
        server = self._server()
        body = render_index(
            server.adapter.latest_scan(),
            ap_gateway_ip=server.ap_gateway_ip,
            error=error,
        )
        self._respond_html(HTTPStatus.OK, body)

    def _respond_html(self, status: HTTPStatus, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _server(self) -> _PortalHTTPServer:
        return cast("_PortalHTTPServer", self.server)


class _PortalHTTPServer(ThreadingHTTPServer):
    """Holds the collaborators the handler needs per request."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self, *, bind_host: str, port: int, adapter: PortalAdapter, ap_gateway_ip: str
    ) -> None:
        self.adapter = adapter
        self.ap_gateway_ip = ap_gateway_ip
        super().__init__((bind_host, port), _PortalRequestHandler)


# ---------------------------------------------------------------------------
# Lifecycle: HTTP + wildcard DNS as one unit
# ---------------------------------------------------------------------------


class Portal:
    """Owns the AP-mode HTTP portal and wildcard DNS responder together.

    ``start()``/``stop()`` are idempotent so the supervisor (issue #9)
    can call them on every tick as ``NetworkState.ap_active`` flips,
    without tracking whether the portal is already running.
    """

    def __init__(self, *, adapter: NmcliAdapter, config: NetProvisionConfig) -> None:
        self._adapter = adapter
        self._config = config
        self._http: _PortalHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._dns: WildcardDnsServer | None = None
        self._dns_thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._http is not None

    def start(self) -> None:
        if self.is_running:
            return
        self._http = _PortalHTTPServer(
            bind_host="0.0.0.0",
            port=self._config.portal_http_port,
            adapter=self._adapter,
            ap_gateway_ip=self._config.ap_gateway_ip,
        )
        self._http_thread = threading.Thread(
            target=self._http.serve_forever,
            daemon=True,
            name="net-provision-portal-http",
        )
        self._http_thread.start()

        self._dns = WildcardDnsServer(
            bind_host="0.0.0.0",
            port=self._config.portal_dns_port,
            answer_ip=self._config.ap_gateway_ip,
        )
        self._dns_thread = threading.Thread(
            target=self._dns.serve_forever, daemon=True, name="net-provision-portal-dns"
        )
        self._dns_thread.start()
        logger.info(
            "Portal started (http :%d, dns :%d)",
            self._config.portal_http_port,
            self._config.portal_dns_port,
        )

    def stop(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
        if self._dns is not None:
            self._dns.shutdown()
            self._dns.server_close()
            self._dns = None
        logger.info("Portal stopped")
