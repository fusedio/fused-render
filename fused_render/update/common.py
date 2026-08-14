"""Platform-neutral core of the self-updater: fetch + cryptographically
verify the signed update manifest, and stream-download the artifact it points
at while checking its SHA-256 against the signed value.

Factored out of supervisor/_win32/update.py so the macOS in-app updater
(update/mac.py) shares one implementation of the security-critical path. The
signing scheme is unchanged: an ed25519 signature over a domain-separated
`version\nsha256` line (scripts/windows/generate_update_manifest.py), so a
CDN/bucket compromise can't forge a version or point the client at different
bytes. The artifact URL itself is not signed — its content is pinned by the
signed sha256 — so downloads additionally require HTTPS end to end.

Note the signing context is shared across platforms: a Windows manifest
replayed at the macOS manifest URL verifies, but the sha256 then pins the
Windows installer bytes, which the macOS install path cannot mount — the swap
never happens. Version downgrade replays are rejected by is_newer().
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PUBLIC_KEY = base64.b64decode("u4eiDvccdWmsVCN0nifCEXqmU+xVGIDPe8LP5KRlDns=")
SIGNING_CONTEXT = "fused-render-update"
FETCH_TIMEOUT_S = 15.0
DOWNLOAD_TIMEOUT_S = 300.0
STARTUP_DELAY_S = 60.0
CHECK_INTERVAL_S = 6 * 60 * 60
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 600 * 1024 * 1024
DOWNLOAD_CHUNK = 1024 * 1024


class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """urlopen follows redirects by default; refuse any that leave HTTPS so a
    compromised CDN can't 302 the download to http and bypass the https-only
    control (integrity still rests on the signed sha256, but don't ship bytes
    over cleartext)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise urllib.error.URLError("refusing non-https redirect during update")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(HttpsOnlyRedirect)


def urlopen(url: str, timeout: float):
    return _opener.open(url, timeout=timeout)


def fetch_manifest(url: str, *, urlopen_fn=None, public_key: bytes = PUBLIC_KEY) -> dict:
    """Fetch, validate, and cryptographically verify the manifest. The
    ed25519 signature is checked here — before any caller trusts `version` to
    decide "up to date" or to prompt — so a CDN/bucket compromise can't forge
    a version to suppress or fake an update. `urlopen_fn`/`public_key` are
    injectable for the platform wrappers' test seams."""
    if urlopen_fn is None:
        urlopen_fn = urlopen
    with urlopen_fn(url, FETCH_TIMEOUT_S) as resp:
        raw = resp.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("update manifest is too large")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or not all(
        isinstance(manifest.get(key), str)
        for key in ("version", "url", "sha256", "signature")
    ):
        raise ValueError("malformed update manifest")
    verify_signature(manifest["version"], manifest["sha256"], manifest["signature"],
                     public_key=public_key)
    return manifest


def verify_signature(version: str, sha256: str, signature: str, *,
                     public_key: bytes = PUBLIC_KEY) -> None:
    message = f"{SIGNING_CONTEXT}\n{version}\n{sha256}\n".encode("utf-8")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            base64.b64decode(signature), message
        )
    except InvalidSignature as error:
        raise ValueError("update manifest signature is invalid") from error


def is_newer(candidate: str, current: str) -> bool:
    def parts(version: str) -> tuple[int, ...]:
        return tuple(int(part) for part in version.split("."))

    return parts(candidate) > parts(current)


def download_verified(manifest: dict, *, dir: str | None = None,
                      prefix: str = "fused-render-update-", suffix: str = "",
                      max_bytes: int = MAX_ARTIFACT_BYTES,
                      progress=None, urlopen_fn=None) -> str:
    """Stream the artifact to a temp file (in `dir`, or the system temp dir)
    while hashing it, and confirm its SHA-256 matches the signed value. The
    manifest signature (over version + sha256) is already verified in
    fetch_manifest; the URL is not signed, so require HTTPS. `progress`
    (optional) is called with bytes downloaded so far after each chunk."""
    if urlopen_fn is None:
        urlopen_fn = urlopen
    url = manifest["url"]
    if not url.startswith("https://"):
        raise ValueError("update manifest url is not https")
    sha256 = manifest["sha256"]
    digest = hashlib.sha256()
    total = 0
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=dir)
    ok = False
    try:
        with os.fdopen(fd, "wb") as out, urlopen_fn(url, DOWNLOAD_TIMEOUT_S) as resp:
            while chunk := resp.read(DOWNLOAD_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("update download exceeds the size limit")
                digest.update(chunk)
                out.write(chunk)
                if progress is not None:
                    progress(total)
        if digest.hexdigest() != sha256:
            raise ValueError("downloaded file does not match the signed manifest")
        ok = True
        return path
    finally:
        if not ok:
            discard(path)


def discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
