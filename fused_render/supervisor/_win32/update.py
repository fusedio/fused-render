"""Update check (docs/PYTHON_SUPERVISOR_SPEC.md, "Software updates"). A silent
background loop checks for a newer version and, when one exists, surfaces it
only by relabeling the tray item via a `notify` callback — it never downloads
or prompts on its own. Downloading and installing happen solely when the user
clicks the tray item and approves the prompt. Runs on worker threads; never
raises, so a failed check can't tear down the Job-owned server."""
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
from fused_render.supervisor.paths import DesktopPaths

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

# One check at a time (auto vs. manual, or two auto ticks). Only touched while
# _check_lock is held.
_check_lock = threading.Lock()
# Set once an installer has been launched. The app stays up until the Inno
# wizard finishes and --shutdown-for-upgrade fires; this latch stops a check in
# that window from launching a second setup. Only touched under _check_lock.
_install_launched = False


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


def start_auto_checks(paths: DesktopPaths, notify) -> None:
    """Spawn the background check loop: after a startup delay (so it never
    competes with launch), check now and every _CHECK_INTERVAL_S. Silent — a
    newer version is surfaced only by `notify(version)` relabeling the tray;
    set FUSED_RENDER_NO_AUTO_UPDATE to a non-empty value to disable it
    entirely."""
    if os.environ.get("FUSED_RENDER_NO_AUTO_UPDATE"):
        return

    def loop():
        time.sleep(_STARTUP_DELAY_S)
        swept = False
        while True:
            try:
                swept = swept or _sweep_stale_downloads()
                _auto_check(paths, notify)
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
        if _install_launched:
            _alert("An update has already been started. Restart FusedRender to check again.",
                   _MB_ICONINFORMATION)
            return
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
        _offer_install(paths, manifest)
    finally:
        _check_lock.release()


def _auto_check(paths: DesktopPaths, notify) -> None:
    """Background tick: silent on "no update" and on transient errors (logged
    only, never a dialog). A newer version is surfaced only through `notify`,
    which relabels the tray item — nothing is downloaded or prompted here."""
    if not _check_lock.acquire(blocking=False):
        return
    try:
        if _install_launched:
            return
        try:
            manifest = _fetch_manifest()
            if not _is_newer(manifest["version"], __version__):
                return
        except (OSError, ValueError, http.client.HTTPException) as error:
            paths.log(f"auto update check failed: {error}")
            return
        notify(manifest["version"])
    finally:
        _check_lock.release()


def _offer_install(paths: DesktopPaths, manifest: dict) -> None:
    """Prompt, then on yes download + verify + launch the installer, whose own
    --shutdown-for-upgrade path stops and relaunches the app. Declining stages
    nothing. Call only while _check_lock is held."""
    global _install_launched
    if _prompt_install(manifest["version"]) != _IDYES:
        return
    try:
        installer = _download_verified(manifest)
    except (OSError, ValueError, http.client.HTTPException) as error:
        paths.log(f"update download failed: {error}")
        _alert("The update could not be downloaded or verified.", _MB_ICONERROR)
        return
    try:
        setup = _launch_installer(installer)
    except OSError as error:
        paths.log(f"update launch failed: {error}")
        _discard(installer)
        _alert("The update could not be started.", _MB_ICONERROR)
        return
    # Latch so no later check launches a second setup while the wizard is up;
    # _watch_setup unlatches if the wizard exits without installing.
    _install_launched = True
    _watch_setup(paths, setup, installer)


def _launch_installer(installer: str):
    """ShellExecuteEx rather than os.startfile: the same UAC-aware launch,
    but it returns a process handle so _watch_setup can see the wizard exit.
    win32 imports are deferred so the module still imports on non-Windows CI,
    and win32event is preloaded here so a broken pywin32 bundle surfaces on
    this handled path, before the wizard launches — not in the watcher."""
    try:
        import pywintypes
        import win32event  # noqa: F401 - preload for _wait_for_exit
        from win32com.shell import shell, shellcon
    except ImportError as error:
        raise OSError(str(error)) from error

    try:
        info = shell.ShellExecuteEx(
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpFile=installer, nShow=1
        )
    except pywintypes.error as error:
        raise OSError(str(error)) from error
    return info.get("hProcess")


def _watch_setup(paths: DesktopPaths, setup, installer: str) -> None:
    """Unlatch when the wizard exits without installing (cancelled, or setup
    failed), so later checks can offer the update again and the staged file
    doesn't linger in %TEMP%. A completed install never gets here: its
    --shutdown-for-upgrade stops this process before setup exits."""
    if setup is None:  # no handle to watch — keep the latch, as before
        return

    def watch():
        global _install_launched
        _wait_for_exit(setup)
        with _check_lock:
            _install_launched = False
        _discard(installer)
        paths.log("update setup exited without installing")

    threading.Thread(target=watch, daemon=True, name="fused-render-update-watch").start()


def _wait_for_exit(handle) -> None:
    import win32event

    win32event.WaitForSingleObject(handle, win32event.INFINITE)


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


def _sweep_stale_downloads() -> bool:
    """Best-effort cleanup of installers a previous session staged but never
    installed (declined, or the process died). Guarded by _check_lock so it
    can't delete a file a concurrent manual check just staged and is waiting to
    launch; if a check holds the lock, return False so the caller retries on a
    later tick. A file the running installer holds open won't delete anyway."""
    if not _check_lock.acquire(blocking=False):
        return False
    try:
        for stale in glob.glob(os.path.join(tempfile.gettempdir(), f"{_STAGE_PREFIX}*{_STAGE_SUFFIX}")):
            _discard(stale)
    finally:
        _check_lock.release()
    return True


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
        f"FusedRender {version} is available.\n\n"
        "Download and install now? FusedRender will restart to finish.",
        "FusedRender update",
        _MB_YESNO | _MB_ICONINFORMATION | _MB_SETFOREGROUND,
    )
