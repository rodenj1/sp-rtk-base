"""Tests for verifying a Correction source (issue #194).

Seam under test: ``POST /api/correction-sources/verify`` over HTTP
(TestClient), with a real CorrectionSourceVerificationService (its data
window shortened) against a scripted fake NTRIP caster on a real localhost
socket.
"""

from __future__ import annotations

import base64
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.models.config_models import (
    CorrectionSourceProfile,
    NtripCorrectionConfig,
)
from sp_rtk_base.services import (
    get_correction_verification_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.correction_verification import (
    CorrectionSourceVerificationService,
)
from tests.fixtures.scripted_ntrip_caster import FakeCaster, Script
from tests.unit.msm_frames import other_frame

VERIFY = "/api/correction-sources/verify"
SECRET = "s3cret-pa55"
WINDOW_S = 1.0

ICY = b"ICY 200 OK\r\n"
REF_1005 = other_frame(1005).data
REF_1006 = other_frame(1006).data
NOT_REF = other_frame(1033).data  # a valid Frame, but not a reference position


@pytest.fixture
def caster() -> Iterator[FakeCaster]:
    fake = FakeCaster()
    yield fake
    fake.close()


@pytest.fixture
def verifier() -> CorrectionSourceVerificationService:
    return CorrectionSourceVerificationService(
        connect_timeout_seconds=2.0, data_window_seconds=WINDOW_S
    )


@pytest.fixture
def client(
    api_client_with_services: TestClient,
    verifier: CorrectionSourceVerificationService,
) -> TestClient:
    app: Any = api_client_with_services.app
    app.dependency_overrides[get_correction_verification_service] = lambda: verifier
    return api_client_with_services


def _form(fake: FakeCaster, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "caster": "127.0.0.1",
        "port": fake.port,
        "mountpoint": "MP1",
        "username": "rover",
        "password": SECRET,
        "version": "1.0",
        "tls": False,
    }
    body.update(overrides)
    return body


def _stages(result: dict[str, Any]) -> dict[str, tuple[str, str | None]]:
    return {s["stage"]: (s["status"], s["code"]) for s in result["stages"]}


SKIPPED: tuple[str, None] = ("skipped", None)


class TestGreen:
    def test_a_source_sending_its_reference_position_is_green(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=[NOT_REF, REF_1005]))

        response = client.post(VERIFY, json=_form(caster))

        assert response.status_code == 200
        result = response.json()
        assert result["verdict"] == "green"
        assert result["failing_stage"] is None
        assert [s["stage"] for s in result["stages"]] == [
            "connect",
            "caster",
            "auth",
            "mountpoint",
            "data",
        ]
        assert all(s["status"] == "passed" for s in result["stages"])
        verified = datetime.fromisoformat(result["verified_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
        assert (expires - verified).total_seconds() == 30.0

    def test_a_reference_position_ends_the_window_early(
        self, client: TestClient, caster: FakeCaster, verifier: Any
    ) -> None:
        verifier.data_window_seconds = 10.0
        caster.scripts.append(Script(reply=ICY + REF_1006))

        start = time.monotonic()
        result = client.post(VERIFY, json=_form(caster)).json()

        assert _stages(result)["data"] == ("passed", None)
        assert time.monotonic() - start < 3.0

    def test_an_anonymous_caster_that_accepts_passes_auth(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=[REF_1005]))

        result = client.post(
            VERIFY, json=_form(caster, username="", password="")
        ).json()

        assert _stages(result)["auth"] == ("passed", None)
        assert b"Authorization" not in caster.requests[0]


class TestWarning:
    def test_frames_without_a_reference_position_are_a_warning_not_a_red(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=[NOT_REF] * 5))

        result = client.post(VERIFY, json=_form(caster)).json()

        assert result["verdict"] == "green"
        assert _stages(result)["data"] == ("warning", "no_reference_position")


class TestRed:
    def test_a_closed_port_fails_connect_as_refused(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        unused = socket.create_server(("127.0.0.1", 0))
        port = unused.getsockname()[1]
        unused.close()

        result = client.post(VERIFY, json=_form(caster, port=port)).json()

        assert result["verdict"] == "red"
        assert result["failing_stage"] == "connect"
        assert _stages(result) == {
            "connect": ("failed", "refused"),
            "caster": SKIPPED,
            "auth": SKIPPED,
            "mountpoint": SKIPPED,
            "data": SKIPPED,
        }

    def test_an_unknown_host_fails_connect_as_dns(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        result = client.post(VERIFY, json=_form(caster, caster="caster.invalid")).json()

        assert _stages(result)["connect"] == ("failed", "dns")

    def test_a_plain_caster_fails_a_tls_handshake(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts.append(Script(greeting=ICY + b"\r\n"))

        result = client.post(VERIFY, json=_form(caster, version="2.0", tls=True)).json()

        assert _stages(result)["connect"] == ("failed", "tls_handshake")

    def test_a_reply_that_is_not_ntrip_fails_caster(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts.append(Script(reply=b"<html>Banned</html>\r\n", hold=False))

        result = client.post(VERIFY, json=_form(caster)).json()

        assert result["failing_stage"] == "caster"
        assert _stages(result)["connect"] == ("passed", None)
        assert _stages(result)["auth"] == SKIPPED

    def test_rejected_credentials_fail_auth(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts.append(
            Script(reply=b"HTTP/1.0 401 Unauthorized\r\n\r\n", hold=False)
        )

        result = client.post(VERIFY, json=_form(caster)).json()

        assert result["failing_stage"] == "auth"
        assert _stages(result)["caster"] == ("passed", None)
        assert _stages(result)["mountpoint"] == SKIPPED

    @pytest.mark.parametrize(
        "reply",
        [
            b"SOURCETABLE 200 OK\r\n\r\nSTR;OTHER;;\r\nENDSOURCETABLE\r\n",
            b"HTTP/1.1 404 Not Found\r\n\r\n",
        ],
    )
    def test_a_sourcetable_or_not_found_fails_mountpoint(
        self, client: TestClient, caster: FakeCaster, reply: bytes
    ) -> None:
        caster.scripts.append(Script(reply=reply, hold=False))

        result = client.post(VERIFY, json=_form(caster)).json()

        assert result["failing_stage"] == "mountpoint"
        assert _stages(result)["auth"] == ("passed", None)
        assert _stages(result)["data"] == SKIPPED

    def test_no_bytes_fail_data_as_silent(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts.append(Script(reply=ICY))  # accepted, then nothing

        result = client.post(VERIFY, json=_form(caster)).json()

        assert result["failing_stage"] == "data"
        assert _stages(result)["data"] == ("failed", "silent")
        assert _stages(result)["mountpoint"] == ("passed", None)

    def test_bytes_that_never_form_a_frame_fail_data(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        nmea = b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n"
        caster.scripts.append(Script(reply=ICY, body=[nmea] * 5))

        result = client.post(VERIFY, json=_form(caster)).json()

        assert _stages(result)["data"] == ("failed", "not_rtcm3")


class TestRules:
    def test_a_verification_opens_one_connection_and_never_retries(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts += [
            Script(reply=b"HTTP/1.0 401 Unauthorized\r\n\r\n", hold=False),
            Script(reply=ICY, body=[REF_1005]),
        ]

        client.post(VERIFY, json=_form(caster))
        time.sleep(0.2)

        assert len(caster.requests) == 1

    def test_a_blank_password_uses_the_saved_sources(
        self,
        client: TestClient,
        caster: FakeCaster,
        mock_config_service: ConfigService,
    ) -> None:
        mock_config_service.create_correction_source(
            CorrectionSourceProfile(
                name="mine",
                config=NtripCorrectionConfig(
                    caster="127.0.0.1",
                    port=caster.port,  # the same caster the form names
                    mountpoint="MP1",
                    username="rover",
                    password=SECRET,
                ),
            )
        )
        caster.scripts.append(Script(reply=ICY, body=[REF_1005]))

        client.post(VERIFY, json=_form(caster, password="", name="mine"))

        expected = base64.b64encode(f"rover:{SECRET}".encode())
        assert b"Basic " + expected in caster.requests[0]

    def test_a_typed_password_wins_over_the_saved_one(
        self,
        client: TestClient,
        caster: FakeCaster,
        mock_config_service: ConfigService,
    ) -> None:
        mock_config_service.create_correction_source(
            CorrectionSourceProfile(
                name="mine",
                config=NtripCorrectionConfig(
                    caster="127.0.0.1", mountpoint="MP1", password=SECRET
                ),
            )
        )
        caster.scripts.append(Script(reply=ICY, body=[REF_1005]))

        client.post(VERIFY, json=_form(caster, password="typed", name="mine"))

        assert base64.b64encode(b"rover:typed") in caster.requests[0]

    def test_a_second_concurrent_verification_is_refused(
        self, client: TestClient, caster: FakeCaster, verifier: Any
    ) -> None:
        verifier.data_window_seconds = 2.0
        caster.scripts.append(Script(reply=ICY))  # silent: runs the whole window
        first: dict[str, Any] = {}

        def _first() -> None:
            first["response"] = client.post(VERIFY, json=_form(caster))

        running = threading.Thread(target=_first)
        running.start()
        time.sleep(0.5)
        second = client.post(VERIFY, json=_form(caster))
        running.join(10)

        assert second.status_code == 409
        assert second.json()["code"] == "verification_in_progress"
        assert first["response"].status_code == 200

    def test_the_password_is_never_in_the_response(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts.append(Script(reply=b"HTTP/1.0 401 Unauthorized\r\n\r\n"))

        response = client.post(VERIFY, json=_form(caster))

        assert SECRET not in response.text

    def test_an_invalid_form_is_refused_without_echoing_it(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        response = client.post(VERIFY, json=_form(caster, version="1.0", tls=True))

        assert response.status_code == 422
        assert SECRET not in response.text


def _listener(handle: Any) -> tuple[socket.socket, int]:
    """A localhost listener that hands one accepted connection to ``handle``."""
    server = socket.create_server(("127.0.0.1", 0))

    def _serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            try:
                handle(conn)
            except OSError:
                pass

    threading.Thread(target=_serve, daemon=True).start()
    port: int = server.getsockname()[1]
    return server, port


def _hold_open(conn: socket.socket) -> None:
    while conn.recv(4096):
        pass


@pytest.fixture(scope="module")
def self_signed_cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A throwaway self-signed certificate for localhost (not in any CA store)."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    folder = tmp_path_factory.mktemp("tls")
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class TestConnectCodes:
    def test_a_caster_that_never_answers_the_tls_hello_is_a_timeout(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        server, port = _listener(_hold_open)
        try:
            result = client.post(
                VERIFY, json=_form(caster, port=port, version="2.0", tls=True)
            ).json()
        finally:
            server.close()

        assert _stages(result)["connect"] == ("failed", "timeout")

    def test_an_untrusted_certificate_is_a_tls_certificate_failure(
        self,
        client: TestClient,
        caster: FakeCaster,
        self_signed_cert: tuple[Path, Path],
    ) -> None:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(*self_signed_cert)

        def _tls_caster(conn: socket.socket) -> None:
            with tls.wrap_socket(conn, server_side=True) as wrapped:
                wrapped.recv(4096)

        server, port = _listener(_tls_caster)
        try:
            result = client.post(
                VERIFY,
                json=_form(
                    caster, caster="localhost", port=port, version="2.0", tls=True
                ),
            ).json()
        finally:
            server.close()

        assert _stages(result)["connect"] == ("failed", "tls_certificate")


class TestSavedPasswordStaysWithItsCaster:
    def test_the_saved_password_is_never_sent_to_another_caster(
        self,
        client: TestClient,
        caster: FakeCaster,
        mock_config_service: ConfigService,
    ) -> None:
        # Saved against another caster: Verify must not send its password here
        mock_config_service.create_correction_source(
            CorrectionSourceProfile(
                name="mine",
                config=NtripCorrectionConfig(
                    caster="caster.example.com",
                    mountpoint="MP1",
                    username="rover",
                    password=SECRET,
                ),
            )
        )
        caster.scripts.append(Script(reply=ICY, body=[REF_1005]))

        client.post(VERIFY, json=_form(caster, password="", name="mine"))

        assert base64.b64encode(f"rover:{SECRET}".encode()) not in caster.requests[0]

    def test_a_closed_stream_mid_window_is_still_one_connection(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts += [
            Script(reply=ICY, body=[NOT_REF], hold=False),  # then closes
            Script(reply=ICY, body=[REF_1005]),
        ]

        result = client.post(VERIFY, json=_form(caster)).json()
        time.sleep(0.2)

        assert _stages(result)["data"] == ("warning", "no_reference_position")
        assert len(caster.requests) == 1

    def test_a_400_fails_caster_as_a_bad_reply(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts.append(
            Script(reply=b"HTTP/1.1 400 Bad Request\r\n\r\n", hold=False)
        )

        result = client.post(VERIFY, json=_form(caster)).json()

        assert _stages(result)["caster"] == ("failed", "bad_reply")

    def test_the_default_data_window_is_15_seconds(self) -> None:
        assert CorrectionSourceVerificationService().data_window_seconds == 15.0


class TestReferencePositionArrivesLate:
    """EarthScope sends its 1005 only every 30 s (bench, #197): Verify must
    keep listening for it once Frames are flowing, not stop at the silence
    window."""

    def test_a_1005_after_the_silence_window_still_passes(
        self, client: TestClient, caster: FakeCaster, verifier: Any
    ) -> None:
        verifier.data_window_seconds = 1.0  # silence: give up after 1 s
        verifier.reference_window_seconds = 4.0  # Frames flowing: wait for 1005
        # Frames every 20 ms, and the 1005 only after about 1.6 s.
        caster.scripts.append(Script(reply=ICY, body=[NOT_REF] * 80 + [REF_1005]))

        result = client.post(VERIFY, json=_form(caster)).json()

        assert result["verdict"] == "green"
        assert _stages(result)["data"] == ("passed", None)

    def test_frames_without_a_1005_still_warn_after_the_longer_wait(
        self, client: TestClient, caster: FakeCaster, verifier: Any
    ) -> None:
        verifier.data_window_seconds = 1.0
        verifier.reference_window_seconds = 2.0
        caster.scripts.append(Script(reply=ICY, body=[NOT_REF] * 200))

        start = time.monotonic()
        result = client.post(VERIFY, json=_form(caster)).json()

        assert _stages(result)["data"] == ("warning", "no_reference_position")
        assert time.monotonic() - start < 4.0  # it doesn't wait past the window
