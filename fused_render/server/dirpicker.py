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
* **one dialog at a time, and never forever.** A non-blocking lock refuses a
  second request rather than stacking modals, and every wait is bounded — an
  unanswered dialog must fail the request instead of pinning a threadpool
  worker for the life of the process.
"""
import functools
import os
import shutil
import subprocess
import sys
import threading

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

    return menubar_pin.choose_directory(
        start=start, title=title, timeout=_DIALOG_TIMEOUT_S)


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

    def run():
        try:
            cell["path"] = pick_directory(title=title, start=start)
        except BaseException as exc:  # noqa: BLE001 - carried to the caller below
            cell["error"] = exc

    thread = threading.Thread(target=run, daemon=True, name="fused-dir-dialog")
    thread.start()
    thread.join(_DIALOG_TIMEOUT_S)
    if thread.is_alive():
        raise PickerFailed("the folder chooser was not answered in time")
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


# ----------------------------------------------------------------- the entry point

# Non-blocking: a second request must be REFUSED, not queued. Queuing would let
# a page fire two dialogs and then wait on the second for as long as the first
# stays open, which looks identical to a hang.
_lock = threading.Lock()


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
    if not _lock.acquire(blocking=False):
        raise PickerBusy("a folder chooser is already open")
    try:
        chosen = _BACKENDS[backend](start, title)
    except (PickerBusy, PickerFailed, PickerUnavailable):
        raise
    except OSError as exc:
        raise PickerFailed(str(exc)) from exc
    finally:
        _lock.release()
    if chosen is None:
        logger.info("folder chooser (%s): cancelled", backend)
        return None
    # normpath drops the trailing slash `POSIX path of` adds to a folder, and
    # collapses anything else odd a backend hands back.
    chosen = os.path.normpath(chosen)
    if not os.path.isabs(chosen):
        raise PickerFailed(
            f"the folder chooser answered a relative path: {chosen!r}")
    logger.info("folder chooser (%s): %s", backend, chosen)
    return chosen
