"""One self-signed certificate for the local-network listener (lan.py).

Why https at all: WebKit withholds the camera, the microphone and the
clipboard from a page that is not a secure context, and those have to work
in the Fused Render app on the phone. Why not a public CA: a ``.local`` name
or a private address cannot get a publicly trusted certificate.

Why ONE self-signed certificate rather than a private CA: the app pins the
certificate itself, by the SHA-256 of its DER, which the pairing QR carries
as ``fp``. The app checks every TLS handshake against that hash and nothing
else — not the hostname, not the chain, not the expiry — so the certificate
never needs to name an address, never needs reissuing when the computer's
address or name changes, and nothing has to be fetched before pairing.
(The previous design kept a CA so a leaf naming the current IP could be
reissued under a stable anchor; with hostname checking gone, the leaf IS
the anchor and the CA layer had nothing left to do.)

Files live in ``<state dir>/lan_tls/``: ``server.pem`` and ``server.key``
(P-256, ten years). The key never leaves this folder. ``cryptography`` is
NOT a core dependency of fused-render — it arrives only via the
``[bundled]``/``[fused]`` extras — so every function here imports it lazily
and callers guard for ``ModuleNotFoundError`` (lan.py reports it in
``tls_error`` and refuses to mint a pairing code).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import threading

logger = logging.getLogger("fused_render.lan_tls")

CERT_DAYS = 3650

# The one certificate this process uses, per state folder: the fingerprint a
# QR carries and the certificate the listener presents must be the same
# object whatever the folder looks like between two calls.
_cert_by_dir: dict = {}
_lock = threading.Lock()


def _dir() -> str:
    from fused_render.shell.storage import home_dir

    path = os.path.join(home_dir(), "lan_tls")
    os.makedirs(path, exist_ok=True)
    return path


def cert_path() -> str:
    return os.path.join(_dir(), "server.pem")


def key_path() -> str:
    return os.path.join(_dir(), "server.key")


def _write_private(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def ensure_cert():
    """Load or create the certificate. Returns the ``cryptography`` certificate
    object; the PEM files are at ``cert_path()``/``key_path()``. Memoised for
    the life of the process (see ``_cert_by_dir``), and serialised across
    processes sharing the state folder (``_file_lock``): two first starts at
    once must not interleave their writes into a mismatched pair."""
    folder = _dir()
    with _lock:
        if folder not in _cert_by_dir:
            with _file_lock(folder):
                _cert_by_dir[folder] = _load_or_create()
        return _cert_by_dir[folder]


class _file_lock:
    """A cross-process lock on ``<folder>/.lock`` (``fcntl.flock`` where it
    exists; a no-op elsewhere, where one desktop app owns the folder)."""

    def __init__(self, folder: str):
        self._path = os.path.join(folder, ".lock")
        self._fh = None

    def __enter__(self):
        try:
            import fcntl

            self._fh = open(self._path, "a+")  # noqa: SIM115 — released in __exit__
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            self._fh.close()


def _load_or_create():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    cpath, kpath = cert_path(), key_path()
    if os.path.exists(cpath) and os.path.exists(kpath):
        try:
            with open(cpath, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read())
            with open(kpath, "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
            # The pair must match, or the listener fails its handshake on
            # every start until someone deletes the files by hand.
            if cert.public_key().public_numbers() != key.public_key().public_numbers():
                raise ValueError("certificate and key do not match")
            # Expiry is not checked by the app (it pins the hash), but a
            # certificate past its date is still worth replacing well ahead:
            # phones must pair again when it changes, so do it once a year
            # before the end, not the day of. (`not_valid_after_utc` arrived
            # in cryptography 42; the naive property is UTC on older ones.)
            expires = getattr(cert, "not_valid_after_utc", None)
            if expires is None:
                expires = cert.not_valid_after.replace(tzinfo=_dt.timezone.utc)
            if expires > _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=365):
                return cert
            logger.warning("lan_tls: certificate expires within a year - issuing a new one; phones must pair again")
        except Exception:  # noqa: BLE001 — unreadable files → reissue, and say so
            logger.warning("lan_tls: certificate files unreadable - issuing a new one; phones must pair again",
                           exc_info=True)
    elif os.path.exists(cpath) or os.path.exists(kpath):
        logger.warning("lan_tls: certificate incomplete (cert %s, key %s) - issuing a new one; "
                       "phones must pair again", os.path.exists(cpath), os.path.exists(kpath))

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Fused Render"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Fused Render"),
    ])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=CERT_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_agreement=True,
                                     content_commitment=False, data_encipherment=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(cpath, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    _write_private(kpath, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    logger.info("lan_tls: new certificate at %s", cpath)
    return cert


def fingerprint() -> str:
    """SHA-256 of the certificate's DER, lowercase hex — what the QR carries
    as ``fp`` and what the app compares every handshake against."""
    from cryptography.hazmat.primitives import serialization

    cert = ensure_cert()
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def server_ssl_context(certfile: str, keyfile: str, alpn_protocols=None):
    """A server-side context built from the stdlib class rather than
    ``ssl.SSLContext``, which ``_system_trust`` swaps for truststore's
    client-side wrapper (its server handshake fails on macOS; D1442). Shared
    with connect/tls.py."""
    import ssl

    from fused_render._system_trust import ORIGINAL_SSL_CONTEXT

    ctx = ORIGINAL_SSL_CONTEXT(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    if alpn_protocols:
        ctx.set_alpn_protocols(alpn_protocols)
    return ctx
