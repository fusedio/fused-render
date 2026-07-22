"""Update check (docs/PYTHON_SUPERVISOR_SPEC.md, "Software updates"). An
automatic background loop checks and, when a newer version exists, downloads +
verifies it silently and then prompts the user to install; the tray "Check for
updates..." item runs the same flow on demand. Install is always user-approved
— nothing is installed without a click. Runs on worker threads; never raises,
so a failed check can't tear down the Job-owned server."""
from __future__ import annotations

import base64
import ctypes
import glob
import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from fused_render import __version__
from fused_render.win_supervisor.paths import DesktopPaths

_MANIFEST_URL = "https://d2ic19jpchjovp.cloudfront.net/fused-render-windows/latest.json"
_PUBLIC_KEY = base64.b64decode("u4eiDvccdWmsVCN0nifCEXqmU+xVGIDPe8LP5KRlDns=")
_SIGNING_CONTEXT = "fused-render-update"
_FETCH_TIMEOUT_S = 15.0
_DOWNLOAD_TIMEOUT_S = 300.0
_STARTUP_DELAY_S = 60.0
_CHECK_INTERVAL_S = 6 * 60 * 60
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_INSTALLER_BYTES = 600 * 1024 * 1024
_DOWNLOAD_CHUNK = 1024 * 1024
_STAGE_PREFIX = "FusedRenderPy-"
_STAGE_SUFFIX = "-setup.exe"

_MB_OK = 0x0
_MB_YESNO = 0x4
_MB_ICONINFORMATION = 0x40
_MB_ICONWARNING = 0x30
_MB_ICONERROR = 0x10
_MB_SETFOREGROUND = 0x0001_0000
_IDYES = 6

# One check at a time (auto vs. manual, or two auto ticks), and don't re-prompt
# for a version the user already declined this session. Both are only touched
# while _check_lock is held, so the set needs no separate guard.
_check_lock = threading.Lock()
_prompted_versions: set[str] = set()


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """urlopen follows redirects by default; refuse any that leave HTTPS so a
    compromised CDN can't 302 the download to http and bypass the https-only
    control (integrity still rests on the signed sha256, but don't ship bytes
    over cleartext)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise urllib.error.URLError("refusing non-https redirect during update")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirect)


def _urlopen(url: str, timeout: float):
    return _opener.open(url, timeout=timeout)


def start_auto_checks(paths: DesktopPaths) -> None:
    """Spawn the background check loop: after a startup delay (so it never
    competes with launch), check now and every _CHECK_INTERVAL_S. Silent unless
    an update is downloaded and ready — set FUSED_RENDER_NO_AUTO_UPDATE to a
    non-empty value to disable it entirely."""
    if os.environ.get("FUSED_RENDER_NO_AUTO_UPDATE"):
        return

    def loop():
        time.sleep(_STARTUP_DELAY_S)
        swept = False
        while True:
            try:
                if not swept:  # once per session, but inside the safety net below
                    _sweep_stale_downloads()
                    swept = True
                _auto_check(paths)
            except Exception as error:  # noqa: BLE001 - a tick must never kill the loop
                paths.log(f"auto update tick failed: {error}")
            time.sleep(_CHECK_INTERVAL_S)

    threading.Thread(target=loop, daemon=True, name="fused-render-update-auto").start()


def check(paths: DesktopPaths) -> None:
    """Manual check (tray). Unlike the background loop this reports its result
    either way — "up to date", "already in progress", and failures all surface
    a dialog."""
    if not _check_lock.acquire(blocking=False):
        _alert("An update check or install is already in progress.", _MB_ICONINFORMATION)
        return
    try:
        try:
            manifest = _fetch_manifest()
            newer = _is_newer(manifest["version"], __version__)
        except (OSError, ValueError, http.client.HTTPException) as error:
            paths.log(f"update check failed: {error}")
            _alert("FusedRender could not check for updates right now.", _MB_ICONWARNING)
            return
        if not newer:
            _alert(f"FusedRender {__version__} is up to date.", _MB_ICONINFORMATION)
            return
        _offer_install(paths, manifest, announce_errors=True)
    finally:
        _check_lock.release()


def _auto_check(paths: DesktopPaths) -> None:
    """Background tick: silent on "no update" and on transient errors (logged
    only, never a dialog), so periodic checks don't nag. Prompts once per
    version per session once the installer is downloaded and verified."""
    if not _check_lock.acquire(blocking=False):
        return
    try:
        try:
            manifest = _fetch_manifest()
            if not _is_newer(manifest["version"], __version__):
                return
        except (OSError, ValueError, http.client.HTTPException) as error:
            paths.log(f"auto update check failed: {error}")
            return
        if manifest["version"] in _prompted_versions:
            return
        _offer_install(paths, manifest, announce_errors=False)
    finally:
        _check_lock.release()


def _offer_install(paths: DesktopPaths, manifest: dict, announce_errors: bool) -> None:
    """Download + verify, then prompt to install. On yes, launch the installer
    and return — its own --shutdown-for-upgrade path stops and relaunches the
    app. A declined version is remembered so the background loop won't re-offer
    it this session; an accepted-but-failed launch is not, so it retries. Call
    only while _check_lock is held."""
    version = manifest["version"]
    try:
        installer = _download_verified(manifest)
    except (OSError, ValueError, http.client.HTTPException) as error:
        paths.log(f"update download failed: {error}")
        if announce_errors:
            _alert("The update could not be downloaded or verified.", _MB_ICONERROR)
        return

    if _prompt_install(version) != _IDYES:
        _prompted_versions.add(version)
        _discard(installer)
        return
    try:
        # The file sat in %TEMP% while the prompt was open — re-hash right
        # before launch so we never run bytes that were swapped after verify.
        if _sha256_file(installer) != manifest["sha256"]:
            raise ValueError("staged installer changed after verification")
        os.startfile(installer)
    except (OSError, ValueError) as error:
        paths.log(f"update launch failed: {error}")
        _discard(installer)
        # Always alert (even on the background path): the user clicked Install,
        # so a failure here must not look like a silent no-op.
        _alert("The update could not be started.", _MB_ICONERROR)


def _fetch_manifest() -> dict:
    """Fetch, validate, and cryptographically verify the manifest. The
    ed25519 signature is checked here — before any caller trusts `version` to
    decide "up to date" or to prompt — so a CDN/bucket compromise can't forge
    a version to suppress or fake an update."""
    with _urlopen(_MANIFEST_URL, _FETCH_TIMEOUT_S) as resp:
        raw = resp.read(_MAX_MANIFEST_BYTES + 1)
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError("update manifest is too large")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or not all(
        isinstance(manifest.get(key), str)
        for key in ("version", "url", "sha256", "signature")
    ):
        raise ValueError("malformed update manifest")
    _verify_signature(manifest["version"], manifest["sha256"], manifest["signature"])
    return manifest


def _verify_signature(version: str, sha256: str, signature: str) -> None:
    message = f"{_SIGNING_CONTEXT}\n{version}\n{sha256}\n".encode("utf-8")
    try:
        Ed25519PublicKey.from_public_bytes(_PUBLIC_KEY).verify(
            base64.b64decode(signature), message
        )
    except InvalidSignature as error:
        raise ValueError("update manifest signature is invalid") from error


def _is_newer(candidate: str, current: str) -> bool:
    def parts(version: str) -> tuple[int, ...]:
        return tuple(int(part) for part in version.split("."))

    return parts(candidate) > parts(current)


def _download_verified(manifest: dict) -> str:
    """Stream the installer to %TEMP% (never the supervisor's temp dir, which
    the installer's [InstallDelete] wipes) while hashing it, and confirm its
    SHA-256 matches the signed value. The manifest signature (over version +
    sha256) is already verified in _fetch_manifest; the URL is not signed, so
    require HTTPS."""
    url = manifest["url"]
    if not url.startswith("https://"):
        raise ValueError("update manifest url is not https")
    sha256 = manifest["sha256"]
    digest = hashlib.sha256()
    total = 0
    fd, path = tempfile.mkstemp(prefix=_STAGE_PREFIX, suffix=_STAGE_SUFFIX)
    ok = False
    try:
        with os.fdopen(fd, "wb") as out, _urlopen(url, _DOWNLOAD_TIMEOUT_S) as resp:
            while chunk := resp.read(_DOWNLOAD_CHUNK):
                total += len(chunk)
                if total > _MAX_INSTALLER_BYTES:
                    raise ValueError("update installer exceeds the size limit")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != sha256:
            raise ValueError("downloaded installer does not match the signed manifest")
        ok = True
        return path
    finally:
        if not ok:
            _discard(path)


def _sweep_stale_downloads() -> None:
    """Best-effort cleanup of installers a previous session staged but never
    installed (declined, or the process died). Guarded by _check_lock so it
    can't delete a file a concurrent manual check just staged and is waiting to
    launch; if a check holds the lock, skip — the stale file waits one more
    session. A file the running installer holds open won't delete anyway."""
    if not _check_lock.acquire(blocking=False):
        return
    try:
        for stale in glob.glob(os.path.join(tempfile.gettempdir(), f"{_STAGE_PREFIX}*{_STAGE_SUFFIX}")):
            _discard(stale)
    finally:
        _check_lock.release()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_DOWNLOAD_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _alert(text: str, icon: int) -> None:
    ctypes.windll.user32.MessageBoxW(0, text, "FusedRender", _MB_OK | icon)


def _prompt_install(version: str) -> int:
    return ctypes.windll.user32.MessageBoxW(
        0,
        f"FusedRender {version} is ready to install.\n\n"
        "Install now? FusedRender will restart to finish.",
        "FusedRender update",
        _MB_YESNO | _MB_ICONINFORMATION | _MB_SETFOREGROUND,
    )
