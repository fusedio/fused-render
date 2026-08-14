"""Update check (docs/PYTHON_SUPERVISOR_SPEC.md, "Software updates"). A silent
background loop checks for a newer version and, when one exists, surfaces it
only by relabeling the tray item via a `notify` callback — it never downloads
or prompts on its own. Downloading and installing happen solely when the user
clicks the tray item and approves the prompt. Runs on worker threads; never
raises, so a failed check can't tear down the Job-owned server."""
from __future__ import annotations

import ctypes
import glob
import http.client
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request

from fused_render import __version__
from fused_render.supervisor.paths import DesktopPaths
from fused_render.update import common as _common

_MANIFEST_URL = "https://d2ic19jpchjovp.cloudfront.net/fused-render-windows/latest.json"
# Re-exported from update/common so tests can patch them on THIS module (the
# thin wrappers below read the module globals at call time).
_PUBLIC_KEY = _common.PUBLIC_KEY
_SIGNING_CONTEXT = _common.SIGNING_CONTEXT
_STARTUP_DELAY_S = _common.STARTUP_DELAY_S
_CHECK_INTERVAL_S = _common.CHECK_INTERVAL_S
_MAX_INSTALLER_BYTES = _common.MAX_ARTIFACT_BYTES
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


# Test seam: tests patch this to stub network I/O; the wrappers below pass it
# through to update/common per call.
_urlopen = _common.urlopen
_HttpsOnlyRedirect = _common.HttpsOnlyRedirect


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
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
            lpFile=installer,
            lpDirectory=os.path.dirname(installer),
            nShow=1,
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
    """Fetch, validate, and cryptographically verify the manifest (see
    update/common.fetch_manifest). Module globals are passed per call so tests
    can patch `_urlopen`/`_PUBLIC_KEY` on this module."""
    return _common.fetch_manifest(_MANIFEST_URL, urlopen_fn=_urlopen,
                                  public_key=_PUBLIC_KEY)


def _is_newer(candidate: str, current: str) -> bool:
    return _common.is_newer(candidate, current)


def _download_verified(manifest: dict) -> str:
    """Stream the installer to %TEMP% (never the supervisor's temp dir, which
    the installer's [InstallDelete] wipes) while hashing it, and confirm its
    SHA-256 matches the signed value (see update/common.download_verified)."""
    return _common.download_verified(
        manifest, prefix=_STAGE_PREFIX, suffix=_STAGE_SUFFIX,
        max_bytes=_MAX_INSTALLER_BYTES, urlopen_fn=_urlopen,
    )


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
    _common.discard(path)


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
