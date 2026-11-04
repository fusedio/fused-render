"""A self-signed certificate for `https://localhost` loopback redirects.

Why: Slack lets an app be distributed to other workspaces only when every
redirect URL is HTTPS — `http://localhost` is for development in the app's
own workspace. A public PKCE app therefore registers
`https://localhost:4782x/callback`, and the loopback listener (oauth.py
LoopbackFlow, `redirect.scheme == "https"`) must speak TLS. Nothing signs
the certificate, so the browser warns once per sign-in ("Advanced › Proceed")
— the owner's call, 2026-11-04, over a Fused-hosted bounce page (a request to
a Fused host carrying the code) and over bring-your-own only. The card says
so, and the paste fallback (the address the warning page sits on) finishes
the sign-in without clicking through.

One certificate per state folder: `<home_dir()>/connect/localhost.pem` +
`localhost.key` (P-256, 800 days — Apple's verifier rejects a TLS server
certificate valid for more than 825 days as "not standards compliant", and
Safari and the truststore-backed client both go through it — SAN `localhost`
+ `127.0.0.1` + `::1`, EKU serverAuth), reissued when missing, unreadable or
inside 14 days of expiry. Key 0600.
`cryptography` is not a core dependency (lan_tls.py has the same note); on a
bare install the `openssl` CLI makes the same certificate, and with neither
the listener cannot start — the error names the fix.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import os
import shutil
import subprocess
import threading

logger = logging.getLogger(__name__)

DAYS = 800  # < Apple's 825-day cap for TLS server certificates
_lock = threading.Lock()
NAMES = ["localhost"]
IPS = ["127.0.0.1", "::1"]


def _dir() -> str:
    from fused_render.shell import storage

    d = os.path.join(storage.home_dir(), "connect")
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def paths() -> tuple[str, str]:
    d = _dir()
    return os.path.join(d, "localhost.pem"), os.path.join(d, "localhost.key")


def _write_private(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _fresh(cert_path: str, key_path: str) -> bool:
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        return False
    try:
        from cryptography import x509

        with open(cert_path, "rb") as f:
            cur = x509.load_pem_x509_certificate(f.read())
        return cur.not_valid_after_utc > _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=14)
    except ImportError:
        # No way to read the expiry: the file's own age dates the certificate.
        age = _dt.datetime.now(_dt.timezone.utc) - _dt.datetime.fromtimestamp(os.path.getmtime(cert_path), _dt.timezone.utc)
        return age < _dt.timedelta(days=DAYS - 14)
    except Exception:  # noqa: BLE001 — unreadable → reissue
        return False


def _issue_with_cryptography(cert_path: str, key_path: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    now = _dt.datetime.now(_dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "fused-render connect")])
    san = [x509.DNSName(h) for h in NAMES] + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in IPS]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=DAYS))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    _write_private(key_path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def _issue_with_openssl(cert_path: str, key_path: str) -> None:
    exe = shutil.which("openssl")
    if not exe:
        raise RuntimeError("no way to make the localhost certificate: install the `cryptography` package "
                           "(pip install cryptography) or the `openssl` command")
    san = ",".join([f"DNS:{h}" for h in NAMES] + [f"IP:{ip}" for ip in IPS])
    subprocess.run([exe, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-keyout", key_path, "-out", cert_path, "-days", str(DAYS),
                    "-subj", "/CN=localhost/O=fused-render connect", "-addext", f"subjectAltName={san}"],
                   check=True, capture_output=True, timeout=60)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass


def localhost_cert() -> tuple[str, str]:
    """(cert_pem_path, key_pem_path) for `https://localhost`, issued on first use."""
    with _lock:
        cert_path, key_path = paths()
        if _fresh(cert_path, key_path):
            return cert_path, key_path
        try:
            _issue_with_cryptography(cert_path, key_path)
        except ImportError:
            _issue_with_openssl(cert_path, key_path)
        logger.info("connect: localhost certificate issued at %s", cert_path)
        return cert_path, key_path


def server_context():
    """A server-side TLS context on the localhost certificate (lan_tls.server_ssl_context:
    the stdlib class, not the truststore wrapper `_system_trust` installs)."""
    from fused_render.lan_tls import server_ssl_context

    cert_path, key_path = localhost_cert()
    return server_ssl_context(cert_path, key_path)
