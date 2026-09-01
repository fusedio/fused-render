"""A private certificate authority for the local-network listener (lan.py).

Why: a phone browser on plain http withholds the microphone and clipboard
(no secure context), and a trusted certificate for a ``.local`` name cannot
come from a public CA. The native iOS shell (ios/) makes a private CA
workable with ZERO user steps: the pairing QR carries the CA's fingerprint,
the app fetches the CA over http, checks the fingerprint, and pins it — so the
https listener is trusted by the app alone. Browsers keep using http.

Files live in ``<state dir>/lan_tls/``: ``ca.pem``/``ca.key`` (10 years) and
``server.pem``/``server.key`` (1 year), the leaf reissued whenever the set of
names it must carry — the two mDNS names plus the current LAN address — changes.
Keys are P-256; the CA key never leaves this folder and signs nothing but the
leaf. ``cryptography`` is already a dependency.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import logging
import os

logger = logging.getLogger("fused_render.lan_tls")

CA_DAYS = 3650
LEAF_DAYS = 365


def _dir() -> str:
    from fused_render.shell.storage import home_dir

    path = os.path.join(home_dir(), "lan_tls")
    os.makedirs(path, exist_ok=True)
    return path


def ca_pem_path() -> str:
    return os.path.join(_dir(), "ca.pem")


def _write_private(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_ca():
    """Load or create the CA. Returns (cert, key)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    cert_path, key_path = ca_pem_path(), os.path.join(_dir(), "ca.key")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        with open(key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        if cert.not_valid_after_utc > _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=30):
            return cert, key
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Fused Render local CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Fused Render"),
    ])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                     content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    _write_private(key_path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    logger.info("lan_tls: new local CA at %s", cert_path)
    return cert, key


def ensure_server_cert(hosts: list[str], ips: list[str]) -> tuple[str, str]:
    """The leaf for these names, (re)issued when they changed or it is close to
    expiry. Returns (cert_pem_path, key_pem_path)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca_cert, ca_key = _ensure_ca()
    cert_path, key_path = os.path.join(_dir(), "server.pem"), os.path.join(_dir(), "server.key")
    wanted_dns = sorted({h.rstrip(".") for h in hosts if h})
    wanted_ips = sorted({ip for ip in ips if ip})

    if os.path.exists(cert_path) and os.path.exists(key_path):
        try:
            with open(cert_path, "rb") as f:
                cur = x509.load_pem_x509_certificate(f.read())
            san = cur.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have_dns = sorted(san.get_values_for_type(x509.DNSName))
            have_ips = sorted(str(i) for i in san.get_values_for_type(x509.IPAddress))
            fresh = cur.not_valid_after_utc > _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=14)
            # Signature check, not an issuer-name check: a regenerated CA has
            # the same name, and a leaf the old key signed would then be served
            # while the QR advertises the new CA — every new pairing failing.
            cur.verify_directly_issued_by(ca_cert)
            if have_dns == wanted_dns and have_ips == wanted_ips and fresh:
                return cert_path, key_path
        except Exception:  # noqa: BLE001 — unreadable leaf → reissue
            pass

    key = ec.generate_private_key(ec.SECP256R1())
    now = _dt.datetime.now(_dt.timezone.utc)
    san_entries = [x509.DNSName(h) for h in wanted_dns]
    for ip in wanted_ips:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, wanted_dns[0] if wanted_dns else "fused-render")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=LEAF_DAYS))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_agreement=True,
                                     content_commitment=False, data_encipherment=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
        # The chain: leaf then CA, so clients that want the issuer get it.
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))
    _write_private(key_path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    logger.info("lan_tls: server certificate issued for %s %s", wanted_dns, wanted_ips)
    return cert_path, key_path


def ca_fingerprint() -> str:
    """SHA-256 of the CA certificate's DER, lowercase hex — what the QR carries
    and what the app checks the fetched CA against."""
    from cryptography.hazmat.primitives import serialization

    cert, _ = _ensure_ca()
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def ca_pem() -> bytes:
    _ensure_ca()
    with open(ca_pem_path(), "rb") as f:
        return f.read()
