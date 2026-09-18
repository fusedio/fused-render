"""macOS in-app updater (docs/PYTHON_SUPERVISOR_SPEC.md "Software updates"
gives the Windows design this mirrors). A silent background loop checks the
signed manifest and surfaces a newer version only through /api/config's
`update` field — the shell shows a badge. Downloading and installing happen
solely on an explicit POST /api/update/install.

The state machine, the throttle, the Activity-dock job mirroring, the cancel
flag, and the shared constants all live in `update/_manager.py`
(`UpdateManager`) — shared with the Linux AppImage updater (`update/linux.py`).
This module supplies only what is actually mac-specific: bundle/brew
detection, and the DMG download + swap.

ONE install path for every install type (Akshil, 2026-09-08): the DMG swap
below. `detect_method()` still reports "brew" | "dmg" | "none" and that word
still rides along on status(), but it is INFORMATIONAL only — it changes
nothing about what an install does, and nothing in the UI mentions Homebrew.

- "brew": the running bundle is Homebrew-managed. It gets the same download,
  the same version-verified bundle replacement, and no brew command of any
  kind: THE APP NEVER INVOKES BREW ON ITSELF. It cannot — the tap's cask
  (fusedio/homebrew-tap) carries `uninstall quit:`, so a `brew upgrade`
  started from inside the app would quit the app mid-upgrade, which is
  exactly why running brew is not ours to do and not ours to suggest.
  Homebrew's receipt lag is the accepted cost, stated plainly: after an
  in-app swap brew's bookkeeping (Caskroom metadata, `brew list --versions`)
  still names the OLD version, and a later `brew upgrade` reinstalls the same
  version over ours (quitting the app as it goes, per that same `uninstall
  quit:`) — a redundant reinstall, never a broken one. The next check() tick
  reads the bundle on disk either way and flips to "installed" once a newer
  bundle has landed, ours or brew's.
- "dmg": download the signed DMG, verify, and swap the .app bundle in place.
  Replacing the bundle under a running process is the SUPPORTED existing flow
  (a manual DMG drag does exactly this): installed.installed_version() then
  drifts from __version__, ServerStatusBanner raises the restart dialog, and
  fused-render://relaunch (app.begin_relaunch) respawns from disk. That
  relaunch guard REQUIRES the drift, which is why the swap happens on install
  rather than being deferred to quit.

An install also mirrors itself into the Activity dock as a server-owned job
(`sys:update:<version>`, the same bridge shape `server/routers/index.py`'s
`_mirror_one_run_job` uses for a rescan): the dock is where every other
long-running thing in the app already lives, and it is the surface that can
show bytes, a phase, and a Cancel without the sidebar badge growing a second
progress readout. Cancel arrives the way every job cancel does — as a flag on
the reply to the progress tick this manager was going to send anyway — and is
honoured only while DOWNLOADING; once the bundle swap starts there is no safe
point to stop at.

Everything runs on worker threads and never raises out of the manager: a
failed check leaves state "idle"/"error", never a dead loop. Job reporting is
best-effort on top of that — an install must never fail because its row could
not be drawn.
"""
from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from fused_render import __version__, jobs
from fused_render.update import common
# The shared state machine lives in its own module, `_manager.py`, named
# with a leading underscore (not `manager.py`) precisely so it cannot collide
# with `fused_render.update`'s own package-level `manager()` dispatch
# function (Task 4): Python's import machinery stamps every submodule onto
# its parent package's namespace under the submodule's own name as a side
# effect of import, from anywhere, regardless of import style — so a
# submodule literally named `manager` would eventually clobber the
# `manager()` function at that same attribute slot.
from fused_render.update import _manager as _base

logger = logging.getLogger("fused_render.update")

# Overridable for staging/E2E tests (point a test build at a test manifest).
# Safe to expose: the manifest must still verify against the pinned ed25519
# key, so redirecting the URL alone cannot feed the updater different bytes.
MANIFEST_URL = os.environ.get(
    "FUSED_RENDER_UPDATE_MANIFEST_URL",
    "https://d2ic19jpchjovp.cloudfront.net/fused-render-macos/latest.json")
CASK_NAME = "fused-render"
# GUI apps launch with a bare PATH, so brew is probed at its two fixed homes
# (Apple Silicon, then Intel) rather than through the environment.
BREW_PATHS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")

# Re-exported from update/_manager.py so `mac.<name>` keeps resolving for every
# caller and every test that patches it — the shared state machine (check(),
# install(), _job_report(), _beat_installing(), ...) lives there now, but it
# reads every one of these back out through `UpdateManager._const()`, which
# prefers whatever the CONCRETE subclass's own module (this one) currently
# has bound to the name. A `monkeypatch.setattr(mac, "PHASE_DOWNLOADING",
# ...)` therefore still changes what a running install reports, even though
# the code doing the reporting is defined in _manager.py, not here.
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
# The first check runs right after boot, so the sidebar badge (UpdateBadge,
# 60s idle poll on top of this) appears on the first poll rather than a minute
# or two into the session. Not `common.STARTUP_DELAY_S`, which the Windows tray
# updater also drives and wants to keep clear of a whole app launch; here the
# check loop is a background thread, so it is off the startup path anyway and
# the delay only keeps the manifest fetch out of a booting process's first
# tick. Every check after it is common.CHECK_INTERVAL_S (5 min) apart.
MAC_STARTUP_DELAY_S = 1.0
_DOWNLOAD_PREFIX = "FusedRender-"
_DOWNLOAD_SUFFIX = ".dmg"


def bundle_path() -> str | None:
    """The .app bundle root when running packaged, None otherwise (same
    anatomy as app.bundle_path; duplicated here so this module never imports
    app.py, which needs AppKit)."""
    if getattr(sys, "frozen", None) != "macosx_app":
        return None
    contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(contents)


def find_brew() -> str | None:
    for path in BREW_PATHS:
        if os.access(path, os.X_OK):
            return path
    return None


def detect_method(bundle: str | None, *, brew: str | None = None,
                  run=subprocess.run) -> str:
    """"brew" | "dmg" | "none". Brew-managed means `brew list --cask` knows
    the cask AND its app artifact is the very bundle we are running — a user
    with stale brew history but a drag-installed copy elsewhere must get the
    DMG path, not a brew upgrade that replaces a different install."""
    if bundle is None:
        return "none"
    if brew is None:
        brew = find_brew()
    if brew is None:
        return "dmg"
    try:
        listed = run([brew, "list", "--cask", CASK_NAME],
                     capture_output=True, text=True, encoding="utf-8", errors="replace",
                     timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "dmg"
    if listed.returncode != 0:
        return "dmg"
    bundle_real = os.path.realpath(bundle)
    for line in listed.stdout.splitlines():
        candidate = line.strip().split(" -> ")[0].strip()
        if candidate.endswith(".app") and os.path.realpath(candidate) == bundle_real:
            return "brew"
    # The cask's `app` artifact moves the bundle to /Applications; `brew list`
    # output shapes vary across brew versions, so accept the conventional
    # target too when the listing didn't name the bundle directly.
    if bundle_real == os.path.realpath("/Applications/FusedRender.app"):
        return "brew"
    return "dmg"


def _discard_old_bundle(old: str) -> None:
    """Best-effort removal of the bundle the swap renamed away."""
    try:
        shutil.rmtree(old, ignore_errors=True)
    except OSError:
        logger.debug("could not remove old bundle %s", old, exc_info=True)


class UpdateManager(_base.UpdateManager):
    """State machine behind /api/config's `update` field, specialised for the
    macOS .app bundle + DMG install (see the module docstring). Kept as
    `UpdateManager` in THIS module's own namespace, not `MacUpdateManager`:
    every existing caller and every test constructs it as `mac.UpdateManager(
    ...)`, and there is nothing outside this module that needs the shared
    base class's own name from here — importing it under its own name
    (`_base.UpdateManager`) and subclassing under this one keeps that
    surface unchanged.

    states: idle -> checking -> (idle | available) -> installing(progress)
            -> installed | error(message)
    "installed" means the bundle on disk is the new version; the existing
    installed_version drift banner drives the restart from there.
    Every method takes the same route through those states: the DMG swap is
    the one install path, and brew is never invoked — the cask's
    `uninstall quit:` would quit the app mid-upgrade, so an app that ran brew
    on itself would be killing itself to finish an install (see the module
    docstring). There is therefore no terminal command to offer on any state,
    for any method: status()'s `manual_command` is always None."""

    _DOWNLOAD_PREFIX = _DOWNLOAD_PREFIX
    _DOWNLOAD_SUFFIX = _DOWNLOAD_SUFFIX

    @property
    def _STARTUP_DELAY_S(self) -> float:
        # A property, not a plain class attribute: tests patch the module
        # global `mac.MAC_STARTUP_DELAY_S` at runtime (after this class is
        # already defined), and start_auto_checks() — which lives in the
        # shared base class — reads `self._STARTUP_DELAY_S` on every call, so
        # this has to re-read the module's current value each time rather
        # than freeze whatever it was at class-body evaluation.
        return MAC_STARTUP_DELAY_S

    def __init__(self, *, manifest_url: str = MANIFEST_URL, bundle: str | None = None,
                 method: str | None = None, check_only: bool = False):
        super().__init__(
            manifest_url=manifest_url,
            bundle=bundle if bundle is not None else bundle_path(),
            method=method, check_only=check_only)

    # -- status ---------------------------------------------------------------

    def method(self) -> str:
        if self._method is None:
            self._method = detect_method(self._bundle)
        return self._method

    def _disk_version(self) -> str | None:
        """CFBundleShortVersionString of the bundle on disk — what would launch
        next time (same read as installed.installed_version, but against this
        manager's bundle path so tests can point it at a fixture)."""
        if self._bundle is None:
            return None
        try:
            with open(os.path.join(self._bundle, "Contents", "Info.plist"), "rb") as f:
                return plistlib.load(f).get("CFBundleShortVersionString")
        except (OSError, plistlib.InvalidFileException):
            return None

    # -- installing -----------------------------------------------------------

    def _install_artifact(self, manifest: dict) -> None:
        # ONE install path for every install type (D767): nothing branches
        # on `method()` any more, so the only real precondition left is
        # having a bundle to swap. (`_install_dmg` re-checks it — it is
        # also reachable on its own.)
        if self._bundle is None:
            raise RuntimeError("not running from an installed bundle")
        self._install_dmg(manifest)

    # -- dmg path -------------------------------------------------------------

    def _install_dmg(self, manifest: dict) -> None:
        if self._bundle is None:
            raise RuntimeError("not running from an installed bundle")
        # realpath, not the stored path: `bundle_path()` only abspaths, so a
        # bundle reached through a symlink (a /Applications/FusedRender.app
        # link into a Caskroom artifact, say) would otherwise have its LINK
        # renamed by the swap below — leaving the real bundle untouched and the
        # link pointing at a name that no longer exists.
        bundle = os.path.realpath(self._bundle)
        parent = os.path.dirname(bundle)
        # For a Homebrew cask installed as an artifact rather than a copy, that
        # resolved parent is `…/Caskroom/fused-render/<oldversion>/`. It is
        # writable, so the swap lands there and brew's receipt goes on pointing
        # at a directory whose contents are now the NEW version — the accepted
        # receipt lag (see the module docstring): the app on disk is simply
        # newer than `brew list --versions` says until brew next runs, and the
        # app does not run brew to fix that.
        if not os.access(parent, os.W_OK):
            raise RuntimeError(
                f"cannot write to {parent} — update by downloading the DMG manually")

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
            # One report per 1MB chunk — the same tick that carries the bytes
            # up is the one that carries a pending cancel back down, so the ✕
            # is honoured within a chunk of being pressed.
            self._job_report(done=float(done), total=total,
                             detail=PHASE_DOWNLOADING, cancellable=True)

        dmg = common.download_verified(
            manifest, dir=updates, prefix=_DOWNLOAD_PREFIX,
            suffix=_DOWNLOAD_SUFFIX, progress=on_bytes,
            should_abort=self._cancel_requested)
        # A ✕ learned on the LAST byte tick has no next chunk to be honoured
        # on (bugbot, PR #1058): `should_abort` runs before each chunk, so a
        # cancel that arrived with the final one lands here, after the loop.
        # The download is complete but the bundle is untouched, which is still
        # the point where cancelling costs nothing — so it is honoured, and the
        # DMG goes the way a mid-stream partial would.
        #
        # One more read of the flag before that check, because the last tick is
        # not the last moment: `download_verified` hashes the whole file after
        # the final chunk, which on a 300MB DMG is seconds during which the row
        # still shows a live ✕ and no tick is left to carry the answer back. An
        # id-only upsert is a legal report on an existing row — it changes
        # nothing and returns the record, `cancel_requested` included.
        self._job_report()
        if self._cancel_requested():
            common.discard(dmg)
            raise common.UpdateCancelled("cancelled after download")
        # Downloading is over: from here on the bundle is being replaced, and
        # there is no point between the ditto and the two renames where
        # stopping would leave anything better than finishing does — so the
        # row drops its Cancel and its numbers (no honest total exists for a
        # copy-and-swap) and says what it is doing instead.
        with self._lock:
            self._phase = "installing"
        self._job_report(detail=PHASE_INSTALLING, message="", done=None,
                         total=None, cancellable=False)
        # …and it has to keep saying it: the swap reports no progress, but a
        # silent running row is "No longer reporting" after 30s and gone after
        # ten minutes (see `INSTALL_HEARTBEAT_S`), which would take the final
        # `done` upsert with it. The watchdog re-sends the same phase until the
        # swap and its cleanup are over, and is stopped from the `finally`
        # below on every path out — including the ones that raise.
        beat_stop = threading.Event()
        beat = threading.Thread(target=self._beat_installing, args=(beat_stop,),
                                daemon=True,
                                name="fused-render-update-heartbeat")
        beat.start()
        mount = None
        old = None
        swap_in = os.path.join(parent, ".FusedRender-update.app")
        try:
            mount = self._attach(dmg)
            source = self._find_app(mount)
            self._verify_app_version(source, manifest["version"])
            if os.path.exists(swap_in):
                shutil.rmtree(swap_in)
            # ditto straight from the mounted image into the bundle's own
            # parent dir: it preserves the code signature, resource forks and
            # xattrs a plain copy can drop (a stripped signature would leave a
            # bundle Gatekeeper refuses to launch), and landing on the target
            # volume makes the swap below two same-volume renames.
            subprocess.run(["/usr/bin/ditto", source, swap_in],
                           check=True, capture_output=True, timeout=600)
            # The swap: both renames happen inside `parent` so each is atomic
            # on the volume; the running process keeps its open files on the
            # old inode (same situation as a manual DMG drag / brew upgrade).
            old = os.path.join(parent, f".FusedRender-old-{os.getpid()}.app")
            os.rename(bundle, old)
            try:
                os.rename(swap_in, bundle)
            except OSError:
                os.rename(old, bundle)  # roll back — never leave no app at all
                raise
        finally:
            if mount is not None:
                subprocess.run(["/usr/bin/hdiutil", "detach", mount, "-quiet"],
                               check=False, capture_output=True, timeout=60)
            common.discard(dmg)
            if os.path.exists(swap_in):
                shutil.rmtree(swap_in, ignore_errors=True)
            # Last, so the row is still being kept alive through the detach and
            # the cleanup above — `_install`'s terminal report comes next, and
            # only once the beat has actually stopped: a beat mid-report could
            # otherwise overwrite the finished row with "Installing".
            beat_stop.set()
            beat.join(timeout=INSTALL_HEARTBEAT_S + 5)
        # Old bundle: best-effort removal on a worker; open files keep working
        # on the unlinked inodes until this process exits. `old` is a name this
        # function just renamed a realpath'd `bundle` to, so it is a real
        # directory, never a symlink — `rmtree` is the whole story.
        if old is not None:
            threading.Thread(target=_discard_old_bundle, args=(old,),
                             daemon=True).start()

    def _attach(self, dmg: str) -> str:
        result = subprocess.run(
            ["/usr/bin/hdiutil", "attach", dmg, "-nobrowse", "-readonly",
             "-plist", "-mountrandom", tempfile.gettempdir()],
            capture_output=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError("could not open the downloaded update image")
        for entity in plistlib.loads(result.stdout).get("system-entities", []):
            if entity.get("mount-point"):
                return entity["mount-point"]
        raise RuntimeError("update image mounted with no volume")

    def _find_app(self, mount: str) -> str:
        for name in sorted(os.listdir(mount)):
            if name.endswith(".app"):
                return os.path.join(mount, name)
        raise RuntimeError("update image contains no app bundle")

    def _verify_app_version(self, app: str, version: str) -> None:
        """The DMG's integrity is already pinned by the signed sha256; this
        guards against a mispublished manifest (right signature, wrong file)
        swapping in an unexpected version."""
        try:
            with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
                found = plistlib.load(f).get("CFBundleShortVersionString")
        except (OSError, plistlib.InvalidFileException) as error:
            raise RuntimeError("update app bundle has no readable Info.plist") from error
        if found != version:
            raise RuntimeError(
                f"update image contains version {found}, expected {version}")


_manager: UpdateManager | None = None
_manager_lock = threading.Lock()


def manager() -> UpdateManager | None:
    """The process-wide manager, or None when start() was never called (dev
    server, CLI, non-mac packages) — /api/config omits `update` then."""
    return _manager


def start() -> UpdateManager | None:
    """Create the singleton and start its background checks. Called once from
    the mac app's server bootstrap; idempotent. No-op (returns None) when not
    running from a bundle — an unpackaged dev run has nothing to swap, so it
    gets no badge at all rather than an install that can only fail."""
    global _manager
    with _manager_lock:
        if _manager is None:
            if bundle_path() is None:
                # ...unless a dev run asked for a manager that only looks
                # (DEV_MANAGER_ENV, above): same loop, same throttle, same
                # states, no swap.
                if not os.environ.get(DEV_MANAGER_ENV):
                    return None
                _manager = UpdateManager(bundle=None, method="none", check_only=True)
            else:
                _manager = UpdateManager()
            _manager.start_auto_checks()
        return _manager
