"""Platform-neutral in-app updater state machine (docs/PYTHON_SUPERVISOR_SPEC.md
"Software updates" gives the Windows design this mirrors). A silent background
loop checks the signed manifest and surfaces a newer version only through
/api/config's `update` field — the shell shows a badge. Downloading and
installing happen solely on an explicit POST /api/update/install.

Factored out of update/mac.py so the macOS and Linux in-app updaters share one
state machine, one throttle, and one Activity-dock job mirroring; each
platform supplies only what actually differs (`method()`, `_disk_version()`,
`_install_artifact()`, and the class attrs governing the download's temp-file
naming and startup delay). See mac.py / linux.py for what each subclass adds.

An install mirrors itself into the Activity dock as a server-owned job
(`sys:update:<version>`, the same bridge shape `server/routers/index.py`'s
`_mirror_one_run_job` uses for a rescan): the dock is where every other
long-running thing in the app already lives, and it is the surface that can
show bytes, a phase, and a Cancel without the sidebar badge growing a second
progress readout. Cancel arrives the way every job cancel does — as a flag on
the reply to the progress tick this manager was going to send anyway — and is
honoured only while DOWNLOADING; once the artifact swap starts there is no
safe point to stop at.

Everything runs on worker threads and never raises out of the manager: a
failed check leaves state "idle"/"error", never a dead loop. Job reporting is
best-effort on top of that — an install must never fail because its row could
not be drawn.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

from fused_render import __version__, jobs
from fused_render.update import common

logger = logging.getLogger("fused_render.update")

# Fallback default for `_running_version()`'s `_const("__version__")` lookup:
# every concrete subclass module (mac.py, linux.py) re-imports and re-exports
# its own `__version__` for exactly this reason, but a base `UpdateManager`
# used directly (there is none in production, but nothing stops a test from
# doing so) still needs a name to fall back to in `globals()`.

# Overridable for staging/E2E tests (point a test build at a test manifest).
# Safe to expose: the manifest must still verify against the pinned ed25519
# key, so redirecting the URL alone cannot feed the updater different bytes.
# (Each subclass defines its own MANIFEST_URL constant against its own
# platform env var and default CloudFront path; this module has no default of
# its own since there is no platform-neutral artifact to point at.)

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
# STALE_AFTER_S (30s): a whole-artifact swap has no progress to report, but a
# row that says nothing for half a minute is shown as "No longer reporting",
# and one that says nothing for STALE_DROP_S (600s) is dropped outright —
# after which the final `done` upsert lands on a dismissed id and the
# "Installed — restart to finish" line is never drawn.
INSTALL_HEARTBEAT_S = 10.0
# A CHECK-ONLY MANAGER IN A DEV RUN (Akshil, 2026-09-10). `start()` refuses to
# run outside a bundle — there is nothing to swap — which also means an
# unpackaged server never shows the badge, so the sidebar's "Check for updates"
# row (UpdateBadge) cannot be tried against 127.0.0.1 at all. Set this to a
# non-empty value and `start()` builds a manager with no bundle: it fetches and
# verifies the real manifest, compares against the running __version__, and
# reports every state a packaged app would — but `install()` refuses, and
# `status()` says so (`check_only`) so the badge hides its Update button. Never
# read by a packaged app: a bundle is a bundle whatever the environment says.
DEV_MANAGER_ENV = "FUSED_RENDER_UPDATE_DEV_MANAGER"
# Floor between two checks that actually hit the network. The client checks
# on its own when the app comes back to the front (update-status.ts), and
# that trigger is a window event: a user cmd-tabbing in and out, or a
# second window taking focus, could otherwise turn one return into a
# string of manifest fetches. The client already keeps a 30-minute gap of
# its own; this one is the server-side backstop, because the client's gap
# lives in one tab's module state and any reload resets it. Only the
# throttled path (POST /api/update/check) is affected — the auto loop
# passes force=True so its own five-minute tick is never swallowed. The
# sidebar's "Check for updates" row goes through the throttled path too: a
# press inside the gap gets the answer the last fetch left, which is at most
# a minute old and is what "up to date" meant a moment ago anyway.
MIN_CHECK_GAP_S = 60.0
# The floor AFTER A FAILED fetch. Not the full minute — a laptop that just came
# back online must be able to press "Check for updates" and get a real retry,
# not the same "Couldn't check" answered from memory for the rest of the minute
# (bugbot, PR #1097) — but not nothing either: several windows on one server
# each fire their own check-on-return, and with the floor gone an outage would
# turn every focus flip into a 15-second manifest fetch (review, PR #1097). Five
# seconds bounds that to one fetch at a time and is shorter than any human
# retry.
FAILED_CHECK_GAP_S = 5.0
DONE_MESSAGE = "Installed — restart to finish"
CANCELLED_MESSAGE = "Cancelled"
# Keep a margin over the artifact itself: the download and the staged/swapped
# copy coexist briefly during the swap.
_DISK_SPACE_FACTOR = 3


class UpdateManager:
    """State machine behind /api/config's `update` field.

    states: idle -> checking -> (idle | available) -> installing(progress)
            -> installed | error(message)
    "installed" means the artifact on disk is the new version; the existing
    installed_version drift banner drives the restart from there. Every
    method takes the same route through those states regardless of platform —
    `method()` rides along on status() purely as an INFORMATIONAL word (mac's
    subclass uses it for "brew" vs "dmg"; nothing here brances on it)."""

    # Subclass class attrs: the download's temp-file naming and the delay
    # before the first check (see start_auto_checks()).
    _DOWNLOAD_PREFIX: str = "FusedRender-"
    _DOWNLOAD_SUFFIX: str = ""
    _STARTUP_DELAY_S: float = 1.0

    def __init__(self, *, manifest_url: str, bundle: str | None = None,
                 method: str | None = None, check_only: bool = False):
        # RLock: the early-return paths in check()/install() read status()
        # while already holding the lock.
        self._lock = threading.RLock()
        self._manifest_url = manifest_url
        self._bundle = bundle
        self._method = method  # resolved lazily: detection can cost a subprocess
        # See DEV_MANAGER_ENV: a manager that may look but never swap.
        self._check_only = check_only
        # Why the LAST CHECK could not answer (network, a manifest that did not
        # verify), or None when it did. Distinct from `_error`, which is an
        # install's. Without this a failed fetch and "up to date" were the same
        # wire status — "idle" — and the sidebar's manual check would have said
        # "Up to date" to a laptop that was offline.
        self._check_error: str | None = None
        self._state = "idle"
        self._latest: dict | None = None
        self._error: str | None = None
        self._progress: float | None = None
        self._progress_total: float | None = None
        self._phase: str | None = None
        self._install_thread: threading.Thread | None = None
        # time.monotonic() of the last check that actually fetched the
        # manifest — the MIN_CHECK_GAP_S throttle's only state. monotonic,
        # not wall time, so a clock change cannot open or close the gap.
        self._last_check_at: float | None = None
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

    def _const(self, name: str):
        """Read a module-level constant by NAME, preferring the concrete
        subclass's own module (mac.py / linux.py) over this one.

        `check()`, `install()`, `_beat_installing()` etc. all live here in the
        shared base, but `tests/test_mac_update.py` patches constants like
        `MIN_CHECK_GAP_S` on the `mac` module directly (the same
        re-export-for-patchability idiom `_win32/update.py` uses) — a bare
        module global in THIS file would never see that patch, since it lives
        in a different module's namespace. Looking the name up on
        `sys.modules[type(self).__module__]` first means a subclass's
        module-level re-export (or its own override, for a value a platform
        actually wants to differ on) is always what gets read, and only a
        subclass that defines no such name at all falls back to the default
        below."""
        module = sys.modules.get(type(self).__module__)
        if module is not None and hasattr(module, name):
            return getattr(module, name)
        return globals()[name]

    # -- status ---------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            # An update can also land from outside this process entirely — a
            # `brew upgrade` or a manual DMG drag in the user's own hands — so
            # "available" re-checks the artifact on disk on every read (the UI
            # polls this every minute) rather than waiting out the next
            # common.CHECK_INTERVAL_S tick to notice it.
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
                # Always None since D767 — kept on the wire so the client's
                # UpdateStatus shape is unchanged. There is no terminal
                # command to offer for any method.
                "manual_command": None,
                # Which half of an install is running — "downloading" while the
                # artifact streams, "installing" from there to the swap — so the
                # badge can say the one word that matters (Akshil, 2026-09-08:
                # "no longer phrases, just words"). None outside "installing".
                "phase": self._phase if self._state == "installing" else None,
                # True only for the dev-run manager (DEV_MANAGER_ENV): the badge
                # draws the "Update available" row but not its Update button.
                "check_only": self._check_only,
                # The last check's failure, if it failed — what lets a manual
                # check say "Couldn't check" rather than "Up to date".
                "check_error": self._check_error,
            }

    def method(self) -> str:
        """"" | a platform-specific informational word. Overridden by every
        subclass (mac probes brew, linux reports whether it's running from an
        AppImage)."""
        raise NotImplementedError

    # -- checking -------------------------------------------------------------

    def start_auto_checks(self) -> None:
        """Background check loop (startup delay, then every five minutes —
        common.CHECK_INTERVAL_S).
        Silent: a newer version only flips state to "available"; set
        FUSED_RENDER_NO_AUTO_UPDATE to a non-empty value to disable."""
        if os.environ.get("FUSED_RENDER_NO_AUTO_UPDATE"):
            return

        def loop():
            # Resolve the install method HERE, off the request path: probing
            # it (mac's brew probe, e.g.) can take seconds, and `status()` used
            # to pay it on the first /api/config the shell asked for after boot.
            try:
                self.method()
            except Exception:  # noqa: BLE001 - detection is best-effort
                logger.exception("update method detection failed")
            time.sleep(self._STARTUP_DELAY_S)
            swept = False
            while True:
                try:
                    # Inside the try, like the Windows loop's sweep: it walks
                    # the updates dir, and an OSError there must cost one tick,
                    # not the whole auto-check thread. Still only once per
                    # process — the leftovers it clears are a previous
                    # session's.
                    # NEVER FROM THE CHECK-ONLY MANAGER (review, PR #1097): the
                    # updates dir is one machine-wide path, shared with the
                    # packaged app's manager, and a dev run that swept it would
                    # delete an artifact the real app had just downloaded.
                    if not swept and not self._check_only:
                        self._sweep_stale_downloads()
                        swept = True
                    # force: this tick IS the cadence, so it must never be
                    # swallowed by MIN_CHECK_GAP_S because a focus flip
                    # happened to fetch a minute ago.
                    self.check(force=True)
                except Exception:  # noqa: BLE001 - a tick must never kill the loop
                    logger.exception("auto update tick failed")
                time.sleep(common.CHECK_INTERVAL_S)

        threading.Thread(target=loop, daemon=True,
                         name="fused-render-update-auto").start()

    def check(self, force: bool = False) -> dict:
        """Fetch + verify the manifest and update state. Never touches state
        while an install is running. Returns status().

        Throttled by default: a check that would land within MIN_CHECK_GAP_S
        of the last one that actually fetched returns the current status
        untouched, so the client's check-on-return cannot turn a run of focus
        flips into a run of CDN requests. `force=True` (the auto loop) always
        fetches."""
        with self._lock:
            if self._state == "installing":
                return self.status()
            # A FETCH ALREADY IN FLIGHT OWNS THE ANSWER (bugbot, PR #1097).
            # "checking" is set only here, right before a fetch, and every exit
            # from that fetch resolves it, so the state IS the fact that one is
            # out. A second fetch alongside it — the five-minute tick landing
            # while a press on the sidebar row is still waiting — asked the same
            # question of the same manifest, and whichever answer landed second
            # found the state moved and was dropped: a version found by the
            # later fetch was lost until the next tick, and a recovered success
            # behind a failed press was lost the same way. Forced or not, the
            # caller gets the status the in-flight fetch will finish.
            if self._state == "checking":
                return self.status()
            # A NON-FORCED CHECK ONLY EVER LOOKS FROM "IDLE" (bugbot, PR #1078):
            # an update already offered, installed or failed is an answer, and a
            # check-on-return or a press on the sidebar row has nothing to learn
            # by asking again. The five-minute loop forces, and is the one that
            # keeps a found version current.
            if not force and self._state != "idle":
                return self.status()
            if not force and self._last_check_at is not None and (
                    time.monotonic() - self._last_check_at < self._const("MIN_CHECK_GAP_S")):
                # Not an error and not "checking": nothing was asked of the
                # network, so the caller gets the answer the last check left —
                # which status() still re-derives from the artifact on disk.
                return self.status()
            self._last_check_at = time.monotonic()
            # ONLY AN IDLE MANAGER SAYS "checking" (bugbot, PR #1097). A forced
            # re-check from "available" used to flip the state to "checking" for
            # the length of the fetch: the badge lost its accordion for those
            # seconds every tick, and — once the client learned to hold the
            # accordion through it — install() silently refused for the same
            # seconds, because "checking" is not a state it installs from. A
            # re-check does not un-know the update it is re-checking: the state
            # it entered from is what the wire says while the fetch is out, and
            # `during` is what the tail below compares against, so an install
            # that starts mid-fetch is left alone exactly as before.
            during = "checking" if self._state == "idle" else self._state
            self._state = during
        try:
            manifest = common.fetch_manifest(self._manifest_url)
            newer = common.is_newer(manifest["version"], self._running_version())
        except Exception as error:  # noqa: BLE001 - network/manifest failures are routine
            logger.info("update check failed: %s", error)
            with self._lock:
                self._check_error = str(error) or error.__class__.__name__
                # A failure arms the SHORT floor, not the full minute (see
                # FAILED_CHECK_GAP_S): a fetch that failed is not the load the
                # long gap guards against, and the sidebar's row comes back to
                # "Check for updates" after four seconds looking pressable — it
                # has to be. Expressed as a back-dated timestamp so the one
                # comparison above stays the only throttle logic.
                self._last_check_at = time.monotonic() - (
                    self._const("MIN_CHECK_GAP_S") - self._const("FAILED_CHECK_GAP_S"))
            # Keep a previously-found update visible over a transient failure —
            # but re-derive WHICH state from the artifact on disk, exactly like
            # the success path below: a network blip after a completed install
            # must not resurface the install button.
            disk = self._disk_version()
            with self._lock:
                if self._state == during:
                    self._error = None
                    if self._latest and disk is not None and not common.is_newer(
                            self._latest["version"], disk):
                        self._state = "installed"
                    elif self._latest:
                        self._state = "available"
                    else:
                        self._state = "idle"
            return self.status()
        # The artifact on disk, not the running __version__, decides "already
        # installed": after a successful swap (ours, or a manual one outside
        # this process) this process still runs the old code, and comparing
        # against __version__ alone would flip a completed install back to
        # "available" — offering a second swap against an already-new artifact.
        disk = self._disk_version()
        with self._lock:
            self._check_error = None
            # Untouched if anything else moved the state while the fetch was
            # out — an install that began from "available" (the one case that
            # used to be impossible, since the state was "checking").
            if self._state == during:
                self._error = None
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

    def _running_version(self) -> str:
        """The version of THIS process, for the check's "is a fetched manifest
        newer" comparison. Read through `_const()`, exactly like every other
        module-level constant this class reads back from the concrete
        subclass's own module: `mac.py`/`linux.py` each do `from fused_render
        import __version__` at module scope, and a test that does
        `monkeypatch.setattr(mac, "__version__", ...)` is rebinding THAT
        module-level name, which only `sys.modules[type(self).__module__]`
        (what `_const` looks up) ever sees — a bare `from fused_render import
        __version__` here reads the package attribute instead, which no test
        touches."""
        return self._const("__version__")

    def _disk_version(self) -> str | None:
        """The version that would launch next time — what decides "already
        installed" independent of what THIS process has loaded. Overridden by
        every subclass: mac reads the bundle's Info.plist; linux reads the
        stamp file `_install_artifact` writes after a swap."""
        raise NotImplementedError

    # -- installing -----------------------------------------------------------

    def install(self, expected_version: str | None = None) -> dict:
        """Kick the install on a worker thread. One at a time; re-POSTing
        while installing just reports current state. Allowed from "available"
        and from "error" (retry).

        `expected_version` is what the CALLER had on screen — the client
        sends the `latest_version` its last poll showed. That is not the
        same thing as this manager's own `_latest` at the moment install()
        starts: the background loop force-checks every CHECK_INTERVAL_S (5
        min) independent of any click, so `_latest` can already have moved
        — silently, from the caller's point of view — before the click even
        lands. Comparing against a snapshot of `_latest` taken at call-start
        would miss exactly that race, since both the snapshot and the
        post-recheck value would already agree on the NEW version while the
        screen the user clicked on still showed the old one. `expected_version`
        is the only thing that actually pins down what the user saw.

        Also force a fresh check before proceeding, so a version published
        between the caller's last poll and this call — but not yet picked up
        by the background loop either — still gets caught: a failed fetch
        falls back to the last known-good `_latest` (see check()), so this
        never makes an install worse, only fresher when it can be. Only
        worth the round trip when there is something to install at all —
        skipped from "idle"/"checking"/"installing", which refuse below
        regardless of what a re-check would say.

        If `_latest` (after that recheck) does not match `expected_version`,
        do not install it out from under the click: the button the user
        pressed said "Update to v<expected>", and quietly swapping in a
        different version — even a newer one — is its own kind of dishonest
        wire status, the same family of bug the manager otherwise goes out
        of its way to avoid (`_check_error`, `check_only`, etc. all exist so
        this state machine never tells the UI something that isn't true).
        Instead this leaves `_latest`/state exactly as they are ("available"
        with whatever is actually current) and returns without installing —
        the badge now shows that version, and a second click commits to it.
        `expected_version=None` (an older client with no such field, or a
        direct caller) skips this check entirely and trusts `_latest`
        as-is, same as before this parameter existed."""
        with self._lock:
            worth_rechecking = (self._latest is not None
                               and self._state in ("available", "error"))
        if worth_rechecking:
            self.check(force=True)
        with self._lock:
            if self._state == "installing":
                return self.status()
            if self._latest is None or self._state not in ("available", "error"):
                return self.status()
            if (expected_version is not None
                    and self._latest["version"] != expected_version):
                logger.info(
                    "update install deferred: v%s is current, not the v%s "
                    "the caller had on screen",
                    self._latest["version"], expected_version)
                return self.status()
            # The dev-run manager (DEV_MANAGER_ENV) has no artifact to swap.
            # Refused here rather than left to fail inside the worker thread,
            # so the state stays "available" and honest instead of "error".
            if self._check_only:
                logger.info("update install refused: check-only manager (%s)", DEV_MANAGER_ENV)
                return self.status()
            manifest = self._latest
            self._state = "installing"
            self._error = None
            self._progress = 0.0
            self._progress_total = None
            self._phase = "downloading"
            self._job_id = self._const("JOB_PREFIX") + str(manifest["version"])
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
            done=0.0, total=None, detail=self._const("PHASE_DOWNLOADING"), message="",
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
            self._install_artifact(manifest)
        except common.UpdateCancelled:
            # Not a failure: the update is still there to install, so the
            # manager goes back to exactly where the ✕ was pressed from —
            # "available", `_latest` intact, no error text, and the install
            # button live again. Only the download path can raise this (see
            # `_install_artifact`'s `should_abort`), so there is never a
            # half-swapped artifact to reason about here.
            logger.info("update install cancelled")
            # The terminal report goes FIRST, before the state flip: the moment
            # `_state` leaves "installing" a re-POST is allowed through, and it
            # would mint a fresh attempt on this same per-version id — onto
            # which this stale "cancelled" would then land, killing a row whose
            # download had just started. Reporting first closes that window;
            # every terminal path below follows the same order.
            cancelled_message = self._const("CANCELLED_MESSAGE")
            self._job_report(state="cancelled", detail=cancelled_message,
                             message=cancelled_message, cancellable=False)
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
        # SILENT ON SUCCESS (2026-09-18). This row used to land on the default
        # `trail` tier, which pops a card in the floating column
        # (`popupJobs`, frontend/src/platform/lib/jobs.ts) — and a toast saying
        # "Installed — restart to finish" now arrives at the same instant as
        # the blocking dialog that says the same thing and offers the button,
        # because the install reaching "installed" is what raises that dialog
        # (`UpdateDialog`'s restart mode, D2). Two announcements of one event,
        # one of them dismissible and useless.
        #
        # `silent` is the tier for exactly that — "finishing is not news"
        # (jobs.py's TIERS) — and the ONE lever that suppresses the pop.
        # It is a property of SUCCESS only: `effective_tier` still promotes an
        # `error`/`cancelled` row to `attention`, so a failed install is as loud
        # as it ever was, and the RUNNING row (its own reports, above, which do
        # not restate tier and so stay `trail`) is untouched — the download's
        # bytes, phase and ✕ all still draw.
        #
        # NOTE that `silent` also means NOT RETAINED: `jobRows` drops a
        # silent+done row from the Notifications panel and `_sweep` ages it out
        # of the registry. There is no tier that pops nothing yet keeps a row,
        # and inventing one for this would be a fifth tier for a row nobody
        # reads after the restart it is announcing.
        #
        # `detail` as well as `message`: `jobStatusLine` reads `detail` for a
        # `done` row and `message` for an `error` one.
        done_message = self._const("DONE_MESSAGE")
        self._job_report(state="done", detail=done_message, message=done_message,
                         done=None, total=None, cancellable=False,
                         tier=jobs.SILENT)
        with self._lock:
            self._state = "installed"
            self._progress = None
            self._progress_total = None

    def _install_artifact(self, manifest: dict) -> None:
        """Download + verify + swap. Must raise `common.UpdateCancelled` (not
        return) when a cancel is honoured, and must leave nothing behind on
        any other failure. Overridden by every subclass: mac mounts a DMG and
        ditto's the bundle in place; linux downloads the new AppImage next to
        the running one and os.replace()s it."""
        raise NotImplementedError

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
            # No dedicated update page or Preferences tab exists — the
            # update surface is sidebar chrome (UpdateBadge.tsx's badge) and
            # the blocking restart dialog ServerStatusBanner.tsx raises —
            # present on every route
            # rather than a page of its own. /preferences is the same
            # fallback the gh-CLI-install job uses for the same reason.
            result = jobs.upsert({"id": job_id, **fields},
                                 page="/preferences", server=True)
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
        no numbers, still no ✕ — and that is the point: a whole-artifact swap
        can outrun both stale windows in `jobs.py` with nothing to say in
        between."""
        while not stop.wait(self._const("INSTALL_HEARTBEAT_S")):
            # Re-checked after the wait: a beat that woke just as the swap
            # ended must not land after the terminal row (bugbot, PR #1058) —
            # and the stopper JOINS this thread before writing that row, so a
            # beat already inside `_job_report` finishes first.
            if stop.is_set():
                return
            self._job_report(detail=self._const("PHASE_INSTALLING"), message="",
                             cancellable=False)

    def _cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel

    # -- shared download-dir housekeeping ---------------------------------------

    def _updates_dir(self) -> str:
        path = os.path.expanduser("~/Library/Application Support/fused-render/updates")
        os.makedirs(path, exist_ok=True)
        return path

    def _sweep_stale_downloads(self) -> None:
        """Best-effort cleanup of downloads a previous session left behind
        (install failed, or the process died mid-download).

        Scoped to entries whose name matches THIS manager's own
        `_DOWNLOAD_PREFIX`/`_DOWNLOAD_SUFFIX` — never the whole directory.
        On mac `_updates_dir()` is a dedicated `…/fused-render/updates`
        directory the app owns outright, so an unscoped sweep only ever hit
        the app's own leftovers; on Linux it is the AppImage's own parent
        directory (`_updates_dir()` there deliberately downloads next to the
        running AppImage, since `os.replace()` needs the same filesystem), a
        directory the USER owns (`~/Applications`, `~/Downloads`, …) and
        shares with whatever else they keep there. An unscoped sweep would
        delete every sibling file on every boot; scoping it to the download
        naming pattern is what makes this safe on both platforms.

        Name-matching alone is not enough to spare the running artifact
        itself, though: the released AppImage is named
        `FusedRender-<version>-x86_64.AppImage`, which matches
        `FusedRender-`/`.AppImage` the same way a stale download would — so
        the manager's own target path is excluded explicitly, by real path,
        on top of the name match."""
        import shutil
        prefix = self._DOWNLOAD_PREFIX
        suffix = self._DOWNLOAD_SUFFIX
        running = os.path.realpath(self._bundle) if self._bundle is not None else None
        try:
            updates = self._updates_dir()
            for name in os.listdir(updates):
                if not (name.startswith(prefix) and name.endswith(suffix)):
                    continue
                full = os.path.join(updates, name)
                if running is not None and os.path.realpath(full) == running:
                    continue
                try:
                    if os.path.isdir(full):
                        shutil.rmtree(full)
                    else:
                        os.unlink(full)
                except OSError:
                    pass
        except OSError:
            pass

    def _check_disk_space(self, updates: str) -> None:
        stat = os.statvfs(updates)
        free = stat.f_bavail * stat.f_frsize
        if free < _DISK_SPACE_FACTOR * common.MAX_ARTIFACT_BYTES // 2:
            raise RuntimeError("not enough free disk space to download the update")
