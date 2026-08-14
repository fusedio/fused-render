"""macOS in-app updater (docs/PYTHON_SUPERVISOR_SPEC.md "Software updates"
gives the Windows design this mirrors). A silent background loop checks the
signed manifest and surfaces a newer version only through /api/config's
`update` field — the shell shows a badge. Downloading and installing happen
solely on an explicit POST /api/update/install.

Two install methods, decided once per process:

- "brew": the running bundle is Homebrew-managed. Install delegates to
  `brew upgrade --cask fused-render` so brew's own bookkeeping (Caskroom
  metadata, `brew list`) stays truthful. On failure there is NO fallback to
  the DMG path — swapping the bundle behind brew's back would desync it
  permanently — the error surfaces the exact command for the user to run.
- "dmg": download the signed DMG, verify, and swap the .app bundle in place.
  Replacing the bundle under a running process is the SUPPORTED existing flow
  (a manual DMG drag does exactly this): installed.installed_version() then
  drifts from __version__, ServerStatusBanner shows the restart card, and
  fused-render://relaunch (app.begin_relaunch) respawns from disk. That
  relaunch guard REQUIRES the drift, which is why the swap happens on install
  rather than being deferred to quit.

Everything runs on worker threads and never raises out of the manager: a
failed check leaves state "idle"/"error", never a dead loop.
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

from fused_render import __version__
from fused_render.update import common

logger = logging.getLogger("fused_render.update")

MANIFEST_URL = "https://d2ic19jpchjovp.cloudfront.net/fused-render-macos/latest.json"
CASK_NAME = "fused-render"
# GUI apps launch with a bare PATH, so brew is probed at its two fixed homes
# (Apple Silicon, then Intel) rather than through the environment.
BREW_PATHS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
BREW_TIMEOUT_S = 15 * 60
_DOWNLOAD_PREFIX = "FusedRender-"
_DOWNLOAD_SUFFIX = ".dmg"
# Keep a margin over the DMG itself: the download and the staged .app copy
# coexist briefly during the swap.
_DISK_SPACE_FACTOR = 3


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
                     capture_output=True, text=True, timeout=30)
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


class UpdateManager:
    """State machine behind /api/config's `update` field.

    states: idle -> checking -> (idle | available) -> installing(progress)
            -> installed | error(message, manual_command?)
    "installed" means the bundle on disk is the new version; the existing
    installed_version drift banner drives the restart from there."""

    def __init__(self, *, manifest_url: str = MANIFEST_URL, bundle: str | None = None,
                 method: str | None = None):
        # RLock: the early-return paths in check()/install() read status()
        # while already holding the lock.
        self._lock = threading.RLock()
        self._manifest_url = manifest_url
        self._bundle = bundle if bundle is not None else bundle_path()
        self._method = method  # resolved lazily: brew probing costs a subprocess
        self._state = "idle"
        self._latest: dict | None = None
        self._error: str | None = None
        self._manual_command: str | None = None
        self._progress: float | None = None
        self._install_thread: threading.Thread | None = None

    # -- status ---------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "method": self.method(),
                "latest_version": self._latest["version"] if self._latest else None,
                "progress": self._progress,
                "error": self._error,
                "manual_command": self._manual_command,
            }

    def method(self) -> str:
        if self._method is None:
            self._method = detect_method(self._bundle)
        return self._method

    # -- checking -------------------------------------------------------------

    def start_auto_checks(self) -> None:
        """Background check loop (startup delay, then every CHECK_INTERVAL_S).
        Silent: a newer version only flips state to "available"; set
        FUSED_RENDER_NO_AUTO_UPDATE to a non-empty value to disable."""
        if os.environ.get("FUSED_RENDER_NO_AUTO_UPDATE"):
            return

        def loop():
            time.sleep(common.STARTUP_DELAY_S)
            self._sweep_stale_downloads()
            while True:
                try:
                    self.check()
                except Exception:  # noqa: BLE001 - a tick must never kill the loop
                    logger.exception("auto update tick failed")
                time.sleep(common.CHECK_INTERVAL_S)

        threading.Thread(target=loop, daemon=True,
                         name="fused-render-update-auto").start()

    def check(self) -> dict:
        """Fetch + verify the manifest and update state. Never touches state
        while an install is running. Returns status()."""
        with self._lock:
            if self._state == "installing":
                return self.status()
            self._state = "checking"
            self._error = None
        try:
            manifest = common.fetch_manifest(self._manifest_url)
            newer = common.is_newer(manifest["version"], __version__)
        except Exception as error:  # noqa: BLE001 - network/manifest failures are routine
            logger.info("update check failed: %s", error)
            with self._lock:
                if self._state == "checking":
                    # Keep a previously-found update visible over a later
                    # transient failure.
                    self._state = "available" if self._latest else "idle"
            return self.status()
        with self._lock:
            if self._state == "checking":
                if newer:
                    self._latest = manifest
                    self._state = "available"
                else:
                    self._latest = None
                    self._state = "idle"
        return self.status()

    # -- installing -----------------------------------------------------------

    def install(self) -> dict:
        """Kick the install on a worker thread. One at a time; re-POSTing
        while installing just reports current state. Allowed from "available"
        and from "error" (retry)."""
        with self._lock:
            if self._state == "installing":
                return self.status()
            if self._latest is None or self._state not in ("available", "error"):
                return self.status()
            manifest = self._latest
            self._state = "installing"
            self._error = None
            self._manual_command = None
            self._progress = 0.0
            thread = threading.Thread(
                target=self._install, args=(manifest,), daemon=True,
                name="fused-render-update-install")
            self._install_thread = thread
        thread.start()
        return self.status()

    def _install(self, manifest: dict) -> None:
        try:
            if self.method() == "brew":
                self._install_brew()
            elif self.method() == "dmg":
                self._install_dmg(manifest)
            else:
                raise RuntimeError("not running from an installed bundle")
        except Exception as error:  # noqa: BLE001 - reported through state, never raised
            logger.exception("update install failed")
            with self._lock:
                self._state = "error"
                self._error = str(error)
            return
        with self._lock:
            self._state = "installed"
            self._progress = None

    # -- brew path ------------------------------------------------------------

    def _install_brew(self) -> None:
        brew = find_brew()
        command = f"brew upgrade --cask {CASK_NAME}"
        if brew is None:
            with self._lock:
                self._manual_command = command
            raise RuntimeError("Homebrew was not found")
        try:
            result = subprocess.run(
                [brew, "upgrade", "--cask", CASK_NAME],
                capture_output=True, text=True, timeout=BREW_TIMEOUT_S,
                env={**os.environ, "HOMEBREW_NO_ENV_HINTS": "1"},
            )
        except subprocess.TimeoutExpired as error:
            with self._lock:
                self._manual_command = command
            raise RuntimeError("brew upgrade timed out") from error
        if result.returncode != 0:
            # No DMG fallback — a brew-managed install must stay brew-managed.
            # Surface the exact command so the user can run it themselves.
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
            with self._lock:
                self._manual_command = command
            raise RuntimeError("brew upgrade failed: " + " / ".join(tail))

    # -- dmg path -------------------------------------------------------------

    def _updates_dir(self) -> str:
        path = os.path.expanduser("~/Library/Application Support/fused-render/updates")
        os.makedirs(path, exist_ok=True)
        return path

    def _sweep_stale_downloads(self) -> None:
        """Best-effort cleanup of DMGs and staged bundles a previous session
        left behind (install failed, or the process died mid-download)."""
        try:
            updates = self._updates_dir()
            for name in os.listdir(updates):
                full = os.path.join(updates, name)
                try:
                    if os.path.isdir(full):
                        shutil.rmtree(full)
                    else:
                        os.unlink(full)
                except OSError:
                    pass
        except OSError:
            pass

    def _install_dmg(self, manifest: dict) -> None:
        bundle = self._bundle
        if bundle is None:
            raise RuntimeError("not running from an installed bundle")
        parent = os.path.dirname(bundle)
        if not os.access(parent, os.W_OK):
            raise RuntimeError(
                f"cannot write to {parent} — update by downloading the DMG manually")

        updates = self._updates_dir()
        self._check_disk_space(updates)

        # Manifest carries no size field (schema 1), so progress is a raw byte
        # count; the UI shows MB downloaded, not a percentage.
        def on_bytes(done: int) -> None:
            with self._lock:
                self._progress = float(done)

        dmg = common.download_verified(
            manifest, dir=updates, prefix=_DOWNLOAD_PREFIX,
            suffix=_DOWNLOAD_SUFFIX, progress=on_bytes)
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
        # Old bundle: best-effort removal on a worker; open files keep working
        # on the unlinked inodes until this process exits.
        if old is not None:
            threading.Thread(target=shutil.rmtree, args=(old,),
                             kwargs={"ignore_errors": True}, daemon=True).start()

    def _check_disk_space(self, updates: str) -> None:
        stat = os.statvfs(updates)
        free = stat.f_bavail * stat.f_frsize
        if free < _DISK_SPACE_FACTOR * common.MAX_ARTIFACT_BYTES // 2:
            raise RuntimeError("not enough free disk space to download the update")

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
                return None
            _manager = UpdateManager()
            _manager.start_auto_checks()
        return _manager
