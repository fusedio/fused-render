"""The user's OWN "choose a folder" dialog, raised from the server process.

A template that has to write something somewhere needs a destination, and the
honest way to ask for one is the dialog the operating system already provides:
NSOpenPanel, IFileDialog, zenity. Every alternative is worse. The app's UI runs
in the user's real default browser (`webbrowser.open`, app.py), not an app-owned
webview, so `webkitdirectory` and `showDirectoryPicker` are both dead ends —
a browser deliberately strips the absolute path, and every `/api/fs/*` endpoint
requires `os.path.isabs`. Hence this: the dialog is raised by the process that
already has filesystem authority, and the path never leaves it.

Which backend a machine gets (`_choose_backend`, a pure function so it can be
tested as one):

* **macOS, packaged app** → `appkit`. uvicorn runs on a daemon thread inside the
  same rumps/AppKit process (app.py's `_start_server_thread`), so there is a
  main thread to hop onto and `menubar_pin` can raise a real NSOpenPanel — the
  app's own dialog, with its sidebar and favourites.
* **macOS, `fused-render serve`** → `osascript`. No AppKit run loop exists, so a
  block posted with `callAfter` would never be delivered; `choose folder` in a
  child process is the established way this server already shells out to
  AppleScript.
* **Windows** → `win32`, in-process on a dedicated STA thread. The tray is a
  SEPARATE process there and `supervisor/protocol.py` has no reply payload, so
  routing the dialog through it could not carry the answer back.
* **Linux** → `linux` (zenity / kdialog / tkinter), but only with a display.
* **anything else, or a headless Linux** → no backend at all, which is what
  `/api/config`'s `native_dir_picker: false` tells the templates so they use the
  in-page dialog instead of waiting on a modal nobody can see.

Two invariants every backend shares, because a modal dialog is not an ordinary
subprocess:

* **a cancel is an answer.** It returns None and reads as a cancel all the way
  out to the page. Reporting it as an error makes the page pop a second,
  different chooser at someone who just said no.
* **one dialog at a time, and never forever.** A non-blocking claim refuses a
  second request rather than stacking modals, and every wait is bounded — an
  unanswered dialog must fail the request instead of pinning a threadpool
  worker for the life of the process.

  Those two pull against each other when a wait times out over a dialog that is
  STILL UP, which the in-process backends cannot take down. Freeing the claim
  there stacks a second modal on the first; keeping it forever wedges the picker.
  So the claim is handed to the dialog itself and clears when the dialog really
  ends — see the one-dialog claim section below, and `DialogAbandoned`.
"""
import functools
import ntpath
import os
import posixpath
import shutil
import subprocess
import sys
import threading
from pathlib import PureWindowsPath

from fused_render._view_url_codec import _is_drive_path, canonical_fs_path
from fused_render.server.common import logger

# Long enough that a human really can browse to a folder, short enough that a
# dialog nobody ever answers (a forgotten window on another space, a wedged
# helper) fails the request instead of holding a threadpool worker forever.
# A module attribute so tests can shorten it.
_DIALOG_TIMEOUT_S = 300


class PickerUnavailable(RuntimeError):
    """This machine has no folder dialog the server can raise."""


class PickerBusy(RuntimeError):
    """A folder dialog is already open; a second modal must not be stacked."""


class PickerFailed(RuntimeError):
    """The dialog broke. NOT what a user cancelling looks like — see None."""


class DialogAbandoned(RuntimeError):
    """We stopped waiting, but the dialog is STILL ON SCREEN.

    Raised only by the backends that own a modal they cannot take down: the
    Windows STA thread and the AppKit main thread. `finished` is set when that
    dialog really does end, which is what lets `pick_directory` keep refusing new
    requests until then without wedging the picker forever.

    A child-process backend (osascript, zenity) never raises this: subprocess
    kills the child on timeout, so there is no dialog left and a plain
    PickerFailed is the honest answer.
    """

    def __init__(self, finished: threading.Event):
        super().__init__("the folder chooser was not answered in time")
        self.finished = finished


# ------------------------------------------------------------ backend selection


def _appkit_runloop_live() -> bool:
    """Whether this process is an AppKit app with a running run loop.

    The `sys.modules` check comes first and is not just an optimization: it means
    the server process NEVER imports AppKit merely to answer this question. Under
    `fused-render serve` AppKit is absent, and importing it there would drag in
    the whole Cocoa stack (and, on some builds, initialise things a plain CLI
    process has no business initialising) just to be told "no".
    """
    if sys.platform != "darwin" or "AppKit" not in sys.modules:
        return False
    try:
        app = sys.modules["AppKit"].NSApp()
        return bool(app is not None and app.isRunning())
    except Exception:  # noqa: BLE001 - any surprise from Cocoa means "no"
        return False


def _choose_backend(platform: str, environ, *, has_osascript: bool = True,
                    appkit_live: bool = False) -> str:
    """The backend name for a machine, or "" for none. Pure — see the module
    docstring for why each platform lands where it does."""
    if platform == "darwin":
        if appkit_live:
            return "appkit"
        return "osascript" if has_osascript else ""
    # `sys.platform` alone, never `os.name`: reading the ambient os.name here
    # would make this function answer differently on the machine it runs on than
    # on the platform it was asked about — i.e. not pure, and its tests would
    # pass everywhere except on a Windows runner.
    if platform == "win32":
        return "win32"
    if platform.startswith("linux"):
        # No display means no dialog: a hosted or headless deploy must advertise
        # the capability as false rather than open something nobody can dismiss.
        if environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"):
            return "linux"
    return ""


@functools.cache
def _has_osascript() -> bool:
    # Cached: `available()` is read on every /api/config, which every page load
    # makes, and a PATH walk per request buys nothing — /usr/bin/osascript does
    # not come and go inside one process. The AppKit half is NOT cached: whether
    # a run loop is up is exactly the thing that can change.
    return shutil.which("osascript") is not None


def _backend() -> str:
    return _choose_backend(
        sys.platform,
        os.environ,
        has_osascript=_has_osascript(),
        appkit_live=_appkit_runloop_live(),
    )


def available() -> bool:
    """Whether `pick_directory` can actually raise a dialog on this machine.
    Reported to the browser as `/api/config`'s `native_dir_picker`."""
    return bool(_backend())


# ------------------------------------------------------------- macOS: AppKit


def _pick_appkit(start, title):
    from fused_render import menubar_pin

    try:
        return menubar_pin.choose_directory(
            start=start, title=title, timeout=_DIALOG_TIMEOUT_S)
    except menubar_pin.PanelNotAnswered as exc:
        # The panel belongs to the AppKit main thread and there is nothing here
        # that could dismiss it, so this is not "the dialog failed" — it is "the
        # dialog is still up and we are no longer waiting".
        raise DialogAbandoned(exc.finished) from exc


# ---------------------------------------------------------- macOS: osascript


def _as_applescript_string(text: str) -> str:
    """An AppleScript string literal. A path can be user input, and an
    unescaped quote or backslash in one turns a single statement into two."""
    escaped = str(text).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _osascript_script(start, title) -> str:
    """The `choose folder` one-liner. Separate from running it so a test can
    swap in a script that needs no human and still exercise the real
    subprocess."""
    parts = ["choose folder with prompt ", _as_applescript_string(title)]
    if start:
        parts += [" default location POSIX file ", _as_applescript_string(start)]
    return "POSIX path of (" + "".join(parts) + ")"


def _pick_osascript(start, title):
    script = _osascript_script(start, title)
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_DIALOG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has already killed the child; say what happened rather
        # than letting the request look like a cancel.
        raise PickerFailed("the folder chooser was not answered in time") from exc
    except OSError as exc:
        raise PickerFailed(f"could not run osascript: {exc}") from exc
    if proc.returncode == 0:
        return proc.stdout.decode("utf-8", "replace").strip() or None
    stderr = proc.stderr.decode("utf-8", "replace").strip()
    # -128 is AppleScript's "user cancelled". That is an answer, not a fault.
    if "-128" in stderr:
        return None
    raise PickerFailed(stderr or f"osascript exited with status {proc.returncode}")


# --------------------------------------------------------------- Linux / Windows
# Both live with their platform's other native UI, behind the same `_backend`
# seam the supervisor uses, so this module holds no per-OS dialog code of its
# own. They raise OSError for "the dialog broke" and return None for a cancel.


def _pick_linux(start, title):
    from fused_render.supervisor._linux.ui import pick_directory

    return pick_directory(title=title, start=start)


def _pick_win32(start, title):
    """The Windows shell dialog, on its own STA thread.

    `IFileDialog` pumps its own message loop and shell extensions need a
    single-threaded apartment, so it cannot run on a threadpool worker that
    something else has already CoInitialized differently — the same discipline
    `supervisor/core.py` documents around `pick_file`. The lock in
    `pick_directory` is the single-dialog half of it.
    """
    from fused_render.supervisor._win32.ui import pick_directory

    cell = {}
    # Set by the dialog thread itself, whatever the outcome. On the timeout path
    # this is the ONLY thing that can tell anyone the modal has gone: nothing here
    # can dismiss an IFileDialog pumping its own message loop.
    finished = threading.Event()

    def run():
        try:
            cell["path"] = pick_directory(title=title, start=start)
        except BaseException as exc:  # noqa: BLE001 - carried to the caller below
            cell["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True, name="fused-dir-dialog")
    thread.start()
    thread.join(_DIALOG_TIMEOUT_S)
    if thread.is_alive():
        raise DialogAbandoned(finished)
    if "error" in cell:
        raise cell["error"]
    return cell.get("path")


# A dict, not an if-chain, so a test can substitute one backend without
# monkeypatching platform detection as well.
_BACKENDS = {
    "appkit": _pick_appkit,
    "osascript": _pick_osascript,
    "linux": _pick_linux,
    "win32": _pick_win32,
}


# --------------------------------------------------------------- the one-dialog claim
# There is one screen, so this is necessarily module state.
#
# INVARIANT: at most one dialog is outstanding, and the claim on it is held by
# whoever can observe that dialog end. `_outstanding` is None when no dialog is
# up; otherwise it is an Event that becomes set when the outstanding dialog
# finishes. For a dialog we are still waiting on, that Event is one nobody sets —
# the waiter clears the slot itself when its wait returns. For a dialog we GAVE UP
# on, it is the dialog's own completion Event, so the claim clears when the modal
# really goes away and not a moment sooner.
#
# Both halves matter. Releasing on the timeout path stacks a second modal over the
# first, unanswered one — exactly what the claim exists to prevent. Never
# releasing wedges the picker for the rest of the process after a single
# unanswered dialog.
_state = threading.Lock()   # guards `_outstanding`; never held while a dialog is up
_outstanding: threading.Event | None = None


def _claim() -> threading.Event:
    """Claim the right to put a dialog up, or raise PickerBusy.

    Non-blocking by design: a second request must be REFUSED, not queued.
    Queuing would let a page fire two dialogs and then wait on the second for as
    long as the first stays open, which is indistinguishable from a hang.
    """
    global _outstanding
    with _state:
        if _outstanding is not None and not _outstanding.is_set():
            raise PickerBusy("a folder chooser is already open")
        _outstanding = threading.Event()
        return _outstanding


def _unclaim(claim: threading.Event, still_up: threading.Event | None) -> None:
    """Give the claim back. With `still_up`, the dialog outlived our wait, so the
    claim passes to it instead of being freed."""
    global _outstanding
    with _state:
        # Identity-checked: if something else already took the slot over (only
        # possible after we handed it away), leave it alone.
        if _outstanding is claim:
            _outstanding = still_up


def _forget_outstanding_dialog() -> None:
    """Drop any claim. For tests only — a real dialog is never forgotten, it
    finishes."""
    global _outstanding
    with _state:
        _outstanding = None


# ----------------------------------------------------------------- the entry point


def _normpath_for_shape(path: str) -> str:
    """`normpath` chosen by the SHAPE of the path, not by the host OS.

    `os.path.normpath` is the host's: on POSIX it cannot see that a backslash is a
    separator, so it leaves `C:\\Users\\ada\\code\\` with its trailing separator
    and its `..` uncollapsed. That is invisible in production (a Windows path is
    produced on Windows) and precisely why it is worth not depending on — it is
    the same host-dependence that made the backslash bug itself invisible, and it
    would silently weaken every test of this function written anywhere else.

    A path with a Windows drive — a letter or a UNC share — is normalized as one;
    anything else as POSIX, where a backslash stays the legal filename character
    it is.
    """
    if PureWindowsPath(path).drive:
        return ntpath.normpath(path)
    return posixpath.normpath(path)


def _canonical_absolute(chosen: str) -> str:
    """The form a picked folder is handed on in: canonical, and really absolute.

    Three steps, in this order and for three different reasons.

    `normpath` first: it is what drops the trailing slash AppleScript's `POSIX
    path of` puts on a folder, and collapses any `..` a backend hands back. On a
    Windows path it is also what makes every separator a backslash — so
    canonicalizing after it is the only order that catches them all.

    `canonical_fs_path` second. Every path above the OS in this app is
    forward-slashed: it is what a /view URL decodes to, what the runtime sends as
    X-Fused-Page, and what `/api/config` already applies this same call to for
    `calls_dir` with the same note attached. The shell and folder-picker.js's
    `parent`/`join` both assume it, so a backslashed path here would feed mixed
    separators into clone-destination naming and every /api/fs/* call after it.
    Conditional, not a blind replace: on POSIX a backslash is a legal filename
    character. A UNC path is deliberately left alone too — the codec keeps it as
    one opaque segment, and inventing a second convention here would make this the
    only place in the app that disagrees.

    The absolute test comes LAST, and classifies rather than asking the running
    OS. `posixpath.isabs("C:/Users/ada")` is False, and that is exactly the value
    a Windows server produces — so `os.path.isabs` alone would reject the real
    answer anywhere but Windows.
    """
    canonical = canonical_fs_path(_normpath_for_shape(chosen))
    if not (os.path.isabs(canonical) or _is_drive_path(canonical)):
        raise PickerFailed(
            f"the folder chooser answered a relative path: {canonical!r}")
    return canonical


def pick_directory(start: str | None = None,
                   title: str = "Choose a folder") -> str | None:
    """Raise the OS folder dialog and return the chosen absolute path.

    None means the user cancelled. Raises PickerUnavailable (no dialog here),
    PickerBusy (one is already open) or PickerFailed (it broke). Blocks for as
    long as the dialog is up, so callers must be on a thread that may block —
    the endpoint is a sync `def` and therefore runs in the threadpool.
    """
    backend = _backend()
    if not backend:
        raise PickerUnavailable(
            "this system has no folder chooser the app can open")
    claim = _claim()
    still_up = None
    try:
        chosen = _BACKENDS[backend](start, title)
    except DialogAbandoned as exc:
        # The dialog is still on screen. Hand it the claim (see _unclaim) so the
        # next request is refused while it is up and served once it closes.
        still_up = exc.finished
        logger.warning("folder chooser (%s): abandoned, dialog still open", backend)
        raise PickerFailed(str(exc)) from exc
    except (PickerBusy, PickerFailed, PickerUnavailable):
        raise
    except OSError as exc:
        raise PickerFailed(str(exc)) from exc
    finally:
        _unclaim(claim, still_up)
    if chosen is None:
        logger.info("folder chooser (%s): cancelled", backend)
        return None
    chosen = _canonical_absolute(chosen)
    logger.info("folder chooser (%s): %s", backend, chosen)
    return chosen
