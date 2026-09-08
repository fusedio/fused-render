"""macOS in-app updater (docs/PYTHON_SUPERVISOR_SPEC.md "Software updates"
gives the Windows design this mirrors). A silent background loop checks the
signed manifest and surfaces a newer version only through /api/config's
`update` field — the shell shows a badge. Downloading and installing happen
solely on an explicit POST /api/update/install.

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
  drifts from __version__, ServerStatusBanner shows the restart card, and
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
# The Activity row's id, one per version (`jobs.SERVER_ID_PREFIX`, never the
# literal "sys:"): deterministic, so a retry after a failure re-attaches to
# the row the user is already looking at rather than stacking a second one.
JOB_PREFIX = jobs.SERVER_ID_PREFIX + "update:"
# The phase words the row shows while running, and the two terminal lines.
# Kept here rather than inline so the tests assert against the same strings
# the UI reads (design vocabulary: "Downloading" / "Installing" /
# "Installed — restart to finish" / "Cancelled").
PHASE_DOWNLOADING = "Downloading"
PHASE_INSTALLING = "Installing"
# How often the swap re-reports itself while it runs. Well under `jobs.py`'s
# STALE_AFTER_S (30s): a `ditto` of a whole .app bundle has no progress to
# report, but a row that says nothing for half a minute is shown as "No longer
# reporting", and one that says nothing for STALE_DROP_S (600s) is dropped
# outright — after which the final `done` upsert lands on a dismissed id and
# the "Installed — restart to finish" line is never drawn.
INSTALL_HEARTBEAT_S = 10.0
DONE_MESSAGE = "Installed — restart to finish"
CANCELLED_MESSAGE = "Cancelled"
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
        if os.path.islink(old):
            os.remove(old)
        else:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        logger.debug("could not remove old bundle %s", old, exc_info=True)


class UpdateManager:
    """State machine behind /api/config's `update` field.

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
        self._progress: float | None = None
        self._progress_total: float | None = None
        self._install_thread: threading.Thread | None = None
        # The Activity row for the install currently in flight, and whether
        # its ✕ has been pressed. Both are only ever touched under the lock:
        # the download runs on the install thread while the cancel arrives on
        # whichever thread reported the tick that carried it back.
        self._job_id: str | None = None
        self._cancel = False
        # Latched when a report fails: the row could not be drawn at all, and
        # a download is a report per megabyte — one warning says everything a
        # thousand identical tracebacks would (see `_job_report`).
        self._job_broken = False

    # -- status ---------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            # An update can also land from outside this process entirely — a
            # `brew upgrade` or a manual DMG drag in the user's own hands — so
            # "available" re-checks the bundle on disk on every read (the UI
            # polls this every minute) rather than waiting out the next
            # CHECK_INTERVAL_S tick to notice it.
            if self._state == "available" and self._latest:
                disk = self._disk_version()
                if disk is not None and not common.is_newer(
                        self._latest["version"], disk):
                    self._state = "installed"
            return {
                "state": self._state,
                "method": self.method(),
                "latest_version": self._latest["version"] if self._latest else None,
                "progress": self._progress,
                "progress_total": self._progress_total,
                "error": self._error,
                # Always None since D742 — kept on the wire so the client's
                # UpdateStatus shape is unchanged. There is no terminal
                # command to offer for any method (see the class docstring).
                "manual_command": None,
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
            # Keep a previously-found update visible over a transient failure —
            # but re-derive WHICH state from the bundle on disk, exactly like
            # the success path below: a network blip after a completed install
            # must not resurface the install button.
            disk = self._disk_version()
            with self._lock:
                if self._state == "checking":
                    if self._latest and disk is not None and not common.is_newer(
                            self._latest["version"], disk):
                        self._state = "installed"
                    elif self._latest:
                        self._state = "available"
                    else:
                        self._state = "idle"
            return self.status()
        # The bundle on disk, not the running __version__, decides "already
        # installed": after a successful swap (ours, brew's, or a manual one
        # in a terminal) this process still runs the old code, and comparing
        # against __version__ alone would flip a completed install back to
        # "available" — offering a second swap against an already-new bundle.
        disk = self._disk_version()
        with self._lock:
            if self._state == "checking":
                if newer and disk is not None and not common.is_newer(
                        manifest["version"], disk):
                    self._latest = manifest
                    self._state = "installed"
                elif newer:
                    self._latest = manifest
                    self._state = "available"
                else:
                    self._latest = None
                    self._state = "idle"
        return self.status()

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

    def install(self) -> dict:
        """Kick the install on a worker thread. One at a time; re-POSTing
        while installing just reports current state. Allowed from "available"
        and from "error" (retry)."""
        with self._lock:
            if self._state == "installing":
                return self.status()
            if self._latest is None or self._state not in ("available", "error"):
                return self.status()
            # One install path for every method (Akshil, 2026-09-08): the
            # bundle is the bundle whichever tool put it there, and the swap
            # is version-verified either way. brew is never invoked — see the
            # module docstring.
            manifest = self._latest
            self._state = "installing"
            self._error = None
            self._progress = 0.0
            self._progress_total = None
            self._job_id = JOB_PREFIX + str(manifest["version"])
            self._cancel = False
            self._job_broken = False
            thread = threading.Thread(
                target=self._install, args=(manifest,), daemon=True,
                name="fused-render-update-install")
            self._install_thread = thread
        # Opened here, not on the install thread, so the row exists by the
        # time the POST that started this returns and the dock's very next
        # poll draws it.
        # The phase word goes in `detail`, not `message`: `jobTypeLabel` (the
        # dock/StatusBar chip) reads `detail` for its leading verb and falls
        # back to the title — "Update to v9.9.9" has no verb in it — while
        # `jobStatusLine` joins `message` and `detail` for a running row, so
        # sending the same word in both would render it twice.
        self._job_report(
            title=f"Update to v{manifest['version']}",
            kind="download", unit="bytes", state=jobs.RUNNING,
            done=0.0, total=None, detail=PHASE_DOWNLOADING, message="",
            cancellable=True)
        # The id is per-version, so a retry after a failed or cancelled
        # attempt inherits the previous attempt's row — including a
        # `cancel_requested` flag `upsert` only clears on a transition INTO a
        # terminal state. Disown it before the download starts, or the new
        # attempt reads the old attempt's ✕ on its first tick.
        self._job_clear_cancel()
        thread.start()
        return self.status()

    def _install(self, manifest: dict) -> None:
        try:
            if self.method() in ("dmg", "brew"):
                self._install_dmg(manifest)
            else:
                raise RuntimeError("not running from an installed bundle")
        except common.UpdateCancelled:
            # Not a failure: the update is still there to install, so the
            # manager goes back to exactly where the ✕ was pressed from —
            # "available", `_latest` intact, no error text, and the install
            # button live again. Only the download path can raise this (see
            # `_install_dmg`'s `should_abort`), so there is never a
            # half-swapped bundle to reason about here.
            logger.info("update install cancelled")
            # The terminal report goes FIRST, before the state flip: the moment
            # `_state` leaves "installing" a re-POST is allowed through, and it
            # would mint a fresh attempt on this same per-version id — onto
            # which this stale "cancelled" would then land, killing a row whose
            # download had just started. Reporting first closes that window;
            # every terminal path below follows the same order.
            self._job_report(state="cancelled", detail=CANCELLED_MESSAGE,
                             message=CANCELLED_MESSAGE, cancellable=False)
            with self._lock:
                self._state = "available"
                self._error = None
                self._progress = None
                self._progress_total = None
            return
        except Exception as error:  # noqa: BLE001 - reported through state, never raised
            logger.exception("update install failed")
            self._job_report(state="error", message=str(error), cancellable=False)
            with self._lock:
                self._state = "error"
                self._error = str(error)
            return
        # The terminal row is also the completion NOTICE: `ActivityDock`'s
        # `onJobsReported` -> `terminalNotifications` already carries every
        # terminal job into Notifications, so saying it once here is the whole
        # announcement — no second mechanism, and no client-side transition to
        # watch for. `detail` as well as `message`: `jobStatusLine` reads
        # `detail` for a `done` row and `message` for an `error` one.
        self._job_report(state="done", detail=DONE_MESSAGE, message=DONE_MESSAGE,
                         done=None, total=None, cancellable=False)
        with self._lock:
            self._state = "installed"
            self._progress = None
            self._progress_total = None

    # -- the Activity row ------------------------------------------------------

    def _job_report(self, **fields) -> None:
        """Report one tick of the install into the job registry, and read a
        cancel back off the reply.

        Best-effort in both directions, and that asymmetry is the point: a
        report that fails is logged and dropped (an install must not die
        because its row could not be drawn), while a cancel can only ever
        ARRIVE this way — `jobs.request_cancel` sets a flag and leaves it for
        the reporter to notice on the tick it was going to send anyway (SPEC
        BG-4, and the same read `_mirror_one_run_job` does).

        The registry's own lock is taken INSIDE this call, so `self._lock` is
        released first and never held across it: `jobs.py` never calls back
        into this manager, but holding two locks in one order here and the
        other order in `status()` is a deadlock waiting for a coincidence.
        """
        with self._lock:
            job_id = self._job_id
            broken = self._job_broken
        if job_id is None or broken:
            return
        try:
            result = jobs.upsert({"id": job_id, **fields}, server=True)
        except Exception:  # noqa: BLE001 - reporting is never load-bearing
            # Latched, not retried: reporting failed once and there is a tick
            # per megabyte behind this one, so retrying would fill the log with
            # the same traceback hundreds of times over a single download while
            # the row stayed just as undrawn. One line, then silence — and the
            # install itself carries on, which is the whole rule for this row.
            with self._lock:
                self._job_broken = True
            logger.exception("could not report update job %s — no further "
                             "progress will be reported for it", job_id)
            return
        if result.get("cancel_requested"):
            with self._lock:
                self._cancel = True

    def _job_clear_cancel(self) -> None:
        with self._lock:
            job_id = self._job_id
            self._cancel = False
        if job_id is None:
            return
        try:
            jobs.clear_cancel_requested(job_id)
        except Exception:  # noqa: BLE001 - same best-effort rule as _job_report
            logger.exception("could not clear cancel on update job %s", job_id)

    def _beat_installing(self, stop: threading.Event) -> None:
        """Re-send the Installing phase every `INSTALL_HEARTBEAT_S` until
        `stop` is set. Nothing but the row's clock changes — same phase, still
        no numbers, still no ✕ — and that is the point: a `ditto` of a whole
        .app bundle can outrun both stale windows in `jobs.py` with nothing to
        say in between."""
        while not stop.wait(INSTALL_HEARTBEAT_S):
            # Re-checked after the wait: a beat that woke just as the swap
            # ended must not land after the terminal row (bugbot, PR #1058) —
            # and the stopper JOINS this thread before writing that row, so a
            # beat already inside `_job_report` finishes first.
            if stop.is_set():
                return
            self._job_report(detail=PHASE_INSTALLING, message="",
                             cancellable=False)

    def _cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel

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
        # on the unlinked inodes until this process exits. `rmtree` REFUSES a
        # symlink (and would follow it if it didn't), so the link case unlinks
        # instead — `bundle` is realpath'd above, but an .app that is itself a
        # symlink on the resolved path is cheap to survive rather than leave a
        # dangling `.FusedRender-old-<pid>.app` behind forever.
        if old is not None:
            threading.Thread(target=_discard_old_bundle, args=(old,),
                             daemon=True).start()

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
