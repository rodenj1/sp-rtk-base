#!/usr/bin/env python3
"""
Simple NTRIP Caster for development and testing.

Supports both NTRIP v1.0 and v2.0 protocols:
  - v1.0: SOURCE/ICY protocol for servers, GET with ICY responses for clients
  - v2.0: HTTP/1.1 POST for servers, HTTP/1.1 GET for clients

Usage:
  python ntrip_caster.py [--port 2101] [--password testpass] [--debug]

Environment variables:
  CASTER_PORT      - Port to listen on (default: 2101)
  SERVER_PASSWORD  - Password for NTRIP servers (default: testpass)
  LOG_LEVEL        - debug|info|warn (default: info)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_config = {
    "port": int(os.environ.get("CASTER_PORT", "2101")),
    "password": os.environ.get("SERVER_PASSWORD", "testpass"),
    "log_level": os.environ.get("LOG_LEVEL", "info").upper(),
}

logger = logging.getLogger("ntrip-caster")


# ---------------------------------------------------------------------------
# Mountpoint registry
# ---------------------------------------------------------------------------
@dataclass
class Mountpoint:
    """Tracks a single NTRIP mountpoint (data stream)."""

    name: str
    server_writer: asyncio.StreamWriter | None = None
    clients: list[asyncio.StreamWriter] = field(default_factory=list)  # type: ignore[type-arg]
    bytes_received: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class MountpointRegistry:
    """Thread-safe registry of active mountpoints."""

    def __init__(self) -> None:
        self._mounts: dict[str, Mountpoint] = {}

    def register_server(
        self, name: str, writer: asyncio.StreamWriter
    ) -> Mountpoint | None:
        """Register a server for a mountpoint. Returns the mountpoint or None if taken."""
        if name in self._mounts and self._mounts[name].server_writer is not None:
            return None  # Already has an active server
        if name not in self._mounts:
            self._mounts[name] = Mountpoint(name=name)
        self._mounts[name].server_writer = writer
        logger.info("📡 Mountpoint created: /%s", name)
        return self._mounts[name]

    def unregister_server(self, name: str) -> None:
        """Remove the server from a mountpoint."""
        if name in self._mounts:
            self._mounts[name].server_writer = None
            # Disconnect all clients
            for client in self._mounts[name].clients:
                try:
                    client.close()
                except Exception:
                    pass
            self._mounts[name].clients.clear()
            del self._mounts[name]
            logger.info("📡 Mountpoint removed: /%s", name)

    def add_client(self, name: str, writer: asyncio.StreamWriter) -> bool:
        """Add a client to an existing mountpoint. Returns True if successful."""
        if name not in self._mounts or self._mounts[name].server_writer is None:
            return False
        self._mounts[name].clients.append(writer)
        logger.info(
            "👤 Client connected to /%s (total: %d)",
            name,
            len(self._mounts[name].clients),
        )
        return True

    def remove_client(self, name: str, writer: asyncio.StreamWriter) -> None:
        """Remove a client from a mountpoint."""
        if name in self._mounts:
            try:
                self._mounts[name].clients.remove(writer)
            except ValueError:
                pass
            logger.info(
                "👤 Client disconnected from /%s (remaining: %d)",
                name,
                len(self._mounts[name].clients),
            )

    def get_mount(self, name: str) -> Mountpoint | None:
        """Get a mountpoint by name."""
        return self._mounts.get(name)

    def broadcast(self, name: str, data: bytes) -> None:
        """Send data to all clients on a mountpoint."""
        mount = self._mounts.get(name)
        if not mount:
            return
        mount.bytes_received += len(data)
        dead_clients: list[asyncio.StreamWriter] = []
        for client in mount.clients:
            try:
                client.write(data)
            except Exception:
                dead_clients.append(client)
        for dead in dead_clients:
            try:
                dead.close()
            except Exception:
                pass
            try:
                mount.clients.remove(dead)
            except ValueError:
                pass

    def get_sourcetable(self) -> str:
        """Generate NTRIP sourcetable."""
        lines: list[str] = []
        for name, mount in sorted(self._mounts.items()):
            if mount.server_writer is not None:
                # STR;mountpoint;city;country;format;format-details;carrier;
                # nav-system;network;country-code;lat;lon;nmea;solution;generator;
                # compression;auth;fee;bitrate;misc
                lines.append(
                    f"STR;{name};;;;;;GPS+GLO;none;;;0.00;0.00;0;0;sp-rtk-base-caster;none;N;N;0;"
                )
        lines.append("ENDSOURCETABLE")
        return "\r\n".join(lines) + "\r\n"

    @property
    def active_mountpoints(self) -> list[str]:
        return [n for n, m in self._mounts.items() if m.server_writer is not None]


# Global registry
registry = MountpointRegistry()


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------
@dataclass
class NtripRequest:
    """Parsed NTRIP request."""

    method: str  # SOURCE, GET, POST
    mountpoint: str  # e.g. "TEST" (without leading /)
    http_version: str  # "1.0" or "1.1"
    headers: dict[str, str] = field(default_factory=dict)  # type: ignore[type-arg]
    source_password: str = ""  # For SOURCE requests
    ntrip_version: str = "1.0"  # "1.0" or "2.0"
    is_sourcetable: bool = False


async def read_request(reader: asyncio.StreamReader) -> NtripRequest | None:
    """Read and parse an NTRIP request from the stream."""
    try:
        # Read the request line
        line = await asyncio.wait_for(reader.readline(), timeout=10.0)
        if not line:
            return None
        request_line = line.decode("latin-1").strip()
        logger.debug("Request line: %s", request_line)

        req = NtripRequest(method="", mountpoint="", http_version="1.0")

        parts = request_line.split()
        if len(parts) < 2:
            return None

        # Parse based on first word
        if parts[0] == "SOURCE":
            # NTRIP v1.0 server: SOURCE <password> /<mountpoint>
            req.method = "SOURCE"
            req.ntrip_version = "1.0"
            if len(parts) >= 3:
                req.source_password = parts[1]
                req.mountpoint = parts[2].lstrip("/")
            elif len(parts) == 2:
                req.source_password = ""
                req.mountpoint = parts[1].lstrip("/")
        elif parts[0] == "GET":
            req.method = "GET"
            path = parts[1] if len(parts) >= 2 else "/"
            req.mountpoint = path.lstrip("/")
            req.is_sourcetable = path == "/" or path == ""
            if len(parts) >= 3 and "1.1" in parts[2]:
                req.http_version = "1.1"
                req.ntrip_version = "2.0"
            else:
                req.http_version = "1.0"
                req.ntrip_version = "1.0"
        elif parts[0] == "POST":
            req.method = "POST"
            req.http_version = "1.1"
            req.ntrip_version = "2.0"
            path = parts[1] if len(parts) >= 2 else "/"
            req.mountpoint = path.lstrip("/")
        else:
            logger.warning("Unknown method: %s", parts[0])
            return None

        # Read headers
        while True:
            hline = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not hline or hline == b"\r\n" or hline == b"\n":
                break
            decoded = hline.decode("latin-1").strip()
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                req.headers[key.strip().lower()] = value.strip()

        # Detect NTRIP version from headers
        ntrip_ver = req.headers.get("ntrip-version", "")
        if "2" in ntrip_ver:
            req.ntrip_version = "2.0"

        # Extract credentials from Authorization header
        auth = req.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded_auth = base64.b64decode(auth[6:]).decode("utf-8")
                if ":" in decoded_auth:
                    _, password = decoded_auth.split(":", 1)
                    if req.method == "POST":
                        req.source_password = password
            except Exception:
                pass

        logger.debug(
            "Parsed: method=%s mountpoint=%s version=%s sourcetable=%s",
            req.method,
            req.mountpoint,
            req.ntrip_version,
            req.is_sourcetable,
        )
        return req
    except (asyncio.TimeoutError, ConnectionError, UnicodeDecodeError) as e:
        logger.debug("Request read error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------
async def handle_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Handle an incoming NTRIP connection."""
    peer = writer.get_extra_info("peername")
    logger.debug("New connection from %s", peer)

    try:
        req = await read_request(reader)
        if req is None:
            writer.close()
            await writer.wait_closed()
            return

        if req.method == "GET" and req.is_sourcetable:
            await handle_sourcetable(req, writer)
        elif req.method == "SOURCE":
            await handle_v1_server(req, reader, writer)
        elif req.method == "POST":
            await handle_v2_server(req, reader, writer)
        elif req.method == "GET" and not req.is_sourcetable:
            await handle_client(req, reader, writer)
        else:
            logger.warning("Unhandled request: %s", req.method)
            writer.close()
            await writer.wait_closed()
    except (ConnectionError, BrokenPipeError):
        logger.debug("Connection lost from %s", peer)
    except Exception as e:
        logger.error("Error handling connection from %s: %s", peer, e)
    finally:
        try:
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
        except Exception:
            pass


async def handle_sourcetable(req: NtripRequest, writer: asyncio.StreamWriter) -> None:
    """Serve the NTRIP sourcetable."""
    sourcetable = registry.get_sourcetable()
    content = sourcetable.encode("utf-8")

    if req.ntrip_version == "2.0":
        # NTRIP v2.0: standard HTTP response
        response = (
            f"HTTP/1.1 200 OK\r\n"
            f"Server: SP-Base-NTRIP-Caster/1.0\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(content)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
    else:
        # NTRIP v1.0: SOURCETABLE response
        response = (
            f"SOURCETABLE 200 OK\r\n"
            f"Server: SP-Base-NTRIP-Caster/1.0\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(content)}\r\n"
            f"\r\n"
        )

    logger.info(
        "📋 Sourcetable requested (v%s) — %d active mountpoints",
        req.ntrip_version,
        len(registry.active_mountpoints),
    )
    writer.write(response.encode("utf-8"))
    writer.write(content)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def handle_v1_server(
    req: NtripRequest,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Handle NTRIP v1.0 SOURCE connection (server pushing data)."""
    logger.info("🔌 v1.0 SOURCE request: mountpoint=/%s", req.mountpoint)

    # Authenticate
    if req.source_password != _config["password"]:
        logger.warning(
            "✗ v1.0 SOURCE auth FAILED for /%s (wrong password)",
            req.mountpoint,
        )
        writer.write(b"ERROR - Bad Password\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    # Register mountpoint
    mount = registry.register_server(req.mountpoint, writer)
    if mount is None:
        logger.warning("✗ Mountpoint /%s already in use", req.mountpoint)
        writer.write(b"ERROR - Mountpoint Taken\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    # Send OK response
    writer.write(b"ICY 200 OK\r\n\r\n")
    await writer.drain()
    logger.info("✓ v1.0 SOURCE authenticated: /%s", req.mountpoint)

    # Read data from server and broadcast to clients
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            logger.debug("v1.0 data: /%s — %d bytes", req.mountpoint, len(data))
            registry.broadcast(req.mountpoint, data)
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        registry.unregister_server(req.mountpoint)
        logger.info("🔌 v1.0 SOURCE disconnected: /%s", req.mountpoint)


async def handle_v2_server(
    req: NtripRequest,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Handle NTRIP v2.0 POST connection (server pushing data)."""
    logger.info("🔌 v2.0 POST request: mountpoint=/%s", req.mountpoint)

    # Authenticate — use password already extracted during request parsing
    password = req.source_password
    logger.debug(
        "v2.0 auth check: source_password=%r headers=%r expected=%r",
        req.source_password,
        dict(req.headers),
        _config["password"],
    )

    if password != _config["password"]:
        logger.warning("✗ v2.0 POST auth FAILED for /%s", req.mountpoint)
        response = (
            "HTTP/1.1 401 Unauthorized\r\n"
            "Server: SP-Base-NTRIP-Caster/1.0\r\n"
            'WWW-Authenticate: Basic realm="NTRIP Caster"\r\n'
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(response.encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    # Register mountpoint
    mount = registry.register_server(req.mountpoint, writer)
    if mount is None:
        logger.warning("✗ Mountpoint /%s already in use", req.mountpoint)
        response = (
            "HTTP/1.1 409 Conflict\r\n"
            "Server: SP-Base-NTRIP-Caster/1.0\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(response.encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    # Send OK
    response = (
        "HTTP/1.1 200 OK\r\n"
        "Server: SP-Base-NTRIP-Caster/1.0\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    writer.write(response.encode("utf-8"))
    await writer.drain()
    logger.info("✓ v2.0 POST authenticated: /%s", req.mountpoint)

    # Check if chunked transfer encoding
    is_chunked = "chunked" in req.headers.get("transfer-encoding", "").lower()

    try:
        if is_chunked:
            await _read_chunked_data(reader, req.mountpoint)
        else:
            # Read raw data
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                logger.debug(
                    "v2.0 data: /%s — %d bytes",
                    req.mountpoint,
                    len(data),
                )
                registry.broadcast(req.mountpoint, data)
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        registry.unregister_server(req.mountpoint)
        logger.info("🔌 v2.0 POST disconnected: /%s", req.mountpoint)


async def _read_chunked_data(reader: asyncio.StreamReader, mountpoint: str) -> None:
    """Read HTTP chunked transfer encoding data."""
    while True:
        # Read chunk size line
        size_line = await reader.readline()
        if not size_line:
            break
        try:
            chunk_size = int(size_line.strip(), 16)
        except ValueError:
            # Not a valid chunk header, treat as raw data
            registry.broadcast(mountpoint, size_line)
            continue

        if chunk_size == 0:
            break

        # Read chunk data
        data = await reader.readexactly(chunk_size)
        registry.broadcast(mountpoint, data)

        # Read trailing \r\n
        await reader.readline()


async def handle_client(
    req: NtripRequest,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Handle NTRIP client connection (requesting data stream)."""
    logger.info(
        "👤 v%s GET request: mountpoint=/%s",
        req.ntrip_version,
        req.mountpoint,
    )

    # Check if mountpoint exists
    mount = registry.get_mount(req.mountpoint)
    if mount is None or mount.server_writer is None:
        if req.ntrip_version == "2.0":
            response = (
                "HTTP/1.1 404 Not Found\r\n"
                "Server: SP-Base-NTRIP-Caster/1.0\r\n"
                "Connection: close\r\n"
                "\r\n"
            )
        else:
            response = "ICY 404 Not Found\r\n\r\n"
        writer.write(response.encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    # Send OK and start streaming
    if req.ntrip_version == "2.0":
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Server: SP-Base-NTRIP-Caster/1.0\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
    else:
        response = "ICY 200 OK\r\n\r\n"

    writer.write(response.encode("utf-8"))
    await writer.drain()

    # Register as client
    if not registry.add_client(req.mountpoint, writer):
        writer.close()
        await writer.wait_closed()
        return

    # Keep connection alive until client disconnects
    try:
        while True:
            # Check if client is still connected by reading (client shouldn't send data)
            data = await asyncio.wait_for(reader.read(1024), timeout=60.0)
            if not data:
                break
    except (asyncio.TimeoutError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        registry.remove_client(req.mountpoint, writer)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    """Start the NTRIP caster."""
    parser = argparse.ArgumentParser(description="SP-Base NTRIP Caster")
    parser.add_argument("--port", type=int, default=_config["port"])
    parser.add_argument("--password", default=_config["password"])
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    log_level_name = str(_config["log_level"])
    level = (
        logging.DEBUG if args.debug else getattr(logging, log_level_name, logging.INFO)
    )
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    _config["password"] = args.password

    logger.info("═══════════════════════════════════════════════════")
    logger.info("  SP-Base Local NTRIP Caster")
    logger.info("═══════════════════════════════════════════════════")
    logger.info("  Port: %d", args.port)
    logger.info("  Password: %s", args.password)
    logger.info("  Supports: NTRIP v1.0 (SOURCE/ICY) and v2.0 (HTTP)")
    logger.info("═══════════════════════════════════════════════════")

    server = await asyncio.start_server(handle_connection, "0.0.0.0", args.port)

    logger.info("Listening on 0.0.0.0:%d", args.port)
    logger.info("Test: curl http://localhost:%d/", args.port)

    # Handle shutdown
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def signal_handler() -> None:
        logger.info("Shutting down...")
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    async with server:
        await stop.wait()

    logger.info("NTRIP Caster stopped.")


if __name__ == "__main__":
    asyncio.run(main())
