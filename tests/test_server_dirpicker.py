"""The native folder chooser: fused_render/server/dirpicker.py + the endpoint.

The point of this module is that the folder picker is the USER'S file dialog —
NSOpenPanel, zenity — not a div. That means the interesting parts
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
* the Linux backend is faked (there is no runner for it here),
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
import time

import pytest
from fastapi.responses import JSONResponse

from fused_render.server import dirpicker
from fused_render.server.routers.config import api_config
from fused_render.server.routers.fs_read import api_fs_pick_file, api_fs_pick_folder


@pytest.fixture(autouse=True)
def _no_dialog_outstanding():
    """Nobody's dialog leaks into the next test.

    The one-dialog claim is module state by necessity (there is one screen), and a
    test that deliberately abandons a dialog leaves it held — which would show up
    as an unrelated later test mysteriously getting PickerBusy.
    """
    dirpicker._forget_outstanding_dialog()
    yield
    dirpicker._forget_outstanding_dialog()

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


def test_the_applescript_narrows_a_file_pick_to_the_types_it_was_given():
    script = dirpicker._osascript_script(None, "Pick", files=True,
                                         types=["png", "webp"])
    assert 'of type {"png", "webp"}' in script
    # After the prompt and before the seed: `choose file` accepts the clauses in
    # that order and rejects them in any other.
    assert script.index("with prompt") < script.index("of type")


def test_a_folder_pick_never_grows_an_of_type_clause():
    # `choose folder` has no such clause — emitting one would be a syntax error
    # in the one script that has no filter to express in the first place.
    assert "of type" not in dirpicker._osascript_script(
        None, "Pick", types=["png"])
    # Nor does a file pick with nothing to narrow to.
    assert "of type" not in dirpicker._osascript_script(
        None, "Pick", files=True, types=[])


def test_a_quote_in_a_type_cannot_break_out_of_the_type_list():
    # An extension arrives off the wire, unlike a title: one holding a quote
    # would otherwise close the list early and leave the rest as statements.
    script = dirpicker._osascript_script(None, "Pick", files=True,
                                         types=['pn"g'])
    assert r'{"pn\"g"}' in script


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


def test_a_linux_file_pick_offers_the_types_and_keeps_all_files_reachable(monkeypatch):
    from fused_render.supervisor._linux import ui

    seen = {}

    def fake_run(argv):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="/home/ada/a.png\n", stderr="")

    monkeypatch.setattr(ui, "_dialog_tool", lambda: "zenity")
    monkeypatch.setattr(ui, "_run", fake_run)
    assert ui.pick_file(types=["png", "jpg"]) == "/home/ada/a.png"
    filters = [a for a in seen["argv"] if "|" in a]
    assert filters[0] == "Allowed files | *.png *.jpg"
    # The escape hatch stays: the filter is a courtesy, and a format the caller
    # can read but did not list must not become unpickable.
    assert filters[1] == "All files | *"
    # And the tray's own call, which passes none, is the dialog it always was.
    ui.pick_file()
    assert not [a for a in seen["argv"] if a == "--file-filter"]


def test_a_kdialog_file_pick_puts_the_filter_where_kdialog_wants_it(monkeypatch):
    from fused_render.supervisor._linux import ui

    seen = {}

    def fake_run(argv):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="/home/ada/a.png\n", stderr="")

    monkeypatch.setattr(ui, "_dialog_tool", lambda: "kdialog")
    monkeypatch.setattr(ui, "_run", fake_run)
    assert ui.pick_file(types=["png"]) == "/home/ada/a.png"
    # The filter is POSITIONAL and follows the start dir, so the empty string is
    # load-bearing: without it kdialog reads the filter as the directory to open.
    assert seen["argv"][-2:] == ["", "*.png|Allowed files\n*|All files"]


def test_a_leading_dot_on_a_type_does_not_become_a_double_dot_glob():
    from fused_render.supervisor._linux import ui

    # The wire may send either shape; a "*..png" would match nothing at all.
    assert ui._glob_filter([".png", "jpg"]) == ["*.png", "*.jpg"]
    assert ui._glob_filter(None) == []
    assert ui._glob_filter(["."]) == []


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


# ------------------------------------------------- a dialog we gave up waiting on
# The hard case, and the one the first version got wrong. When a backend CANNOT
# take the dialog down — the AppKit main thread owns a modal we have no handle
# on — giving up on the wait leaves the dialog on
# screen. Releasing the claim there stacks a second modal over the first; never
# releasing it wedges the picker for the life of the process. So the claim passes
# to the dialog, and clears when the dialog itself finishes.


def _abandoning_backend(monkeypatch, finished):
    """A backend that reports "I gave up, and the dialog is still up"."""
    def abandon(start, title):
        raise dirpicker.DialogAbandoned(finished)

    monkeypatch.setattr(dirpicker, "_backend", lambda: "appkit")
    monkeypatch.setitem(dirpicker._BACKENDS, "appkit", abandon)


def test_an_abandoned_dialog_keeps_the_claim_until_it_really_finishes(monkeypatch):
    still_up = threading.Event()
    _abandoning_backend(monkeypatch, still_up)

    with pytest.raises(dirpicker.PickerFailed, match="not answered"):
        dirpicker.pick_directory()

    # The dialog is STILL on screen, so a second request must be refused rather
    # than stacking another modal on top of it.
    monkeypatch.setitem(dirpicker._BACKENDS, "appkit",
                        lambda start, title: "/Users/ada/code")
    with pytest.raises(dirpicker.PickerBusy):
        dirpicker.pick_directory()

    # …and when the orphaned dialog finally closes, the picker works again. This
    # is the other half: refusing forever would be a permanent wedge after one
    # unanswered dialog.
    still_up.set()
    assert dirpicker.pick_directory() == "/Users/ada/code"


def test_a_dialog_that_finished_while_we_gave_up_does_not_wedge(monkeypatch):
    # The race: the dialog completes between the wait timing out and the claim
    # being handed over. Its event is already set, so the next request is served.
    already_done = threading.Event()
    already_done.set()
    _abandoning_backend(monkeypatch, already_done)
    with pytest.raises(dirpicker.PickerFailed):
        dirpicker.pick_directory()
    monkeypatch.setitem(dirpicker._BACKENDS, "appkit",
                        lambda start, title: "/Users/ada/code")
    assert dirpicker.pick_directory() == "/Users/ada/code"


@darwin_only
def test_the_appkit_timeout_really_reports_the_panel_as_still_up(
        menubar_pin, monkeypatch):
    # Same shape on macOS, and worse: the panel is owned by the MAIN thread, so
    # there is nothing at all we could cancel from here.
    let_go = threading.Event()
    monkeypatch.setattr(menubar_pin, "_run_directory_panel",
                        lambda start, title, prompt: let_go.wait(20) and "/late")
    _delivers_on_another_thread(monkeypatch, menubar_pin)
    with pytest.raises(menubar_pin.PanelNotAnswered) as caught:
        _off_main_thread(lambda: menubar_pin.choose_directory(timeout=0.3))
    finished = caught.value.finished
    assert not finished.is_set()
    let_go.set()
    assert finished.wait(5), "the panel never marked itself finished"
    # PanelNotAnswered stays a TimeoutError, so existing callers keep working.
    assert isinstance(caught.value, TimeoutError)


@darwin_only
def test_an_abandoned_appkit_panel_keeps_the_claim(menubar_pin, monkeypatch):
    # End to end through the real appkit backend seam: a panel nobody answers
    # holds the claim, and hands it back when it closes.
    let_go = threading.Event()
    monkeypatch.setattr(menubar_pin, "_run_directory_panel",
                        lambda start, title, prompt: let_go.wait(20) and "/late")
    _delivers_on_another_thread(monkeypatch, menubar_pin)
    monkeypatch.setattr(dirpicker, "_backend", lambda: "appkit")
    monkeypatch.setattr(dirpicker, "_DIALOG_TIMEOUT_S", 0.3)

    with pytest.raises(dirpicker.PickerFailed):
        _off_main_thread(dirpicker.pick_directory)
    with pytest.raises(dirpicker.PickerBusy):
        dirpicker.pick_directory()
    let_go.set()
    monkeypatch.setitem(dirpicker._BACKENDS, "appkit",
                        lambda start, title: "/Users/ada/code")
    # The panel's own event clears the claim; give it a moment to be observed.
    for _ in range(50):
        try:
            assert dirpicker.pick_directory() == "/Users/ada/code"
            break
        except dirpicker.PickerBusy:
            time.sleep(0.05)
    else:
        pytest.fail("the claim was never handed back after the panel closed")


def test_a_killable_dialog_still_hands_the_claim_straight_back(monkeypatch):
    # The asymmetry worth being explicit about: osascript and zenity run in a
    # CHILD PROCESS, which subprocess.run kills on timeout. There is no dialog
    # left, so those backends raise a plain failure and the claim frees at once —
    # they must NOT be made to hold it like the in-process ones do.
    monkeypatch.setattr(dirpicker, "_backend", lambda: "osascript")
    monkeypatch.setitem(dirpicker._BACKENDS, "osascript",
                        lambda start, title: (_ for _ in ()).throw(
                            dirpicker.PickerFailed("not answered in time")))
    with pytest.raises(dirpicker.PickerFailed):
        dirpicker.pick_directory()
    monkeypatch.setitem(dirpicker._BACKENDS, "osascript",
                        lambda start, title: "/Users/ada/code")
    assert dirpicker.pick_directory() == "/Users/ada/code"


def test_an_os_error_from_a_backend_becomes_a_picker_failure(monkeypatch):
    # The platform helpers raise OSError for "the dialog broke" (the convention
    # _linux/ui.py already uses); the endpoint only knows the picker exceptions.
    monkeypatch.setattr(dirpicker, "_backend", lambda: "linux")
    monkeypatch.setitem(dirpicker._BACKENDS, "linux",
                        lambda start, title: (_ for _ in ()).throw(OSError("no display")))
    with pytest.raises(dirpicker.PickerFailed, match="no display"):
        dirpicker.pick_directory()


# --------------------------------------------------- the shape of the path out


def test_a_windows_path_comes_back_forward_slashed(monkeypatch):
    # The shell, /api/fs/*, and folder-picker.js's parent()/join() all assume the
    # canonical forward-slashed form (see canonical_fs_path). A backslashed path
    # would feed mixed separators into every call after it.
    monkeypatch.setattr(dirpicker, "_backend", lambda: "linux")
    monkeypatch.setitem(dirpicker._BACKENDS, "linux",
                        lambda start, title: "C:\\Users\\ada\\code")
    assert dirpicker.pick_directory() == "C:/Users/ada/code"


def test_a_windows_drive_root_survives_as_a_drive_root(monkeypatch):
    # "C:/" is the canonical drive root folder-picker.js's isRoot() recognises; a
    # bare "C:" is cwd-relative and must never be produced.
    monkeypatch.setattr(dirpicker, "_backend", lambda: "linux")
    monkeypatch.setitem(dirpicker._BACKENDS, "linux", lambda start, title: "C:\\")
    assert dirpicker.pick_directory() == "C:/"


def test_a_drive_path_counts_as_absolute_off_windows_too():
    # posixpath.isabs("C:/Users/ada") is False; the check must classify instead.
    assert dirpicker._canonical_absolute("C:\\Users\\ada") == "C:/Users/ada"


def test_a_posix_path_with_a_backslash_in_a_name_round_trips(monkeypatch):
    # A backslash is a legal POSIX filename character, so it must NOT be swapped
    # for a separator — canonical_fs_path leaves it untouched.
    monkeypatch.setattr(dirpicker, "_backend", lambda: "linux")
    monkeypatch.setitem(dirpicker._BACKENDS, "linux",
                        lambda start, title: "/home/ada/we\\ird")
    assert dirpicker.pick_directory() == "/home/ada/we\\ird"

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


# ------------------------------------------------------- the FILE endpoint
# Only the half that is its own: the guard, the relative-start refusal and the
# cancel are `_pick`'s and are covered above. What is new here is `types`, which
# reaches three shells and so is the one field that may not be forwarded raw.


def _pick_file(body, x_fused="1"):
    return api_fs_pick_file(body=body, x_fused=x_fused)


def test_the_types_a_caller_asks_for_reach_the_dialog(monkeypatch):
    seen = {}
    monkeypatch.setattr(dirpicker, "pick_file",
                        lambda **kw: seen.update(kw) or "/Users/ada/a.png")
    assert _data(_pick_file({"types": ["png", "jpg"]})) == {"path": "/Users/ada/a.png"}
    assert seen["types"] == ["png", "jpg"]


def test_a_type_is_normalised_before_it_reaches_a_shell(monkeypatch):
    seen = {}
    monkeypatch.setattr(dirpicker, "pick_file",
                        lambda **kw: seen.update(kw) or "/tmp/a")
    _pick_file({"types": [".PNG", "png", "jpg;*", "", "x" * 40, 7]})
    # Dots stripped, lower-cased, de-duped; a glob-and-a-half and an empty one
    # dropped rather than escaped, because neither is an extension, and a
    # 40-character one dropped rather than SLICED — a truncated extension is a
    # filter matching files nobody asked for. A number is not refused: `str()`
    # makes it one, and "7" merely matches nothing.
    assert seen["types"] == ["png", "7"]
    # Clamped like `title`: a runaway list is a dialog nobody can use.
    _pick_file({"types": [f"e{i}" for i in range(200)]})
    assert len(seen["types"]) == 40


def test_no_types_leaves_the_dialog_showing_everything(monkeypatch):
    seen = {}
    monkeypatch.setattr(dirpicker, "pick_file",
                        lambda **kw: seen.update(kw) or "/tmp/a")
    _pick_file({})
    assert seen["types"] is None
    # Junk in the field is "show everything" too, NOT a dialog that can pick
    # nothing: a filter is a courtesy and a malformed one must not lock the user
    # out of the thing they came to do.
    for junk in ("png", {"png": True}, [], ["!!", "*"]):
        _pick_file({"types": junk})
        assert seen["types"] is None


def test_a_picked_file_comes_back_as_a_path_and_a_cancel_as_null(monkeypatch):
    monkeypatch.setattr(dirpicker, "pick_file", lambda **kw: "/Users/ada/a.png")
    assert _data(_pick_file({"title": "Choose a picture"}))["path"] == "/Users/ada/a.png"
    monkeypatch.setattr(dirpicker, "pick_file", lambda **kw: None)
    resp = _pick_file({})
    assert _status(resp) == 200
    assert _data(resp)["path"] is None


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


# ------------------------------------------------------------- several at once
#
# `pick_files` is `pick_file` with the panel's multi-select on. On macOS both
# backends genuinely answer a list; Linux picks one per dialog and
# `_pick` wraps it, so the contract (a non-empty list, or None) holds on every
# platform and a caller never branches on the OS.


def test_the_applescript_for_several_files_walks_the_list_one_path_per_line():
    script = dirpicker._osascript_script("/Users/ada", "Pick", files=True,
                                         types=["png", "mp3"], multiple=True)
    assert "with multiple selections allowed" in script
    assert 'of type {"png", "mp3"}' in script
    assert 'default location POSIX file "/Users/ada"' in script
    # `POSIX path of` takes one alias, so the multi script is a loop joining
    # on ASCII 30 — a newline is legal in a POSIX name, 0x1E never appears.
    assert "repeat with f in chosen" in script
    assert "character id 30" in script
    assert "linefeed" not in script
    assert "return found as text" in script


def test_a_single_file_pick_never_asks_for_multiple_selections():
    assert "multiple" not in dirpicker._osascript_script(
        None, "Pick", files=True, types=["png"])
    assert "multiple" not in dirpicker._osascript_script(None, "Pick")


@darwin_only
@needs_osascript
def test_a_real_osascript_multi_pick_answers_a_list_of_absolute_paths(monkeypatch, tmp_path):
    """The REAL generated multi-pick script — loop, delimiter, `as text` — run
    through the real osascript, with only the `choose file …` call swapped for
    a literal list so no human has to click. Two names, one holding a space
    and one a newline, because those are the two a naive join gets wrong."""
    one = tmp_path / "a b.png"
    two = tmp_path / "line\nbreak.wav"
    one.write_bytes(b"x")
    two.write_bytes(b"x")
    real = dirpicker._osascript_script(None, "Pick", files=True, multiple=True)
    head, _, tail = real.partition("\n")
    assert head.startswith("set chosen to (choose file")
    literal = ("set chosen to {POSIX file " + dirpicker._as_applescript_string(str(one))
               + ", POSIX file " + dirpicker._as_applescript_string(str(two)) + "}")
    monkeypatch.setattr(dirpicker, "_backend", lambda: "osascript")
    monkeypatch.setattr(dirpicker, "_osascript_script",
                        lambda *a, **kw: literal + "\n" + tail)
    assert dirpicker.pick_files() == [str(one), str(two)]


@darwin_only
@needs_osascript
def test_a_real_osascript_multi_pick_cancel_is_none(monkeypatch):
    monkeypatch.setattr(dirpicker, "_backend", lambda: "osascript")
    monkeypatch.setattr(dirpicker, "_osascript_script",
                        lambda *a, **kw: "error number -128")
    assert dirpicker.pick_files() is None


def test_a_backend_that_picks_one_is_wrapped_in_a_list(monkeypatch):
    seen = {}

    def one(start, title, *, files=False, types=None, multiple=False):
        seen.update(multiple=multiple, types=types)
        return "/home/ada/one.wav"

    monkeypatch.setattr(dirpicker, "_backend", lambda: "linux")
    monkeypatch.setitem(dirpicker._BACKENDS, "linux", one)
    assert dirpicker.pick_files(types=["wav"]) == ["/home/ada/one.wav"]
    assert seen == {"multiple": True, "types": ["wav"]}


def test_a_backend_that_picks_several_has_each_canonicalised(monkeypatch):
    monkeypatch.setattr(dirpicker, "_backend", lambda: "appkit")
    monkeypatch.setitem(dirpicker._BACKENDS, "appkit",
                        lambda *a, **kw: ["/Users/ada/a.png/", "/Users/ada//b.mp4"])
    assert dirpicker.pick_files() == ["/Users/ada/a.png", "/Users/ada/b.mp4"]


def test_an_empty_multi_answer_is_a_cancel(monkeypatch):
    monkeypatch.setattr(dirpicker, "_backend", lambda: "appkit")
    monkeypatch.setitem(dirpicker._BACKENDS, "appkit", lambda *a, **kw: [])
    assert dirpicker.pick_files() is None


def test_the_appkit_backend_forwards_multiple_to_the_panel(monkeypatch):
    import types as _types

    seen = {}
    fake = _types.SimpleNamespace(
        choose_file=lambda **kw: seen.update(kw) or ["/Users/ada/a.png"],
        choose_directory=lambda **kw: "/Users/ada",
        PanelNotAnswered=TimeoutError)
    monkeypatch.setitem(sys.modules, "fused_render.menubar_pin", fake)
    assert dirpicker._pick_appkit(None, "Pick", files=True, types=["png"],
                                  multiple=True) == ["/Users/ada/a.png"]
    assert seen["multiple"] is True and seen["types"] == ["png"]


def _pick_files(body, x_fused="1"):
    from fused_render.server.routers.fs_read import api_fs_pick_files
    return api_fs_pick_files(body=body, x_fused=x_fused)


def test_the_pick_files_route_answers_a_list_and_null_on_cancel(monkeypatch):
    seen = {}
    monkeypatch.setattr(dirpicker, "pick_files",
                        lambda **kw: seen.update(kw) or ["/Users/ada/a.png", "/Users/ada/b.wav"])
    resp = _pick_files({"title": "Choose files", "types": [".PNG", "wav"]})
    assert _status(resp) == 200
    assert _data(resp) == {"paths": ["/Users/ada/a.png", "/Users/ada/b.wav"]}
    assert seen["types"] == ["png", "wav"] and seen["title"] == "Choose files"
    monkeypatch.setattr(dirpicker, "pick_files", lambda **kw: None)
    assert _data(_pick_files({})) == {"paths": None}


def test_the_pick_files_route_keeps_the_single_routes_refusals(monkeypatch):
    assert _status(_pick_files({}, x_fused=None)) == _status(_pick_file({}, x_fused=None))
    assert _status(_pick_files({"start": "relative/dir"})) == 400
    monkeypatch.setattr(dirpicker, "pick_files",
                        lambda **kw: (_ for _ in ()).throw(dirpicker.PickerBusy("busy")))
    assert _status(_pick_files({})) == 409
    monkeypatch.setattr(dirpicker, "pick_files",
                        lambda **kw: (_ for _ in ()).throw(dirpicker.PickerUnavailable("none")))
    assert _status(_pick_files({})) == 501
