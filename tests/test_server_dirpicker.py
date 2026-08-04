"""The native folder chooser: fused_render/server/dirpicker.py + the endpoint.

The point of this module is that the folder picker is the USER'S file dialog —
NSOpenPanel, IFileDialog, zenity — not a div. That means the interesting parts
are all at the seams: which backend a machine gets, whether a cancel is
distinguishable from a failure, and whether a second request can stack a second
modal. Those are what this pins.

What is faked and what is not, deliberately:

* backend SELECTION is a pure function of (platform, environ, what is
  installed), so it is tested as one — no monkeypatching of `sys.platform`.
* the osascript backend runs the REAL `osascript`, with only the script it
  evaluates swapped for one that needs no human (a canned `error number -128`
  for the cancel path, `path to home folder` for the success path). A canned
  `CompletedProcess` would assert our own fiction about pipes, timeouts and
  exit codes — this project has been bitten by exactly that before.
* the Windows and Linux backends are faked (there is no runner for them here),
  and the assertions are about argv and the cancel-vs-error contract.
* the AppKit backend's THREAD hop is tested for real (a background thread, a
  real Event, a real timeout); the panel itself needs a live AppKit run loop,
  which a test process does not have.
"""
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest
from fastapi.responses import JSONResponse

from fused_render.server import dirpicker
from fused_render.server.routers.config import api_config
from fused_render.server.routers.fs_read import api_fs_pick_folder

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
needs_osascript = pytest.mark.skipif(
    shutil.which("osascript") is None, reason="no osascript binary")


def _data(resp) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return resp


def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


# --------------------------------------------------------- backend selection


def test_macos_prefers_the_in_process_panel_when_the_run_loop_is_live():
    # The packaged app: uvicorn is a daemon thread inside the rumps/AppKit
    # process, so there is a main thread to hop onto and NSOpenPanel is the
    # dialog the user expects from the app itself.
    assert dirpicker._choose_backend(
        "darwin", {}, has_osascript=True, appkit_live=True) == "appkit"


def test_macos_without_a_run_loop_falls_back_to_osascript():
    # `fused-render serve`: no AppKit run loop, so callAfter would post a block
    # nothing will ever deliver. osascript raises the dialog out of process.
    assert dirpicker._choose_backend(
        "darwin", {}, has_osascript=True, appkit_live=False) == "osascript"


def test_macos_with_neither_has_no_backend():
    assert dirpicker._choose_backend(
        "darwin", {}, has_osascript=False, appkit_live=False) == ""


def test_windows_always_has_the_shell_dialog():
    assert dirpicker._choose_backend("win32", {}) == "win32"


def test_linux_needs_a_display():
    # The hosted case, and the reason /api/config advertises the capability at
    # all: a headless deploy must never be told to wait on a modal nobody can
    # see or dismiss.
    assert dirpicker._choose_backend("linux", {}) == ""
    assert dirpicker._choose_backend("linux", {"DISPLAY": ":0"}) == "linux"
    assert dirpicker._choose_backend("linux", {"WAYLAND_DISPLAY": "wayland-0"}) == "linux"


def test_availability_is_just_whether_there_is_a_backend(monkeypatch):
    monkeypatch.setattr(dirpicker, "_backend", lambda: "")
    assert dirpicker.available() is False
    monkeypatch.setattr(dirpicker, "_backend", lambda: "osascript")
    assert dirpicker.available() is True


# ------------------------------------------------------------- the AppleScript


def test_the_applescript_asks_for_a_folder_and_starts_where_told():
    script = dirpicker._osascript_script("/Users/ada/code", "Clone into…")
    assert "choose folder" in script
    assert "POSIX path of" in script          # we want a path, not an alias
    assert '"Clone into…"' in script
    assert 'POSIX file "/Users/ada/code"' in script


def test_the_applescript_omits_a_default_location_when_there_is_none():
    assert "default location" not in dirpicker._osascript_script(None, "Pick")


def test_a_quote_in_a_prompt_cannot_break_out_of_the_applescript():
    # A title is not user input today, but a path CAN be, and an unescaped
    # quote or backslash in either turns one string into two statements.
    script = dirpicker._osascript_script('/tmp/we"ird\\dir', 'say "hi"')
    assert r'\"hi\"' in script
    assert r'we\"ird\\dir' in script


# ---------------------------------------------------- osascript, really run
# No CompletedProcess doubles here: only the SCRIPT is swapped, so the pipes,
# the exit code, the stderr parsing and the timeout are the real ones.


@pytest.fixture
def real_osascript(monkeypatch):
    """`pick_directory` wired to the REAL osascript backend, with only the script
    swapped for one a test can answer without a human. Everything else — the
    argv, the pipes, the exit code, the timeout, the single-dialog lock, the
    normalization — is production code."""
    def wire(script):
        monkeypatch.setattr(dirpicker, "_backend", lambda: "osascript")
        monkeypatch.setattr(dirpicker, "_osascript_script",
                            lambda start, title: script)
    return wire


@darwin_only
@needs_osascript
def test_a_real_osascript_user_cancel_reads_as_a_cancel(real_osascript):
    # -128 is what AppleScript raises when the user dismisses `choose folder`,
    # and a cancel must be an ANSWER (None), never an error.
    real_osascript("error number -128")
    assert dirpicker.pick_directory() is None


@darwin_only
@needs_osascript
def test_a_real_osascript_answer_comes_back_as_an_absolute_path(real_osascript):
    real_osascript("POSIX path of (path to home folder)")
    chosen = dirpicker.pick_directory()
    # `POSIX path of` hands back a trailing slash for a folder; the endpoint
    # contract is a plain absolute path, so it is normalized away.
    assert chosen == os.path.expanduser("~")
    assert not chosen.endswith("/")


@darwin_only
@needs_osascript
def test_a_real_osascript_failure_is_an_error_not_a_cancel(real_osascript):
    # A broken script is not the user saying no. Reporting it as a cancel would
    # make the button silently do nothing, forever.
    real_osascript("this is not applescript at all")
    with pytest.raises(dirpicker.PickerFailed):
        dirpicker.pick_directory()


@darwin_only
@needs_osascript
def test_a_dialog_nobody_answers_times_out_instead_of_pinning_the_worker(
        real_osascript, monkeypatch):
    # A modal is answered by a human or not at all, so the wait has to be
    # bounded: a real `delay` outlives the timeout and the child is killed.
    monkeypatch.setattr(dirpicker, "_DIALOG_TIMEOUT_S", 0.5)
    real_osascript("delay 30")
    with pytest.raises(dirpicker.PickerFailed) as caught:
        dirpicker.pick_directory()
    assert "not answered" in str(caught.value)


@darwin_only
@needs_osascript
def test_a_timed_out_dialog_still_hands_back_the_lock(real_osascript, monkeypatch):
    # The bug this guards: releasing the lock only on the happy path leaves the
    # picker permanently "busy" after one unanswered dialog.
    monkeypatch.setattr(dirpicker, "_DIALOG_TIMEOUT_S", 0.5)
    real_osascript("delay 30")
    for _ in range(2):
        with pytest.raises(dirpicker.PickerFailed):
            dirpicker.pick_directory()


# ------------------------------------------------------------- linux backend


def test_zenity_is_asked_for_a_directory_and_seeded(monkeypatch):
    from fused_render.supervisor._linux import ui

    seen = {}

    def fake_run(argv):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="/home/ada/code\n", stderr="")

    monkeypatch.setattr(ui, "_dialog_tool", lambda: "zenity")
    monkeypatch.setattr(ui, "_run", fake_run)
    assert ui.pick_directory(start="/home/ada") == "/home/ada/code"
    assert "--directory" in seen["argv"]
    assert "--file-selection" in seen["argv"]
    # zenity only treats --filename as a folder when it ends in a separator.
    assert "/home/ada/" in seen["argv"]


def test_kdialog_is_asked_for_an_existing_directory(monkeypatch):
    from fused_render.supervisor._linux import ui

    seen = {}

    def fake_run(argv):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="/home/ada/code\n", stderr="")

    monkeypatch.setattr(ui, "_dialog_tool", lambda: "kdialog")
    monkeypatch.setattr(ui, "_run", fake_run)
    assert ui.pick_directory(start="/home/ada") == "/home/ada/code"
    assert "--getexistingdirectory" in seen["argv"]


def test_a_linux_cancel_is_none_but_a_crash_raises(monkeypatch):
    from fused_render.supervisor._linux import ui

    monkeypatch.setattr(ui, "_dialog_tool", lambda: "zenity")
    # Exit 1 with nothing on stdout is how both tools report "user said no".
    monkeypatch.setattr(ui, "_run", lambda argv: subprocess.CompletedProcess(
        argv, 1, stdout="", stderr=""))
    assert ui.pick_directory() is None
    # Anything else is a broken dialog, and must not masquerade as a cancel.
    monkeypatch.setattr(ui, "_run", lambda argv: subprocess.CompletedProcess(
        argv, 255, stdout="", stderr="cannot open display\n"))
    with pytest.raises(OSError):
        ui.pick_directory()
    # A dialog that never returned at all (timeout / missing binary) likewise.
    monkeypatch.setattr(ui, "_run", lambda argv: None)
    with pytest.raises(OSError):
        ui.pick_directory()


# ----------------------------------------------------------- appkit thread hop


@pytest.fixture
def menubar_pin():
    """The real module. Skipped rather than faked where pyobjc is absent (a
    Linux CI runner, or a dev venv without the `app` extra): a stub would assert
    our own idea of the AppKit surface, which is the opposite of the point."""
    return pytest.importorskip("fused_render.menubar_pin")


def _off_main_thread(call):
    """Run `call()` on a worker thread and return its result, re-raising there.

    Not a detail: `choose_directory` runs the panel INLINE when it is already on
    the main thread (posting from the run loop to itself would deadlock), and
    pytest runs on the main thread — so calling it directly here would take that
    branch and never exercise the hop, the Event, or the timeout at all. Three of
    these tests did exactly that until the timeout one refused to fail.
    """
    cell = {}

    def run():
        try:
            cell["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            cell["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "the call never returned"
    if "error" in cell:
        raise cell["error"]
    return cell["value"]


def _delivers_on_another_thread(monkeypatch, menubar_pin):
    """Stand in for callAfter with "runs on some other thread" — the property the
    Event and the result cell actually have to survive."""
    monkeypatch.setattr(
        menubar_pin, "_call_on_main_thread",
        lambda fn: threading.Thread(target=fn, daemon=True).start())


@darwin_only
def test_the_panel_result_crosses_back_to_the_calling_thread(menubar_pin, monkeypatch):
    seen = {}

    def panel(start, title, prompt):
        seen["args"] = (start, title, prompt)
        return "/Users/ada/code"

    monkeypatch.setattr(menubar_pin, "_run_directory_panel", panel)
    _delivers_on_another_thread(monkeypatch, menubar_pin)
    assert _off_main_thread(
        lambda: menubar_pin.choose_directory(start="/Users/ada")) == "/Users/ada/code"
    # The starting folder really reaches the panel — the whole reason the
    # endpoint accepts one.
    assert seen["args"][0] == "/Users/ada"


@darwin_only
def test_on_the_main_thread_the_panel_runs_inline(menubar_pin, monkeypatch):
    # The menu action's own path: posting from the run loop to itself would
    # deadlock, so this branch must NOT go through callAfter.
    monkeypatch.setattr(menubar_pin, "_run_directory_panel",
                        lambda start, title, prompt: "/Users/ada")
    monkeypatch.setattr(menubar_pin, "_call_on_main_thread",
                        lambda fn: pytest.fail("must not post to itself"))
    assert menubar_pin.choose_directory() == "/Users/ada"


@darwin_only
def test_a_cancelled_panel_crosses_back_as_a_cancel(menubar_pin, monkeypatch):
    monkeypatch.setattr(menubar_pin, "_run_directory_panel",
                        lambda start, title, prompt: None)
    _delivers_on_another_thread(monkeypatch, menubar_pin)
    assert _off_main_thread(menubar_pin.choose_directory) is None


@darwin_only
def test_a_panel_that_raises_raises_on_the_calling_thread(menubar_pin, monkeypatch):
    # Without re-raising, the request thread would see None and report a cancel
    # while the real reason sat in a dead thread's traceback.
    def boom(start, title, prompt):
        raise ValueError("no panel for you")

    monkeypatch.setattr(menubar_pin, "_run_directory_panel", boom)
    _delivers_on_another_thread(monkeypatch, menubar_pin)
    with pytest.raises(ValueError, match="no panel"):
        _off_main_thread(menubar_pin.choose_directory)


@darwin_only
def test_a_panel_nobody_delivers_times_out(menubar_pin, monkeypatch):
    # The failure mode this guards is real: under `fused-render serve` there is
    # no run loop, so a posted block is never run and the waiter would hang for
    # the life of the process.
    monkeypatch.setattr(menubar_pin, "_run_directory_panel",
                        lambda start, title, prompt: "/never")
    monkeypatch.setattr(menubar_pin, "_call_on_main_thread", lambda fn: None)
    with pytest.raises(TimeoutError):
        _off_main_thread(lambda: menubar_pin.choose_directory(timeout=0.2))


# --------------------------------------------------------- the single-dialog lock


def test_a_second_request_cannot_stack_a_second_modal(monkeypatch):
    opened = threading.Event()
    release = threading.Event()

    def slow_pick(start, title):
        opened.set()
        release.wait(5)
        return "/Users/ada/code"

    monkeypatch.setattr(dirpicker, "_backend", lambda: "osascript")
    monkeypatch.setitem(dirpicker._BACKENDS, "osascript", slow_pick)

    first = {}
    worker = threading.Thread(
        target=lambda: first.update(path=dirpicker.pick_directory()), daemon=True)
    worker.start()
    assert opened.wait(5), "the first dialog never opened"
    with pytest.raises(dirpicker.PickerBusy):
        dirpicker.pick_directory()
    release.set()
    worker.join(5)
    assert first["path"] == "/Users/ada/code"
    # And the lock is handed back, so the next request is served normally.
    assert dirpicker.pick_directory() == "/Users/ada/code"


def test_the_lock_is_released_when_a_dialog_fails(monkeypatch):
    def boom(start, title):
        raise dirpicker.PickerFailed("nope")

    monkeypatch.setattr(dirpicker, "_backend", lambda: "osascript")
    monkeypatch.setitem(dirpicker._BACKENDS, "osascript", boom)
    for _ in range(2):
        with pytest.raises(dirpicker.PickerFailed):
            dirpicker.pick_directory()


def test_no_backend_is_unavailable_not_a_failure(monkeypatch):
    monkeypatch.setattr(dirpicker, "_backend", lambda: "")
    with pytest.raises(dirpicker.PickerUnavailable):
        dirpicker.pick_directory()


def test_a_backend_answering_a_relative_path_is_a_failure(monkeypatch):
    # Every /api/fs/* endpoint requires an absolute path, so a backend that
    # somehow answers a relative one must be caught here rather than handed on
    # to be refused confusingly later.
    monkeypatch.setattr(dirpicker, "_backend", lambda: "osascript")
    monkeypatch.setitem(dirpicker._BACKENDS, "osascript",
                        lambda start, title: "relative/dir")
    with pytest.raises(dirpicker.PickerFailed):
        dirpicker.pick_directory()


def test_an_os_error_from_a_backend_becomes_a_picker_failure(monkeypatch):
    # The platform helpers raise OSError for "the dialog broke" (the convention
    # _linux/ui.py already uses); the endpoint only knows the picker exceptions.
    monkeypatch.setattr(dirpicker, "_backend", lambda: "linux")
    monkeypatch.setitem(dirpicker._BACKENDS, "linux",
                        lambda start, title: (_ for _ in ()).throw(OSError("no display")))
    with pytest.raises(dirpicker.PickerFailed, match="no display"):
        dirpicker.pick_directory()


# --------------------------------------------------------------- the endpoint


def _pick(body, x_fused="1"):
    return api_fs_pick_folder(body=body, x_fused=x_fused)


def test_the_endpoint_is_guarded_like_every_other_mutating_post():
    resp = _pick({}, x_fused=None)
    assert _status(resp) == 403
    assert "X-Fused" in _data(resp)["error"]


def test_a_relative_start_is_refused_before_any_dialog(monkeypatch):
    monkeypatch.setattr(dirpicker, "pick_directory",
                        lambda **kw: pytest.fail("must not open a dialog"))
    resp = _pick({"start": "relative/dir"})
    assert _status(resp) == 400
    assert "absolute" in _data(resp)["error"]


def test_a_chosen_folder_comes_back_as_a_path(monkeypatch):
    seen = {}

    def fake(start=None, title=None):
        seen.update(start=start, title=title)
        return "/Users/ada/code"

    monkeypatch.setattr(dirpicker, "pick_directory", fake)
    assert _data(_pick({"start": "/Users/ada", "title": "Clone into…"})) == {
        "path": "/Users/ada/code"}
    assert seen["start"] == "/Users/ada"
    # The caller's wording reaches the OS dialog: it is the dialog the user
    # actually sees, so "Clone this bundle into…" must not be swapped for the
    # generic default while only the in-page fallback says the real thing.
    assert seen["title"] == "Clone into…"


def test_a_runaway_title_is_clamped_rather_than_sizing_the_dialog(monkeypatch):
    seen = {}
    monkeypatch.setattr(dirpicker, "pick_directory",
                        lambda start=None, title=None: seen.update(title=title) or "/tmp")
    _pick({"title": "x" * 5000})
    assert len(seen["title"]) == 120


def test_no_title_gets_a_sensible_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(dirpicker, "pick_directory",
                        lambda start=None, title=None: seen.update(title=title) or "/tmp")
    _pick({})
    assert seen["title"] == "Choose a folder"


def test_no_start_is_fine(monkeypatch):
    monkeypatch.setattr(dirpicker, "pick_directory", lambda **kw: "/tmp")
    assert _data(_pick({}))["path"] == "/tmp"
    # An empty string is "no starting folder", not a path to stat.
    assert _data(_pick({"start": ""}))["path"] == "/tmp"


def test_a_cancel_is_a_null_path_and_a_200_not_an_error(monkeypatch):
    # The contract the page depends on: a cancel must be distinguishable from a
    # failure, or cancelling pops a second chooser.
    monkeypatch.setattr(dirpicker, "pick_directory", lambda **kw: None)
    resp = _pick({})
    assert _status(resp) == 200
    assert _data(resp) == {"path": None}


def test_a_busy_picker_is_a_conflict(monkeypatch):
    def busy(**kw):
        raise dirpicker.PickerBusy("a folder chooser is already open")

    monkeypatch.setattr(dirpicker, "pick_directory", busy)
    resp = _pick({})
    assert _status(resp) == 409
    assert "already open" in _data(resp)["error"]


def test_no_native_dialog_says_so_rather_than_pretending(monkeypatch):
    def unavailable(**kw):
        raise dirpicker.PickerUnavailable("no folder chooser here")

    monkeypatch.setattr(dirpicker, "pick_directory", unavailable)
    resp = _pick({})
    assert _status(resp) == 501
    assert "no folder chooser here" in _data(resp)["error"]


def test_a_broken_dialog_is_a_server_error(monkeypatch):
    def failed(**kw):
        raise dirpicker.PickerFailed("the panel exploded")

    monkeypatch.setattr(dirpicker, "pick_directory", failed)
    resp = _pick({})
    assert _status(resp) == 500
    assert "exploded" in _data(resp)["error"]


# ------------------------------------------------------------ the capability flag


def test_the_config_advertises_the_capability(monkeypatch):
    # The templates branch on this: with it false they use the in-page dialog
    # instead of waiting on a modal that will never appear (a hosted deploy).
    monkeypatch.setattr(dirpicker, "available", lambda: True)
    assert api_config(start_dir="/tmp", token=None)["native_dir_picker"] is True
    monkeypatch.setattr(dirpicker, "available", lambda: False)
    assert api_config(start_dir="/tmp", token=None)["native_dir_picker"] is False


def test_this_machine_really_can_raise_a_folder_dialog():
    # Not a tautology: it asserts that on a developer/desktop macOS or a Linux
    # session with a display, the capability comes out TRUE — i.e. the detection
    # is not accidentally disabled everywhere by a typo in a platform name.
    if sys.platform == "darwin" and shutil.which("osascript"):
        assert dirpicker.available() is True
    else:
        pytest.skip("no GUI session this test can make a claim about")
