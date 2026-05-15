#!/usr/bin/env python3
"""NTRIP Caster Validation Tool.

Tests a local (or remote) NTRIP caster by performing protocol-level
handshakes for both NTRIP v1.0 and v2.0, verifying authentication,
sourcetable retrieval, and data push capabilities.

This is a standalone tool — no sp-base or sp-rtk-base-relay imports needed.

Usage:
    uv run python tools/test_ntrip_caster.py
    uv run python tools/test_ntrip_caster.py --host localhost --port 2101
    uv run python tools/test_ntrip_caster.py --password testpass --mountpoint MY_MOUNT

Exit codes:
    0 = all tests passed
    1 = one or more tests failed
"""

from __future__ import annotations

import argparse
import base64
import socket
import sys
import time


# ---------------------------------------------------------------------------
# ANSI colors for terminal output
# ---------------------------------------------------------------------------

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✓ PASS{RESET}: {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗ FAIL{RESET}: {msg}")


def info(msg: str) -> None:
    print(f"  {CYAN}ℹ{RESET} {msg}")


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")
    print("─" * 60)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

# Fake RTCM3 data: valid preamble (0xD3) + length + dummy payload
# This is a minimal RTCM-like frame that a caster should accept as binary data
FAKE_RTCM_DATA = (
    b"\xd3\x00\x0d"  # Preamble + 13 bytes length
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"  # 13 bytes payload
    b"\x00\x00\x00"  # 3 bytes CRC (dummy)
)


def tcp_connect(host: str, port: int, timeout: float = 5.0) -> socket.socket:
    """Create a TCP connection to the caster."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((host, port))
    return sock


def read_response(sock: socket.socket, timeout: float = 5.0) -> str:
    """Read response until \\r\\n or timeout."""
    sock.settimeout(timeout)
    buf = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\r\n" in buf:
                break
    except socket.timeout:
        pass
    return buf.decode("ascii", errors="replace")


def read_full_response(sock: socket.socket, timeout: float = 5.0) -> str:
    """Read response until \\r\\n\\r\\n or timeout (for multi-line responses)."""
    sock.settimeout(timeout)
    buf = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\r\n\r\n" in buf or b"ENDSOURCETABLE" in buf:
                break
    except socket.timeout:
        pass
    return buf.decode("ascii", errors="replace")


# ---------------------------------------------------------------------------
# Test: TCP connectivity
# ---------------------------------------------------------------------------

def test_tcp_connectivity(host: str, port: int) -> bool:
    """Test basic TCP connectivity to the caster."""
    try:
        sock = tcp_connect(host, port)
        sock.close()
        ok(f"TCP connection to {host}:{port}")
        return True
    except OSError as e:
        fail(f"TCP connection to {host}:{port}: {e}")
        return False


# ---------------------------------------------------------------------------
# Test: Sourcetable retrieval (NTRIP v1.0 style)
# ---------------------------------------------------------------------------

def test_sourcetable_v1(host: str, port: int) -> bool:
    """Fetch the sourcetable using NTRIP v1.0 GET / request."""
    try:
        sock = tcp_connect(host, port)
        request = (
            f"GET / HTTP/1.0\r\n"
            f"User-Agent: NTRIP sp-base-test/1.0\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = read_full_response(sock, timeout=5.0)
        sock.close()

        if "SOURCETABLE" in response or "200" in response:
            ok("Sourcetable retrieval (v1.0 GET /)")
            # Show sourcetable content
            for line in response.split("\r\n"):
                if line.strip():
                    info(f"  {line.strip()}")
            return True
        else:
            fail(f"Sourcetable retrieval (v1.0): unexpected response: {response[:200]!r}")
            return False
    except OSError as e:
        fail(f"Sourcetable retrieval (v1.0): {e}")
        return False


# ---------------------------------------------------------------------------
# Test: Sourcetable retrieval (NTRIP v2.0 style)
# ---------------------------------------------------------------------------

def test_sourcetable_v2(host: str, port: int) -> bool:
    """Fetch the sourcetable using NTRIP v2.0 GET / request."""
    try:
        sock = tcp_connect(host, port)
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Ntrip-Version: Ntrip/2.0\r\n"
            f"User-Agent: NTRIP sp-base-test/1.0\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = read_full_response(sock, timeout=5.0)
        sock.close()

        if "SOURCETABLE" in response or "200" in response:
            ok("Sourcetable retrieval (v2.0 GET /)")
            return True
        else:
            fail(f"Sourcetable retrieval (v2.0): unexpected response: {response[:200]!r}")
            return False
    except OSError as e:
        fail(f"Sourcetable retrieval (v2.0): {e}")
        return False


# ---------------------------------------------------------------------------
# Test: NTRIP v1.0 SOURCE authentication (server push)
# ---------------------------------------------------------------------------

def test_v1_source_auth(
    host: str, port: int, password: str, mountpoint: str
) -> bool:
    """Test NTRIP v1.0 SOURCE authentication (server role).

    Protocol:
        SOURCE <password> /<mountpoint>\\r\\n
        Source-Agent: NTRIP sp-base-test/1.0\\r\\n
        \\r\\n

    Expected: ICY 200 OK
    """
    try:
        sock = tcp_connect(host, port)
        request = (
            f"SOURCE {password} /{mountpoint}\r\n"
            f"Source-Agent: NTRIP sp-base-test/1.0\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = read_response(sock, timeout=5.0)
        sock.close()

        if "ICY 200 OK" in response or "200" in response:
            ok(f"NTRIP v1.0 SOURCE auth: {mountpoint} (password={password})")
            info(f"  Response: {response.strip()!r}")
            return True
        else:
            fail(f"NTRIP v1.0 SOURCE auth: {response.strip()!r}")
            return False
    except OSError as e:
        fail(f"NTRIP v1.0 SOURCE auth: {e}")
        return False


# ---------------------------------------------------------------------------
# Test: NTRIP v1.0 SOURCE auth with wrong password
# ---------------------------------------------------------------------------

def test_v1_source_auth_failure(
    host: str, port: int, mountpoint: str
) -> bool:
    """Test that NTRIP v1.0 SOURCE rejects a wrong password."""
    try:
        sock = tcp_connect(host, port)
        request = (
            f"SOURCE WRONG_PASSWORD /{mountpoint}\r\n"
            f"Source-Agent: NTRIP sp-base-test/1.0\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = read_response(sock, timeout=5.0)
        sock.close()

        # Should NOT contain "200 OK" or "ICY 200 OK"
        if "200" not in response or "ERROR" in response or "401" in response:
            ok("NTRIP v1.0 SOURCE auth rejection (wrong password)")
            info(f"  Response: {response.strip()!r}")
            return True
        else:
            fail(
                f"NTRIP v1.0 SOURCE with wrong password was accepted! "
                f"Response: {response.strip()!r}"
            )
            return False
    except OSError as e:
        # Connection reset/closed is also an acceptable rejection
        ok(f"NTRIP v1.0 SOURCE auth rejection (connection closed: {e})")
        return True


# ---------------------------------------------------------------------------
# Test: NTRIP v2.0 POST authentication (server push)
# ---------------------------------------------------------------------------

def test_v2_post_auth(
    host: str, port: int, username: str, password: str, mountpoint: str
) -> bool:
    """Test NTRIP v2.0 HTTP POST authentication (server role).

    Protocol:
        POST /<mountpoint> HTTP/1.1\\r\\n
        Host: <host>\\r\\n
        Ntrip-Version: Ntrip/2.0\\r\\n
        Authorization: Basic <base64(user:pass)>\\r\\n
        User-Agent: NTRIP sp-base-test/1.0\\r\\n
        Transfer-Encoding: chunked\\r\\n
        \\r\\n

    Expected: HTTP/1.1 200 OK
    """
    try:
        credentials = base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")

        sock = tcp_connect(host, port)
        request = (
            f"POST /{mountpoint} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Ntrip-Version: Ntrip/2.0\r\n"
            f"Authorization: Basic {credentials}\r\n"
            f"User-Agent: NTRIP sp-base-test/1.0\r\n"
            f"Transfer-Encoding: chunked\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = read_response(sock, timeout=5.0)
        sock.close()

        if "200" in response:
            ok(f"NTRIP v2.0 POST auth: {mountpoint} (user={username})")
            info(f"  Response: {response.strip()!r}")
            return True
        else:
            fail(f"NTRIP v2.0 POST auth: {response.strip()!r}")
            return False
    except OSError as e:
        fail(f"NTRIP v2.0 POST auth: {e}")
        return False


# ---------------------------------------------------------------------------
# Test: NTRIP v2.0 POST auth with wrong password
# ---------------------------------------------------------------------------

def test_v2_post_auth_failure(
    host: str, port: int, mountpoint: str
) -> bool:
    """Test that NTRIP v2.0 POST rejects wrong credentials."""
    try:
        credentials = base64.b64encode(
            b"wrong_user:wrong_password"
        ).decode("ascii")

        sock = tcp_connect(host, port)
        request = (
            f"POST /{mountpoint} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Ntrip-Version: Ntrip/2.0\r\n"
            f"Authorization: Basic {credentials}\r\n"
            f"User-Agent: NTRIP sp-base-test/1.0\r\n"
            f"Transfer-Encoding: chunked\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = read_response(sock, timeout=5.0)
        sock.close()

        if "401" in response or "403" in response or "200" not in response:
            ok("NTRIP v2.0 POST auth rejection (wrong credentials)")
            info(f"  Response: {response.strip()!r}")
            return True
        else:
            fail(
                f"NTRIP v2.0 POST with wrong credentials was accepted! "
                f"Response: {response.strip()!r}"
            )
            return False
    except OSError as e:
        ok(f"NTRIP v2.0 POST auth rejection (connection closed: {e})")
        return True


# ---------------------------------------------------------------------------
# Test: NTRIP v1.0 data push (SOURCE + send RTCM)
# ---------------------------------------------------------------------------

def test_v1_data_push(
    host: str, port: int, password: str, mountpoint: str
) -> bool:
    """Test pushing RTCM data via NTRIP v1.0 SOURCE protocol."""
    try:
        sock = tcp_connect(host, port)
        request = (
            f"SOURCE {password} /{mountpoint}\r\n"
            f"Source-Agent: NTRIP sp-base-test/1.0\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = read_response(sock, timeout=5.0)

        if "200" not in response and "ICY 200 OK" not in response:
            fail(f"NTRIP v1.0 data push: auth failed: {response.strip()!r}")
            sock.close()
            return False

        # Send fake RTCM data (raw bytes for v1.0)
        for _ in range(3):
            sock.sendall(FAKE_RTCM_DATA)
            time.sleep(0.1)

        sock.close()
        ok(f"NTRIP v1.0 data push: sent {len(FAKE_RTCM_DATA) * 3} bytes to /{mountpoint}")
        return True
    except OSError as e:
        fail(f"NTRIP v1.0 data push: {e}")
        return False


# ---------------------------------------------------------------------------
# Test: NTRIP v2.0 data push (POST + chunked encoding)
# ---------------------------------------------------------------------------

def test_v2_data_push(
    host: str, port: int, username: str, password: str, mountpoint: str
) -> bool:
    """Test pushing RTCM data via NTRIP v2.0 POST + chunked encoding."""
    try:
        credentials = base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")

        sock = tcp_connect(host, port)
        request = (
            f"POST /{mountpoint} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Ntrip-Version: Ntrip/2.0\r\n"
            f"Authorization: Basic {credentials}\r\n"
            f"User-Agent: NTRIP sp-base-test/1.0\r\n"
            f"Transfer-Encoding: chunked\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = read_response(sock, timeout=5.0)

        if "200" not in response:
            fail(f"NTRIP v2.0 data push: auth failed: {response.strip()!r}")
            sock.close()
            return False

        # Send fake RTCM data with chunked encoding
        total_sent = 0
        for _ in range(3):
            chunk_header = f"{len(FAKE_RTCM_DATA):x}\r\n".encode("ascii")
            chunk_trailer = b"\r\n"
            sock.sendall(chunk_header + FAKE_RTCM_DATA + chunk_trailer)
            total_sent += len(FAKE_RTCM_DATA)
            time.sleep(0.1)

        # Send final zero-length chunk to indicate end
        sock.sendall(b"0\r\n\r\n")

        sock.close()
        ok(f"NTRIP v2.0 data push: sent {total_sent} bytes (chunked) to /{mountpoint}")
        return True
    except OSError as e:
        fail(f"NTRIP v2.0 data push: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="NTRIP Caster Validation Tool — tests v1.0 and v2.0 protocols",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s                           # Test localhost:2101 with defaults\n"
            "  %(prog)s --host localhost --port 2101\n"
            "  %(prog)s --password mypass --mountpoint MY_MOUNT\n"
            "  %(prog)s --host rtk2go.com --port 2101 --password secret\n"
        ),
    )
    parser.add_argument(
        "--host", default="localhost", help="Caster hostname (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=2101, help="Caster port (default: 2101)"
    )
    parser.add_argument(
        "--password", default="testpass",
        help="Server password (default: testpass)"
    )
    parser.add_argument(
        "--username", default="test",
        help="Username for v2.0 auth (default: test)"
    )
    parser.add_argument(
        "--mountpoint", default="TEST",
        help="Mountpoint name (default: TEST)"
    )
    parser.add_argument(
        "--skip-data", action="store_true",
        help="Skip data push tests (auth-only validation)"
    )

    args = parser.parse_args()

    print(f"\n{BOLD}═══════════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  NTRIP Caster Validation Tool{RESET}")
    print(f"{BOLD}═══════════════════════════════════════════════════════════{RESET}")
    print(f"  Target: {args.host}:{args.port}")
    print(f"  Mountpoint: {args.mountpoint}")
    print(f"  Password: {args.password}")
    print(f"  Username (v2): {args.username}")

    passed = 0
    failed = 0
    total = 0

    def run_test(test_fn: object, *test_args: object) -> None:
        nonlocal passed, failed, total
        total += 1
        # Type narrowing: test_fn is always a callable returning bool
        assert callable(test_fn)
        if test_fn(*test_args):
            passed += 1
        else:
            failed += 1

    # --- TCP connectivity ---
    section("1. TCP Connectivity")
    run_test(test_tcp_connectivity, args.host, args.port)

    if failed > 0:
        print(f"\n{RED}Cannot connect to caster. Aborting remaining tests.{RESET}")
        print(f"\nMake sure the caster is running:")
        print(f"  docker compose -f docker/ntrip-caster/docker-compose.yml up -d")
        return 1

    # --- Sourcetable ---
    section("2. Sourcetable Retrieval")
    run_test(test_sourcetable_v1, args.host, args.port)
    run_test(test_sourcetable_v2, args.host, args.port)

    # --- NTRIP v1.0 Authentication ---
    section("3. NTRIP v1.0 Authentication (SOURCE)")
    run_test(test_v1_source_auth, args.host, args.port, args.password, args.mountpoint)
    run_test(test_v1_source_auth_failure, args.host, args.port, args.mountpoint)

    # --- NTRIP v2.0 Authentication ---
    section("4. NTRIP v2.0 Authentication (POST)")
    run_test(
        test_v2_post_auth,
        args.host, args.port, args.username, args.password, args.mountpoint,
    )
    run_test(test_v2_post_auth_failure, args.host, args.port, args.mountpoint)

    # --- Data push ---
    if not args.skip_data:
        section("5. NTRIP v1.0 Data Push (SOURCE + raw RTCM)")
        run_test(
            test_v1_data_push,
            args.host, args.port, args.password, f"{args.mountpoint}_V1",
        )

        section("6. NTRIP v2.0 Data Push (POST + chunked RTCM)")
        run_test(
            test_v2_data_push,
            args.host, args.port, args.username, args.password,
            f"{args.mountpoint}_V2",
        )

    # --- Summary ---
    print(f"\n{BOLD}═══════════════════════════════════════════════════════════{RESET}")
    if failed == 0:
        print(f"  {GREEN}{BOLD}ALL {passed}/{total} TESTS PASSED{RESET}")
    else:
        print(f"  {RED}{BOLD}{failed}/{total} TESTS FAILED{RESET}, {passed} passed")
    print(f"{BOLD}═══════════════════════════════════════════════════════════{RESET}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
