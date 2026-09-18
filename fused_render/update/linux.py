"""Linux in-app updater (AppImage) (docs/PYTHON_SUPERVISOR_SPEC.md "Software
updates" gives the Windows design this mirrors). A silent background loop
checks the signed manifest and surfaces a newer version only through
/api/config's `update` field — the shell shows a badge. Downloading and
installing happen solely on an explicit POST /api/update/install.

The state machine, the throttle, the Activity-dock job mirroring, the cancel
flag, and the shared constants all live in `update/manager.py`
(`UpdateManager`) — shared with the macOS DMG updater (`update/mac.py`). This
module supplies only what is actually Linux-specific: resolving the running
AppImage's path, and the AppImage download + swap.

ONE install path, same as mac: download the signed AppImage next to the
current one, verify, `os.chmod` it executable, then `os.replace()` it onto
the running file. The running process keeps its already-open inode (same
guarantee the mac bundle rename and the Windows installer swap both rely on),
so the swap is safe to do under a live server. `$APPIMAGE`'s path must stay
byte-identical across the swap — `_linux/startup.py` writes `.desktop`
`Exec=` lines and an autostart entry that both point at it, and renaming the
file out from under them would strand both — so the swap always lands the
new bytes at the SAME path, never a new filename.

There is no equivalent of mac's `_verify_app_version`: an AppImage has
nothing cheap to read out of it to confirm the version inside (unlike a DMG's
mounted Info.plist), and the manifest's signature already binds the claimed
version to the exact bytes at its sha256 — a mismatched version there would
already have failed `download_verified`'s checksum compare.

An install also mirrors itself into the Activity dock as a server-owned job
(`sys:update:<version>`), exactly as mac's does — see manager.py's module
docstring for the shape and the cancel contract (honoured only while
DOWNLOADING; once the swap starts there is no safe point to stop at).

Everything runs on worker threads and never raises out of the manager: a
failed check leaves state "idle"/"error", never a dead loop. Job reporting is
best-effort on top of that — an install must never fail because its row could
not be drawn.
"""
from __future__ import annotations

import logging
import os
import threading

from fused_render import __version__, installed, jobs
from fused_render.supervisor._linux import startup
from fused_render.update import common
from fused_render.update import manager as _base

logger = logging.getLogger("fused_render.update")

# Overridable for staging/E2E tests (point a test build at a test manifest).
# Safe to expose: the manifest must still verify against the pinned ed25519
# key, so redirecting the URL alone cannot feed the updater different bytes.
MANIFEST_URL = os.environ.get(
    "FUSED_RENDER_UPDATE_MANIFEST_URL",
    "https://d2ic19jpchjovp.cloudfront.net/fused-render-linux/latest.json")

# Re-exported from update/manager.py so `linux.<name>` keeps resolving for
# every caller and every test that patches it — see mac.py's identical block
# for the full rationale (UpdateManager._const() prefers the concrete
# subclass's own module over manager.py's).
JOB_PREFIX = _base.JOB_PREFIX
PHASE_DOWNLOADING = _base.PHASE_DOWNLOADING
PHASE_INSTALLING = _base.PHASE_INSTALLING
INSTALL_HEARTBEAT_S = _base.INSTALL_HEARTBEAT_S
DEV_MANAGER_ENV = _base.DEV_MANAGER_ENV
MIN_CHECK_GAP_S = _base.MIN_CHECK_GAP_S
FAILED_CHECK_GAP_S = _base.FAILED_CHECK_GAP_S
DONE_MESSAGE = _base.DONE_MESSAGE
CANCELLED_MESSAGE = _base.CANCELLED_MESSAGE
_DISK_SPACE_FACTOR = _base._DISK_SPACE_FACTOR
# The first check runs right after boot; kept separate from mac's
# MAC_STARTUP_DELAY_S (own module, own patchability) even though the two
# currently agree on the same number.
LINUX_STARTUP_DELAY_S = 1.0
_DOWNLOAD_PREFIX = "FusedRender-"
_DOWNLOAD_SUFFIX = ".AppImage"


class UpdateManager(_base.UpdateManager):
    """State machine behind /api/config's `update` field, specialised for the
    Linux AppImage swap (see the module docstring). Kept as `UpdateManager` in
    THIS module's own namespace, not `LinuxUpdateManager`, mirroring mac.py:
    nothing outside this module needs the shared base class's own name from
    here, and `linux.UpdateManager()`/`linux.start()`/`linux.manager()` reads
    exactly like `mac.UpdateManager()`/`mac.start()`/`mac.manager()` for the
    platform dispatch in update/__init__.py.

    states: idle -> checking -> (idle | available) -> installing(progress)
            -> installed | error(message)
    "installed" means the AppImage on disk is the new version; the existing
    installed_version drift banner (fused_render/installed.py's Linux stamp
    read) drives the restart from there."""

    _DOWNLOAD_PREFIX = _DOWNLOAD_PREFIX
    _DOWNLOAD_SUFFIX = _DOWNLOAD_SUFFIX

    @property
    def _STARTUP_DELAY_S(self) -> float:
        # A property, not a plain class attribute — see mac.py's identical
        # override for why: start_auto_checks() (shared base) reads
        # self._STARTUP_DELAY_S on every call, and a test patching the module
        # global `linux.LINUX_STARTUP_DELAY_S` after this class is already
        # defined must still be seen.
        return LINUX_STARTUP_DELAY_S

    def __init__(self, *, manifest_url: str = MANIFEST_URL, bundle: str | None = None,
                 method: str | None = None, check_only: bool = False):
        if bundle is None:
            appimage = startup.appimage_path()
            bundle = str(appimage) if appimage is not None else None
        super().__init__(manifest_url=manifest_url, bundle=bundle,
                         method=method, check_only=check_only)

    # -- status ---------------------------------------------------------------

    def method(self) -> str:
        if self._method is None:
            self._method = "appimage" if self._bundle is not None else "none"
        return self._method

    def _disk_version(self) -> str | None:
        """The version installed.py's Linux stamp says is on disk — the same
        read installed.installed_version() does, against this manager's own
        target path so tests can point it at a fixture. There is nothing
        cheap to read out of an AppImage itself (no mounted Info.plist to
        open, unlike mac); the manifest signature already binds version to
        bytes, so the stamp — written right after a swap this process itself
        performed and verified — is the only signal available."""
        return installed._linux_installed_version(self._bundle)

    # -- installing -----------------------------------------------------------

    def _install_artifact(self, manifest: dict) -> None:
        if self._bundle is None:
            raise RuntimeError("not running from an AppImage")
        self._install_appimage(manifest)

    def _updates_dir(self) -> str:
        # Downloads land in the AppImage's OWN parent directory, deliberately
        # (see _install_appimage): os.replace() is only atomic within one
        # filesystem, and the parent is the one place guaranteed to share it
        # with the target. Overriding the base's macOS-hardcoded path also
        # keeps the shared _sweep_stale_downloads() (manager.py) sweeping the
        # same directory installs actually use, instead of an unrelated,
        # never-populated macOS path.
        if self._bundle is not None:
            return os.path.dirname(self._bundle)
        # The check-only dev manager (DEV_MANAGER_ENV) has no AppImage at
        # all — nothing to swap, nothing to sweep, but _check_disk_space
        # still needs a real directory to statvfs.
        from fused_render.shell.storage import home_dir
        path = os.path.join(home_dir(), "updates")
        os.makedirs(path, exist_ok=True)
        return path

    def _install_appimage(self, manifest: dict) -> None:
        if self._bundle is None:
            raise RuntimeError("not running from an AppImage")
        appimage = self._bundle
        parent = os.path.dirname(appimage)
        # The common Linux case: an AppImage in /opt or on a read-only mount.
        if not os.access(parent, os.W_OK):
            raise RuntimeError(
                f"cannot write to {parent} — update by downloading the "
                "AppImage manually")

        updates = self._updates_dir()
        self._check_disk_space(updates)

        # The manifest itself carries no size field (schema 1); the total
        # comes from the download response's Content-Length instead, so the
        # UI can show a percentage when the CDN sends that header.
        def on_bytes(done: int, size: int | None) -> None:
            total = float(size) if size is not None else None
            with self._lock:
                self._progress = float(done)
                self._progress_total = total
            self._job_report(done=float(done), total=total,
                             detail=PHASE_DOWNLOADING, cancellable=True)

        downloaded = common.download_verified(
            manifest, dir=updates, prefix=_DOWNLOAD_PREFIX,
            suffix=_DOWNLOAD_SUFFIX, progress=on_bytes,
            should_abort=self._cancel_requested)
        # A ✕ learned on the LAST byte tick has no next chunk to be honoured
        # on (same race mac's _install_dmg guards against): the download is
        # complete but the AppImage is untouched, which is still the point
        # where cancelling costs nothing.
        self._job_report()
        if self._cancel_requested():
            common.discard(downloaded)
            raise common.UpdateCancelled("cancelled after download")
        # Downloading is over: from here on the AppImage is being replaced,
        # and there is no point between chmod and os.replace() where
        # stopping would leave anything better than finishing does — so the
        # row drops its Cancel and its numbers and says what it is doing
        # instead.
        with self._lock:
            self._phase = "installing"
        self._job_report(detail=PHASE_INSTALLING, message="", done=None,
                         total=None, cancellable=False)
        # …and it has to keep saying it: the swap itself reports no progress,
        # but a silent running row goes stale (manager.py's INSTALL_HEARTBEAT_S
        # / jobs.py's STALE_AFTER_S), same as mac's.
        beat_stop = threading.Event()
        beat = threading.Thread(target=self._beat_installing, args=(beat_stop,),
                                daemon=True,
                                name="fused-render-update-heartbeat")
        beat.start()
        try:
            os.chmod(downloaded, 0o755)
            # Atomic within `parent` (same filesystem as `downloaded`, by
            # construction): the running process keeps its already-open
            # inode on `appimage`, so the swap under a live process is safe.
            os.replace(downloaded, appimage)
            # Best-effort: the swap itself is what matters. A stamp write
            # that fails only costs a later "restart to finish" banner —
            # never re-raised into an "error" state for an install that, on
            # disk, already succeeded.
            try:
                installed.write_linux_stamp(appimage, manifest["version"])
            except OSError:
                logger.exception(
                    "update installed but the version stamp could not be "
                    "written — no restart banner will show for it")
        finally:
            # A no-op once os.replace() has moved `downloaded` onto
            # `appimage` — discard() swallows the resulting OSError — but
            # still the right cleanup on any failure before that point.
            common.discard(downloaded)
            # Last, so the row is kept alive through the chmod/replace/stamp
            # above — _install()'s terminal report comes next, and only once
            # the beat has actually stopped: a beat mid-report could
            # otherwise overwrite the finished row with "Installing".
            beat_stop.set()
            beat.join(timeout=INSTALL_HEARTBEAT_S + 5)


_manager: UpdateManager | None = None
_manager_lock = threading.Lock()


def manager() -> UpdateManager | None:
    """The process-wide manager, or None when start() was never called (dev
    server, CLI, non-Linux packages) — /api/config omits `update` then."""
    return _manager


def start() -> UpdateManager | None:
    """Create the singleton and start its background checks. Called once from
    the Linux server's bootstrap; idempotent. No-op (returns None) when not
    running from an AppImage — an unpackaged dev run has nothing to swap, so
    it gets no badge at all rather than an install that can only fail."""
    global _manager
    with _manager_lock:
        if _manager is None:
            appimage = startup.appimage_path()
            if appimage is None:
                # ...unless a dev run asked for a manager that only looks
                # (DEV_MANAGER_ENV, see manager.py): same loop, same
                # throttle, same states, no swap.
                if not os.environ.get(DEV_MANAGER_ENV):
                    return None
                _manager = UpdateManager(bundle=None, method="none", check_only=True)
            else:
                _manager = UpdateManager()
            _manager.start_auto_checks()
        return _manager
