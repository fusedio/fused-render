"""macOS in-app updater (docs/LINUX_AUTO_UPDATE_SPEC.md "What already exists").
A silent background loop checks the signed manifest and surfaces a newer
version only through /api/config's `update` field — the shell shows a badge.
Downloading and installing happen solely on an explicit POST
/api/update/install.

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
- "dmg": download the signed DMG, verify, and STAGE the .app bundle as a
  hidden sibling of the installed one (`installed.staged_bundle_path`:
  `/Applications/.Fused Render-update.app`). The staged copy is renamed into
  place by `apply_staged` from the quit teardown (app.quit_teardown's
  "update" rung — every quit surface, the update dialog's Restart included),
  never while the app is serving. It used to be swapped at install time, on
  the theory that a running process keeps its open files; it does, but it
  does not keep the files it has NOT opened yet: every window open across
  the swap asked for a lazily loaded route chunk by a hash that was no
  longer on disk ("This page didn't load", on every release — four a day,
  2026-11-03), and a lazy Python import read the NEW module into the OLD
  process. Staging keeps the running process, its windows and its children
  on one consistent bundle until the user restarts.

  The restart signal is unchanged: installed.installed_version() reports the
  staged version (it is what launches next), so it drifts from __version__
  exactly as a manual DMG drag makes it drift, ServerStatusBanner raises the
  restart card, and <scheme>://relaunch (app.begin_relaunch;
  fused-render://) quits through the teardown — which applies
  the staged bundle — and respawns from disk. The bundle renamed away at
  apply time stays on disk as a hidden `.<Bundle>-old-<pid>-<ns>.app`
  sibling until a LATER boot's `sweep_old_bundles` finds its pid dead: the
  applying process is milliseconds from `os._exit`, but macOS TCC drops its
  Full Disk Access the instant its on-disk code vanishes, and the rollback
  on a failed second rename needs it too.

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

import ctypes
import ctypes.util
import errno
import logging
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from fused_render import __version__, _flavor, bundle_exec, installed, jobs
from fused_render._branch import branch_suffix
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
#
# One manifest per arch: the Intel DMG publishes under `<prefix>-macos-x86_64/`
# (release.yml's build-sign-notarize-release-intel). Keyed on the arch this
# PROCESS runs as, not the chip — the Intel build under Rosetta on an Apple
# Silicon Mac reports x86_64 and must keep updating to the Intel DMG, the one
# its installed bundle is.
_MANIFEST_ARCH_SUFFIX = "-x86_64" if platform.machine() == "x86_64" else ""
MANIFEST_URL = os.environ.get(
    "FUSED_RENDER_UPDATE_MANIFEST_URL",
    f"https://d2ic19jpchjovp.cloudfront.net/{_flavor.release_prefix()}-macos{_MANIFEST_ARCH_SUFFIX}/latest.json")
CASK_NAME = _flavor.cask()
# The installed bundle's name, branch suffix included: `Fused Render.app`,
# `Fused Render-feature.app`. Names the /Applications target and the parked
# `-old-` siblings below (the staged `-update` sibling is named from the
# bundle's own basename instead — `installed.staged_bundle_path`).
_BUNDLE_NAME = _flavor.bundle_name() + branch_suffix()
# What the bundle was called before (`FusedRender.app`), branch suffix
# included. An install from before the rename keeps updating in place under
# that name (staging is basename-derived) until `apply_staged` moves it to
# `_BUNDLE_NAME`, and its `-old-` leftovers still carry it.
_LEGACY_BUNDLE_NAMES = tuple(name + branch_suffix()
                             for name in _flavor.legacy_bundle_names())
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
CANCELLED_MESSAGE = _base.CANCELLED_MESSAGE
_DISK_SPACE_FACTOR = _base._DISK_SPACE_FACTOR
# The first check runs right after boot, so the sidebar badge (UpdateBadge,
# 60s idle poll on top of this) appears on the first poll rather than a minute
# or two into the session. The check loop is a background thread, so it is
# off the startup path anyway and the delay only keeps the manifest fetch out
# of a booting process's first tick. Every check after it is common.CHECK_INTERVAL_S (5 min) apart.
MAC_STARTUP_DELAY_S = 1.0
_DOWNLOAD_PREFIX = _flavor.app_name() + "-"
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
    for name in (_BUNDLE_NAME, *_LEGACY_BUNDLE_NAMES):
        if bundle_real == os.path.realpath(f"/Applications/{name}.app"):
            return "brew"
    return "dmg"


# `apply_staged` renames the running bundle to this sibling — pid of the
# process that applied, then a nanosecond clock so two applies by ONE pid
# never share a name (a recycled pid, or a boot that applies and then quits
# again) — and LEAVES IT THERE. It is removed by `sweep_old_bundles` on a
# later boot, once that pid is gone — never by the process still running
# from it.
def _old_bundle_re() -> re.Pattern[str]:
    """The hidden-sibling shape the swap writes, group 1 the swapping pid.
    Built per call, not at import: `_BUNDLE_NAME` carries the branch suffix
    and tests rebind it. The stamp is optional so leftovers written before
    it existed (`-old-<pid>.app`) are matched too, and so are leftovers
    named for a legacy bundle name (`.FusedRender-old-<pid>.app`)."""
    names = "|".join(re.escape(name) for name in (_BUNDLE_NAME, *_LEGACY_BUNDLE_NAMES))
    return re.compile(
        r"^\.(?:" + names + r")-old-(\d+)(?:-\d+)?\.app$")


def _old_bundle_path(parent: str, pid: int) -> str:
    """Where an apply parks the bundle it replaces: unique per call."""
    return os.path.join(parent, f".{_BUNDLE_NAME}-old-{pid}-{time.time_ns()}.app")


def staged_version(bundle: str) -> str | None:
    """The version staged beside `bundle` and newer than it, else None —
    `installed.staged_version` against the RESOLVED bundle, which is where
    `_install_dmg` stages (a Caskroom symlink's target, not the link)."""
    return installed.staged_version(os.path.realpath(bundle))


def _partial_path(staged: str) -> str:
    """Where `_install_dmg` dittos BEFORE the copy becomes the staged bundle.
    Not a `.app` name: Launch Services never sees it, and no reader of
    `installed.staged_bundle_path` can mistake it for a finished copy."""
    return staged + ".partial"


def _superseded_path(staged: str) -> str:
    """Where `_install_dmg` moves an OLDER staged copy before deleting it.
    Same reason as `_partial_path`: the staged name is only ever renamed
    to or from, never deleted in place — an rmtree takes seconds on 13k
    files, and a quit landing inside it would apply the torn remainder."""
    return staged + ".superseded"


# Serialises everything that writes the `.partial` / `.superseded` siblings:
# `_install_dmg` (the install thread) against `discard_stale_staged` (the
# boot sweep thread). Without it the sweep could delete a `.partial` an
# install is dittoing into, or claim the staged name back from
# `.superseded` inside an install's publish window (CodeRabbit, PR #2088).
# `apply_staged` deliberately does NOT take it: it only ever touches the
# staged name, which is atomically renamed to or from, and a quit must not
# wait out a 30 s ditto on the way to the watchdog's hard deadline.
_stage_lock = threading.Lock()

# The one-call atomic exchange of two paths: macOS `renameatx_np(2)` with
# RENAME_SWAP (APFS, HFS+), Linux `renameat2(2)` with RENAME_EXCHANGE
# (glibc >= 2.28; ext4, xfs, btrfs, tmpfs — where the tests run). Both flags
# happen to be 0x2; AT_FDCWD differs per libc.
_EXCHANGE_FLAG = 0x2
_EXCHANGE_CALL, _AT_FDCWD = (
    ("renameatx_np", -2) if sys.platform == "darwin" else ("renameat2", -100))
_exchange_fn = None


def _swap_paths(a: str, b: str) -> None:
    """Atomically exchange `a` and `b`, or raise OSError with NOTHING moved.
    ENOTSUP when the libc has no such call or the volume refuses the
    exchange (also EINVAL/EXDEV from the kernel); any other errno (EPERM,
    ENOENT…) as `os.rename` would raise it. THERE IS NO TWO-RENAME FALLBACK:
    a park-then-promote has an instant with nothing at the app's path, and
    the quit watchdog's `os._exit` can land in it (CodeRabbit, PR #2088) —
    so where the exchange is unavailable the bundle is left alone."""
    global _exchange_fn
    if _exchange_fn is None:
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            fn = getattr(libc, _EXCHANGE_CALL)
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
            fn.restype = ctypes.c_int
            _exchange_fn = fn
        except (OSError, AttributeError):
            _exchange_fn = False
    if _exchange_fn is False:
        raise OSError(errno.ENOTSUP, f"{_EXCHANGE_CALL} is not available", a, None, b)
    rc = _exchange_fn(_AT_FDCWD, os.fsencode(a), _AT_FDCWD, os.fsencode(b),
                      _EXCHANGE_FLAG)
    if rc == 0:
        return
    code = ctypes.get_errno()
    raise OSError(code, os.strerror(code), a, None, b)


def discard_stale_staged(bundle: str) -> bool:
    """Remove what no quit should ever apply: a staged sibling that is NOT
    newer than `bundle` — the apply already happened (a crash between the
    rename and the exit, say), or the user dragged a newer DMG over the app
    while an older download sat staged — and a `.partial` copy a process
    died in the middle of writing. Run from `start()` on boot, off the
    critical path. True when something was removed."""
    real = os.path.realpath(bundle)
    staged = installed.staged_bundle_path(real)
    removed = False
    with _stage_lock:
        # A process that died between an install's two publish renames
        # left the only complete copy under `.superseded`: give it the
        # staged name back before anything is judged or removed. If that
        # rename fails the copy stays put for a later attempt.
        superseded = _superseded_path(staged)
        if os.path.isdir(superseded) and not os.path.exists(staged):
            try:
                os.rename(superseded, staged)
                logger.info("update: restored %s from %s", staged, superseded)
            except OSError:
                logger.warning("update: could not restore %s", superseded,
                               exc_info=True)
        for leftover in (_partial_path(staged), superseded):
            if not os.path.isdir(leftover):
                continue
            try:
                shutil.rmtree(leftover)
                logger.info("update: removed leftover staged copy %s", leftover)
                removed = True
            except OSError:
                logger.warning("update: could not remove %s", leftover, exc_info=True)
        if not os.path.isdir(staged):
            return removed
        # Only a staged copy PROVABLY not newer goes: an unreadable installed
        # plist is not a verdict (`installed.staged_version` fails closed for
        # the apply; the mirror here is to keep the copy, not delete it).
        current = installed.bundle_version(real)
        if current is None or installed.staged_version(real) is not None:
            return removed
        try:
            shutil.rmtree(staged)
        except OSError:
            logger.warning("update: could not remove stale staged bundle %s", staged,
                           exc_info=True)
            return removed
    logger.info("update: removed stale staged bundle %s (not newer than %s)",
                staged, current)
    return True


def apply_staged(bundle: str, *, pid: int | None = None,
                 rename_legacy: bool = False) -> str | None:
    """Rename the staged sibling into place of `bundle`: the one moment the
    bundle on disk changes. Returns the version now installed, or None when
    there was nothing (newer) to apply or the apply failed — the running
    bundle is left exactly as it was in every failure case.

    Called from the quit teardown (app.quit_teardown's "update" rung), after
    the server has drained and the children rung has run: nothing is about
    to read from the bundle but this process, and it is about to `os._exit`.
    (A child the children rung could not confirm dead keeps its open files
    on the renamed inode — the situation every child was in, for hours, under
    the old install-time swap.) That is what makes the rename safe where it
    was not at install time (see the module docstring).

    ONE atomic exchange, and only that (`_swap_paths`: `renameatx_np` on
    macOS, `renameat2` on Linux): the bundle and the staged copy trade names
    in a single call, so there is no instant at which the app's path is
    empty — the quit watchdog's `os._exit` can land anywhere and leave either
    the old app or the new one, never nothing (CodeRabbit, PR #2088). A
    park-then-promote pair of renames would have exactly that instant, so
    there is no such fallback: where the volume cannot exchange, the bundle
    is left alone. The displaced bundle lands under the staged name, which a
    best-effort third rename parks under `_old_bundle_path` for a later
    boot's `sweep_old_bundles` (see that docstring for the TCC reason); if
    that rename never runs, the boot sweep removes it from the staged name
    as "not newer" instead.

    A failure to exchange discards the staged copy: a volume that refuses
    today refuses the next quit the same way, and `installed_version()`
    would otherwise keep raising a restart card whose restart changes
    nothing. The next check offers the update again, and that install's
    own writability check (or this log line) says why.

    `rename_legacy=True` also moves a bundle still under a legacy name
    (`FusedRender.app`) to `_BUNDLE_NAME` (`Fused Render.app`) once the new
    version is in place — `_rename_legacy_bundle`. The caller passes it only
    for installs nothing else manages (not Homebrew's, whose receipt names
    the old path)."""
    real = os.path.realpath(bundle)
    version = installed.staged_version(real)
    if version is None:
        return None
    staged = installed.staged_bundle_path(real)
    parent = os.path.dirname(real)
    old = _old_bundle_path(parent, os.getpid() if pid is None else pid)
    try:
        _swap_paths(real, staged)
    except OSError as error:
        # ENOENT is not a refusal: the install thread is mid-publish (it
        # just moved the older copy to `.superseded` and is about to rename
        # `.partial` into the staged name), and an rmtree here could land on
        # the complete new copy a moment later. Nothing to discard; the
        # next quit sees whatever the publish leaves.
        if error.errno == errno.ENOENT:
            logger.info("update: nothing at %s to apply yet (an install is "
                        "publishing); leaving it for the next quit", staged)
            return None
        logger.warning("update: could not exchange %s with staged v%s (%s); the "
                       "bundle is untouched and the staged copy is discarded",
                       real, version, error.strerror, exc_info=True)
        shutil.rmtree(staged, ignore_errors=True)
        return None
    # The bundle that was running is now under the staged name. Re-read
    # it: an install that landed from OUTSIDE between the check above and
    # the exchange (a DMG drag, brew) is newer than what was staged, and
    # must win — swap straight back.
    displaced = installed.bundle_version(staged)
    try:
        outran = displaced is not None and not common.is_newer(version, displaced)
    except ValueError:
        outran = False
    if outran:
        logger.info("update: %s became v%s under the staged v%s; keeping it",
                    real, displaced, version)
        try:
            _swap_paths(real, staged)
        except OSError:
            logger.error("update: could not swap back; v%s is at %s", displaced,
                         staged, exc_info=True)
        return None
    try:
        os.rename(staged, old)
    except OSError:
        # Left under the staged name: not newer than the bundle now, so
        # the next boot's `discard_stale_staged` removes it.
        logger.warning("update: applied v%s; could not park the previous "
                       "bundle (left at %s)", version, staged, exc_info=True)
        old = staged
    logger.info("update: applied staged v%s to %s (previous bundle parked at %s)",
                version, real, old)
    if rename_legacy:
        _rename_legacy_bundle(real)
    return version


def renamed_bundle_path(bundle: str) -> str | None:
    """Where `_rename_legacy_bundle` moves `bundle`: the same folder under
    `_BUNDLE_NAME`, or None when `bundle` is not under a legacy name. The
    relauncher opens this path when the one it was given is gone."""
    parent, name = os.path.split(bundle.rstrip(os.sep))
    if not name.endswith(".app") or name[:-4] not in _LEGACY_BUNDLE_NAMES:
        return None
    return os.path.join(parent, f"{_BUNDLE_NAME}.app")


def _rename_legacy_bundle(real: str) -> str | None:
    """Move a just-updated bundle from its legacy name to `_BUNDLE_NAME`
    (`/Applications/FusedRender.app` -> `/Applications/Fused Render.app`), so
    Finder, the Dock and System Settings show the current name. Returns the
    new path, or None when nothing moved: the bundle is not under a legacy
    name, something already sits at the new path (a DMG drag of a newer
    release — `retire_legacy_siblings` handles that copy instead), or the
    rename failed. Best effort and never raises: the app stays fully usable
    at the old path.

    Safe at this point for the same reason the apply is: the server has
    drained, the children are gone, and this process is about to `os._exit`.
    TCC grants follow the bundle id and signature, not the path; the
    relauncher (app.spawn_relauncher) falls back to the new path, and
    schedule_wake rewrites its LaunchAgent on the next boot."""
    target = renamed_bundle_path(real)
    if target is None:
        return None
    if os.path.lexists(target):
        logger.info("update: kept %s under its old name: %s already exists", real, target)
        return None
    try:
        os.rename(real, target)
    except OSError:
        logger.warning("update: could not rename %s to %s", real, target, exc_info=True)
        return None
    logger.info("update: renamed %s to %s", real, target)
    return target


def _bundle_identifier(bundle: str) -> str | None:
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as f:
            value = plistlib.load(f).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException):
        return None
    return value if isinstance(value, str) and value else None


def retire_legacy_siblings(bundle: str, *, executables=None) -> list[str]:
    """Remove a copy of this same app left beside `bundle` under a legacy
    name: someone with `/Applications/FusedRender.app` drags a newer DMG in
    and gets `/Applications/Fused Render.app` next to it — two apps with one
    bundle id, both in Spotlight, Launchpad and "Open With". Returns the
    paths removed.

    Only when `bundle` itself is under the current name, and only a copy
    that is provably the same app (same CFBundleIdentifier), provably not
    newer, not a symlink, and that no running process executes from (an
    empty `ps` answer keeps it). The copy is renamed to the `-old-` shape
    first, so a removal that fails half way is finished by a later boot's
    `sweep_old_bundles`. Never raises."""
    removed: list[str] = []
    try:
        real = os.path.realpath(bundle)
        parent, name = os.path.split(real)
        if name != f"{_BUNDLE_NAME}.app":
            return removed
        own_id = _bundle_identifier(real)
        own_version = installed.bundle_version(real)
        if own_id is None or own_version is None:
            return removed
        if executables is None:
            executables = _running_executables
        running: list[str] | None = None
        for legacy in _LEGACY_BUNDLE_NAMES:
            path = os.path.join(parent, f"{legacy}.app")
            try:
                if os.path.islink(path) or not os.path.isdir(path):
                    continue
                if _bundle_identifier(path) != own_id:
                    continue
                version = installed.bundle_version(path)
                if version is None or common.is_newer(version, own_version):
                    continue
                if running is None:
                    running = executables()
                if not running or _in_use(path, running):
                    continue
                parked = _old_bundle_path(parent, os.getpid())
                os.rename(path, parked)
                shutil.rmtree(parked, ignore_errors=True)
            except Exception:  # noqa: BLE001 - best effort, per copy
                logger.debug("could not retire %s", path, exc_info=True)
                continue
            removed.append(path)
    except Exception:  # noqa: BLE001
        logger.debug("retire_legacy_siblings failed for %s", bundle, exc_info=True)
    if removed:
        logger.info("update: removed earlier copies beside %s: %s",
                    bundle, ", ".join(removed))
    return removed


def _running_executables(run=subprocess.run) -> list[str]:
    """Every live process's executable path, from `ps`. Empty on any
    failure (a real listing always holds at least `ps` itself) — the caller
    then has no second opinion and keeps every bundle."""
    try:
        result = run(["/bin/ps", "-axo", "comm="], capture_output=True,
                     text=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _in_use(bundle: str, executables: list[str]) -> bool:
    """True when any running executable lives inside `bundle` — an app
    launched from the hidden sibling itself, or anything it spawned. Limits,
    stated: `ps` reports the path a process was exec'd under, so a worker
    spawned BEFORE the swap still reads `/Applications/<Bundle>.app/…` after
    its bundle was renamed away, and is not caught here; the app's own
    shutdown reaps those, so the gap is a crash-then-relaunch-before-the-
    children-die window, and the dead-pid check is what actually guards
    the common case."""
    prefix = bundle.rstrip(os.sep) + os.sep
    return any(exe.startswith(prefix) for exe in executables)


def _pid_alive(pid: int) -> bool:
    """False only when the kernel says there is no such process. Anything
    else — a pid of another user's (EPERM), a pid too large for the call,
    an unexpected error — counts as alive: the cost of a wrong "alive" is a
    bundle that waits one more boot; the cost of a wrong "dead" is deleting
    the code a running app is executing."""
    if pid <= 0:
        return True  # 0 / negatives address process groups, never one process
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError, ValueError):
        return True
    return True


def sweep_old_bundles(parent: str, *, keep: str | None = None,
                      alive=_pid_alive,
                      executables=_running_executables) -> list[str]:
    """Remove the `.<Bundle>-old-<pid>[-<ns>].app` siblings earlier swaps
    left in `parent`, for every pid that is no longer running and no
    process still executes from. Returns the paths it removed. `keep` is
    the caller's own resolved bundle, never removed whatever its name.

    Why the old bundle must outlive the process that swapped it away
    (verified 2026-10-15, FusedRender 0.6.46 -> 0.6.48 on macOS 26): TCC
    keys Full Disk Access (and every other per-app grant) on the responsible
    process's on-disk code, and re-validates it on each protected open. The
    swap used to `rmtree` the renamed bundle straight away, on the theory
    that open files keep working on unlinked inodes — true for the files the
    process already holds, but the executable path itself was gone, so from
    that moment every `~/Desktop`, `~/Documents` … read failed with
    EPERM (`/api/fs/list -> 403`, git "Unable to read current working
    directory") until the user quit and relaunched. Toggling FDA off and on
    did nothing: the grant was fine, the process could no longer prove who
    it was. So: the process that swapped keeps its bundle, and the NEXT
    process to start sweeps whatever the dead ones left behind.

    Scoped to our own `_BUNDLE_NAME` and legacy names (other apps share
    /Applications and their leftovers are not ours to sweep), to the
    exact hidden-sibling shape the swap writes, and to pids that `alive`
    says are gone. A recycled pid only ever makes the sweep SKIP a bundle
    (deleted on a later boot), never delete one a live process is running
    from. The pid in the name is the process that did the swap, not the
    only thing that can be running from the bundle (CodeRabbit, PR #1667):
    a child worker that outlived it, or — someone double-clicking the
    hidden sibling — a whole app launched from it, whose own `start()`
    would otherwise sweep the code it is executing. So the caller's own
    bundle is excluded by resolved path, and every candidate is checked
    against the executables `ps` lists before it goes. An empty `ps` answer
    keeps everything. The sweep is best-effort: an unreadable `parent`, a bundle that
    will not delete, or a name that will not parse costs nothing but that
    one entry. It runs regardless of FUSED_RENDER_NO_AUTO_UPDATE — that
    flag is about not checking for updates, and the leftovers are from
    installs that already happened."""
    removed: list[str] = []
    try:
        names = os.listdir(parent)
    except OSError:
        return removed
    pattern = _old_bundle_re()
    keep_real = os.path.realpath(keep) if keep else None
    running: list[str] | None = None  # one `ps`, and only if a candidate needs it
    for name in names:
        try:
            match = pattern.match(name)
            if match is None:
                continue
            pid = int(match.group(1))
            if pid == os.getpid() or alive(pid):
                continue
            full = os.path.join(parent, name)
            if os.path.islink(full) or not os.path.isdir(full):
                continue  # not something the swap wrote
            if keep_real is not None and os.path.realpath(full) == keep_real:
                continue  # the bundle this very process runs from
            if running is None:
                running = executables()
            if not running or _in_use(full, running):
                continue  # `ps` failed, or something still runs from it
            shutil.rmtree(full)
        except Exception:  # noqa: BLE001 - one entry must never end the sweep
            logger.debug("could not sweep old bundle %s in %s", name, parent,
                         exc_info=True)
            continue
        removed.append(full)
    if removed:
        logger.info("update: removed %d old bundle(s) left by earlier swaps: %s",
                    len(removed), ", ".join(removed))
    return removed


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
    "installed" means the new version is what launches next — staged beside
    the bundle (ours, applied on quit) or already in place (a manual drag,
    brew); the existing installed_version drift banner drives the restart
    from there. Every method takes the same route through those states: the
    DMG stage is the one install path, and brew is never invoked — the cask's
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

    def known_method(self) -> str | None:
        """`method()` if it has been worked out already, else None — never
        runs brew. For the quit path, which must not wait on it."""
        return self._method

    def _disk_version(self) -> str | None:
        """What would launch next time: the version staged beside the bundle
        when it is newer, else the bundle's own CFBundleShortVersionString
        (same read as installed.installed_version, but against this manager's
        bundle path so tests can point it at a fixture). The staged read is
        what flips status() to "installed" once `_install_dmg` has staged —
        and back to "available" when a later manifest outruns the staged
        copy, which `_install_dmg` then replaces."""
        if self._bundle is None:
            return None
        staged = staged_version(self._bundle)
        if staged is not None:
            return staged
        return installed.bundle_version(self._bundle)

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
        # bundle reached through a symlink (a /Applications/Fused Render.app
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
        # Downloading is over: from here on the staged copy is being written,
        # and there is no point between the ditto and the spawn probe where
        # stopping would leave anything better than finishing does — so the
        # row drops its Cancel and its numbers (no honest total exists for a
        # copy) and says what it is doing instead.
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
        # The staged sibling: `installed.staged_bundle_path` of the RESOLVED
        # bundle, so installed_version() (which resolves too) and the quit
        # teardown's `apply_staged` find it where this put it.
        staged = installed.staged_bundle_path(bundle)
        # The copy is written under a name NO reader of `staged` looks at,
        # and takes the staged name in one atomic rename at the very end.
        # `Info.plist` lands early in a 15-40 s ditto, and three readers
        # trust a readable staged plist — installed_version() (the restart
        # card), `_disk_version()` and the quit's `apply_staged` — so a ⌘Q
        # or a Restart click mid-copy would otherwise rename a half-written
        # app into /Applications. The staged path is either absent or
        # complete, never in between.
        swap_in = _partial_path(staged)
        # Held from the first write to the `.partial` name through the
        # publish: the boot sweep (`discard_stale_staged`) takes the same
        # lock before it removes a `.partial` or restores a `.superseded`.
        _stage_lock.acquire()
        try:
            mount = self._attach(dmg)
            source = self._find_app(mount)
            self._verify_app_version(source, manifest["version"])
            if os.path.exists(swap_in):
                shutil.rmtree(swap_in)  # a copy an earlier process died in
            # ditto straight from the mounted image into the bundle's own
            # parent dir: it preserves the code signature, resource forks and
            # xattrs a plain copy can drop (a stripped signature would leave a
            # bundle Gatekeeper refuses to launch), and landing on the target
            # volume makes the rename below and `apply_staged`'s two
            # same-volume renames.
            subprocess.run(["/usr/bin/ditto", source, swap_in],
                           check=True, capture_output=True, timeout=600)
            # Prove the staged copy's python SPAWNS before it becomes the
            # app. A 0.6.84 reached this way on macOS 26.6 (2026-10-23) booted
            # — the launcher never execs `Contents/MacOS/python` — and then
            # failed every `posix_spawn` of it with EACCES for six boots:
            # Claude, the index, the engine, all dead behind a live window.
            # The same image on another Mac spawned fine, so whatever that
            # machine does to the copy is only visible by trying it, here,
            # while the installed bundle is still intact. `ensure_spawnable`
            # logs the file's mode/owner/xattrs, sets 0755 where it can, and
            # tries again; when it still refuses, the update is NOT staged —
            # the `finally` below removes the copy and `_install` shows the
            # error — rather than left for the quit to swap in and produce a
            # working-looking app that cannot run anything.
            reason = bundle_exec.ensure_spawnable(
                os.path.join(swap_in, "Contents"), what="update")
            if reason is not None:
                raise RuntimeError(
                    "the downloaded app's Python cannot be run on this Mac "
                    f"({reason}); the installed app was left as it was — "
                    "download the DMG from fused.io and drag it to Applications")
            # A copy already staged (an earlier download the user has not
            # restarted for) is superseded by this one: the manifest moved
            # on, so the next quit should apply the newest, not the first.
            # Moved aside only now, with the replacement complete and
            # probed, and by RENAME, never deleted where it stands: the quit
            # teardown's `apply_staged` can run on another thread at any
            # moment, and an rmtree of 13k files leaves the staged name
            # pointing at a torn bundle for seconds (bugbot, PR #2088). A
            # quit landing between the two renames applies nothing, which
            # beats applying either the older copy or half of it.
            superseded = _superseded_path(staged)
            if os.path.exists(superseded):
                shutil.rmtree(superseded)
            if os.path.exists(staged):
                os.rename(staged, superseded)
            try:
                os.rename(swap_in, staged)
            except OSError:
                # The older copy is still complete under `.superseded`:
                # give it its name back (a quit can still apply it) before
                # the failure is reported. The boot sweep does the same for
                # a process that died here.
                if os.path.exists(superseded):
                    try:
                        os.rename(superseded, staged)
                    except OSError:
                        logger.warning("update: could not restore %s", superseded,
                                       exc_info=True)
                raise
            if os.path.exists(superseded):
                shutil.rmtree(superseded, ignore_errors=True)
            # That is the install: the copy waits here, next to the bundle,
            # until `apply_staged` renames it into place on quit. Nothing on
            # disk that this process, its windows or its children read has
            # moved. `_disk_version()` reads the staged plist from now on,
            # which is what turns the state "installed" and raises the
            # restart card.
            logger.info("update: staged v%s at %s; applied on the next quit",
                        manifest["version"], staged)
        finally:
            if mount is not None:
                subprocess.run(["/usr/bin/hdiutil", "detach", mount, "-quiet"],
                               check=False, capture_output=True, timeout=60)
            common.discard(dmg)
            # Gone on every failure path; already renamed away on success.
            if os.path.exists(swap_in):
                shutil.rmtree(swap_in, ignore_errors=True)
            _stage_lock.release()
            # Last, so the row is still being kept alive through the detach
            # and the cleanup above — `_install` deals with the row next
            # (removing it on success, a terminal report on a failure), and
            # only once the beat has actually stopped: a beat still mid-report
            # could otherwise re-create the row it just took, or overwrite a
            # failed one with "Installing".
            beat_stop.set()
            beat.join(timeout=INSTALL_HEARTBEAT_S + 5)

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
                info = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException) as error:
            raise RuntimeError("update app bundle has no readable Info.plist") from error
        found = info.get("CFBundleShortVersionString")
        if found != version:
            raise RuntimeError(
                f"update image contains version {found}, expected {version}")
        # The same key signed the retired FusedBot.app's manifests, so also pin
        # the app identity: never swap another app into /Applications off a
        # mispublished or swapped manifest.
        bundle = str(info.get("CFBundleIdentifier") or "")
        want = _flavor.bundle_id()
        if bundle != want and not bundle.startswith(want + "."):
            raise RuntimeError(
                f"update image is {bundle or 'unidentified'}, expected {want}")


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
    gets no badge at all rather than an install that can only fail. Also a
    no-op when `cryptography` is not installed (a lean/wheel-only install,
    e.g. `pip install fused-render` with no extras): the updater can never
    verify a manifest without it, and a pip install updates via
    `pip install -U fused-render` instead, so "no updater" is the correct
    behaviour here, not a crash — same convention as the "nothing to swap"
    case just below."""
    global _manager
    with _manager_lock:
        if _manager is None:
            if not common.CRYPTO_AVAILABLE:
                logger.warning(
                    "cryptography is not installed; the in-app updater is "
                    "unavailable on this install (use `pip install -U "
                    "fused-render` to update)")
                return None
            if bundle_path() is None:
                # ...unless a dev run asked for a manager that only looks
                # (DEV_MANAGER_ENV, above): same loop, same throttle, same
                # states, no swap.
                if not os.environ.get(DEV_MANAGER_ENV):
                    return None
                _manager = UpdateManager(bundle=None, method="none", check_only=True)
            else:
                _manager = UpdateManager()
                # Earlier sessions' swapped-away bundles, now that the
                # processes that ran from them are gone. A worker: it is an
                # rmtree of a few hundred MB on the boot path otherwise. Only
                # the packaged app, by its own bundle's RESOLVED parent (the
                # directory `_install_dmg` swaps in, which for a symlinked
                # bundle is not the link's) — a dev run has no swaps to clean
                # up after and no /Applications to look in.
                own = os.path.realpath(_manager._bundle)

                def _sweep(own=own):
                    sweep_old_bundles(os.path.dirname(own), keep=own)
                    # A FusedRender.app a DMG drag of "Fused Render.app"
                    # left beside it (the bundle was renamed).
                    retire_legacy_siblings(own)
                    # A staged copy that is no longer newer than the bundle
                    # it sits beside is spent (applied, then the process
                    # died before `os._exit`; or superseded by a manual
                    # drag) — a stale one must never be applied on quit.
                    discard_stale_staged(own)

                threading.Thread(target=_sweep, daemon=True,
                                 name="fused-render-update-sweep-old").start()
            _manager.start_auto_checks()
        return _manager
