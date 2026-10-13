"""A throwaway certificate authority for tests that need real TLS.

``TestCa().server_context("portal.test")`` is a server context whose
certificate names ``portal.test`` (an IP literal goes in as an IP SAN);
``.client_context()`` trusts only this authority and checks host names, as
the agent's own clients do against public roots.
"""

from __future__ import annotations

import ipaddress
import ssl
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


class TestCa:
    """Its certificates pass Python 3.13's strict X.509 checks (key
    identifiers present)."""

    __test__ = False  # not a pytest class

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        self._cert = (
            x509.CertificateBuilder()
            .subject_name(_name("sp-rtk-base test CA"))
            .issuer_name(_name("sp-rtk-base test CA"))
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(self._key.public_key()),
                False,
            )
            .sign(self._key, hashes.SHA256())
        )
        self._dir = tempfile.TemporaryDirectory()

    @property
    def pem(self) -> str:
        return self._cert.public_bytes(serialization.Encoding.PEM).decode()

    def client_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cadata=self.pem)

    def server_context(self, *names: str) -> ssl.SSLContext:
        """A server context with a certificate for ``names`` (DNS names or
        IP literals)."""
        key = ec.generate_private_key(ec.SECP256R1())
        sans: list[x509.GeneralName] = []
        for name in names:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                sans.append(x509.DNSName(name))
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name(names[0]))
            .issuer_name(self._cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(sans), False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self._key.public_key()
                ),
                False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                False,
            )
            .sign(self._key, hashes.SHA256())
        )
        path = Path(self._dir.name) / f"{x509.random_serial_number()}.pem"
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            + cert.public_bytes(serialization.Encoding.PEM)
        )
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(path)
        return context
