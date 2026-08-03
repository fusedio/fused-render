"""Tests for the Linux pasteboard backend (shell/pasteboard/_linux.py).

`shutil.which` and `subprocess.run` are faked throughout, so every case here
runs on any platform — including the real behaviours we most need pinned
(tool preference order, which clipboard target gets written on which desktop,
URI encoding). Nothing here launches a process or touches a selection.
"""
import subprocess
import sys

import pytest

from fused_render.shell.pasteboard import _linux


class _Run:
    """Records subprocess.run calls and replays canned stdout per command."""

    def __init__(self, outputs=None, fail=()):
        self.outputs = outputs or {}   # argv[0] + first format arg -> stdout bytes
        self.fail = set(fail)          # keys that should exit non-zero
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        key = self._key(argv)
        if key in self.fail:
            return subprocess.CompletedProcess(argv, 1, b"", b"nope")
        return subprocess.CompletedProcess(argv, 0, self.outputs.get(key, b""), b"")

    @staticmethod
    def _key(argv):
        fmt = next((a for a in argv if "/" in a and not a.startswith("-")), "")
        return (argv[0], fmt)


@pytest.fixture
def env(monkeypatch):
    """Install fake tool availability + subprocess, return the recorder."""
    def _setup(tools, run=None, desktop=None, session=None):
        runner = run or _Run()
        monkeypatch.setattr(
            _linux.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None)
        monkeypatch.setattr(_linux.subprocess, "run", runner)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop or "")
        # Session type is pinned on EVERY case, never inherited: tool choice
        # now depends on it, and a developer's own Wayland/X11 session leaking
        # in would make these pass or fail by accident of where they ran.
        # `session=None` means X11, the case where the two tool families most
        # often coexist.
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        if session == "wayland":
            monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("XDG_SESSION_TYPE", session or "x11")
        return runner
    return _setup


URI_LIST = "text/uri-list"
GNOME = "x-special/gnome-copied-files"


# ---------------------------------------------------------------- tool choice

def test_prefers_wl_clipboard_on_a_wayland_session(env):
    run = env({"wl-copy", "wl-paste", "xclip"}, session="wayland")
    _linux.write_files(["/home/u/a.txt"])
    assert run.calls[0][0][0] == "wl-copy"


def test_prefers_xclip_on_an_x11_session_even_with_wl_clipboard_installed(env):
    # Found in review. Preference used to be "whatever is installed", and
    # wl-clipboard is pulled in as a dependency of plenty of unrelated
    # packages — so on X11 the backend chose tools with no compositor to talk
    # to, failed inside them, and reported the whole bridge unsupported while
    # a working xclip was never tried.
    run = env({"wl-copy", "wl-paste", "xclip"}, session="x11")
    _linux.write_files(["/home/u/a.txt"])
    assert run.calls[0][0][0] == "xclip"


def test_a_wayland_session_with_only_xclip_still_works(env):
    # The session states a preference, not a requirement: under XWayland xclip
    # is a real client, and refusing it because wl-clipboard is absent would
    # turn a working machine into an unsupported one.
    run = env({"xclip"}, session="wayland")
    _linux.write_files(["/home/u/a.txt"])
    assert run.calls[0][0][0] == "xclip"


def test_an_x11_session_with_only_wl_clipboard_still_uses_it(env):
    run = env({"wl-copy", "wl-paste"}, session="x11")
    _linux.write_files(["/home/u/a.txt"])
    assert run.calls[0][0][0] == "wl-copy"


def test_wayland_display_alone_marks_a_wayland_session(env, monkeypatch):
    # XDG_SESSION_TYPE can say "x11" under XWayland while WAYLAND_DISPLAY is
    # set; the socket the tools actually connect to is the stronger evidence.
    run = env({"wl-copy", "wl-paste", "xclip"}, session="x11")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    _linux.write_files(["/home/u/a.txt"])
    assert run.calls[0][0][0] == "wl-copy"


def test_falls_back_to_xclip(env):
    run = env({"xclip"})
    _linux.write_files(["/home/u/a.txt"])
    assert run.calls[0][0][0] == "xclip"


def test_wl_clipboard_needs_both_halves(env):
    # wl-copy alone can't read; treat a half-installed wl-clipboard as absent
    # so we fall back to a tool that can do both directions.
    run = env({"wl-copy", "xclip"})
    _linux.write_files(["/home/u/a.txt"])
    assert run.calls[0][0][0] == "xclip"


def test_unsupported_when_no_tool_is_installed(env):
    env(set())
    with pytest.raises(_linux.NoClipboardTool):
        _linux.read_files()
    with pytest.raises(_linux.NoClipboardTool):
        _linux.write_files(["/home/u/a.txt"])


def test_the_contract_reports_unsupported_without_a_tool(env, monkeypatch):
    """End-to-end through the contract: a missing tool is `supported: False`,
    never an exception reaching the route."""
    from fused_render.shell import pasteboard

    env(set())
    monkeypatch.setattr(pasteboard, "_load_backend", lambda: _linux)
    assert pasteboard.read_files() == ([], "", False)
    assert pasteboard.write_files(["/home/u/a.txt"]) == ("", False)


# --------------------------------------------------------------------- read

def test_read_parses_the_gnome_format(env):
    run = env({"wl-copy", "wl-paste"}, run=_Run({
        ("wl-paste", GNOME): b"copy\nfile:///home/u/a%20file.txt\nfile:///home/u/dir\n",
    }))
    assert _linux.read_files() == ["/home/u/a file.txt", "/home/u/dir"]
    # The GNOME target is tried first — it's the only one carrying the
    # copy/cut verb, so it's the more informative of the two.
    assert run.calls[0][0][0] == "wl-paste"


def test_read_falls_back_to_uri_list(env):
    env({"xclip"}, run=_Run(
        outputs={("xclip", URI_LIST): b"file:///home/u/b.txt\r\nfile:///home/u/c.txt\r\n"},
        fail={("xclip", GNOME)}))
    assert _linux.read_files() == ["/home/u/b.txt", "/home/u/c.txt"]


def test_read_ignores_the_cut_verb_line(env):
    # We deliberately don't honour cut (no reliable semantics, and acting on
    # it would delete the user's files on a guess) — but the verb line must
    # not be mistaken for a path either.
    env({"xclip"}, run=_Run({
        ("xclip", GNOME): b"cut\nfile:///home/u/a.txt\n",
    }))
    assert _linux.read_files() == ["/home/u/a.txt"]


def test_read_of_an_empty_clipboard_is_empty(env):
    env({"xclip"}, run=_Run(fail={("xclip", GNOME), ("xclip", URI_LIST)}))
    assert _linux.read_files() == []


def test_read_skips_non_file_uris(env):
    env({"xclip"}, run=_Run({
        ("xclip", GNOME): b"copy\nhttps://example.com/x\nfile:///home/u/a.txt\n",
    }))
    assert _linux.read_files() == ["/home/u/a.txt"]


def test_read_decodes_non_ascii_uris(env):
    env({"xclip"}, run=_Run({
        ("xclip", GNOME): b"copy\nfile:///home/u/%C3%B8/%E3%83%87%E3%83%BC%E3%82%BF.csv\n",
    }))
    assert _linux.read_files() == ["/home/u/ø/データ.csv"]


# -------------------------------------------------------------------- write

def test_write_uses_the_gnome_format_by_default(env):
    run = env({"xclip"})
    _linux.write_files(["/home/u/a file.txt"])
    argv, payload = run.calls[0]
    assert GNOME in argv
    assert payload == b"copy\nfile:///home/u/a%20file.txt"


def test_write_uses_uri_list_on_kde(env):
    run = env({"xclip"}, desktop="KDE")
    _linux.write_files(["/home/u/a.txt"])
    argv, payload = run.calls[0]
    assert URI_LIST in argv
    # No verb line in the uri-list format.
    assert payload == b"file:///home/u/a.txt"


def test_the_kde_uri_list_separates_entries_with_crlf(env):
    # Found in review: the comment above claimed CRLF while the join used
    # "\n", and a single-URI assertion could not tell the difference — no
    # separator appears in a one-entry list. RFC 2483 specifies CRLF, so a
    # multi-file copy into Dolphin was relying on a lenient parser.
    run = env({"xclip"}, desktop="KDE")
    _linux.write_files(["/home/u/a.txt", "/home/u/b.txt"])
    _argv, payload = run.calls[0]
    assert payload == b"file:///home/u/a.txt\r\nfile:///home/u/b.txt"


def test_a_crlf_uri_list_round_trips_through_the_reader(env):
    # The two halves must agree: whatever the write publishes, our own read
    # has to parse back. `_parse` normalizes CRLF, so this holds by design —
    # pinned because the write's separator just changed.
    paths = ["/home/u/a.txt", "/home/u/b.txt"]
    run = env({"xclip"}, desktop="KDE")
    _linux.write_files(paths)
    _argv, payload = run.calls[0]
    env({"xclip"}, run=_Run({("xclip", GNOME): b"", ("xclip", URI_LIST): payload}),
        desktop="KDE")
    assert _linux.read_files() == paths


def test_write_uses_uri_list_for_a_plasma_session(env):
    run = env({"xclip"}, desktop="KDE:plasma")
    assert _linux.write_files(["/home/u/a.txt"]) is None
    assert URI_LIST in run.calls[0][0]


def test_write_desktop_match_is_case_insensitive(env):
    run = env({"xclip"}, desktop="kde")
    _linux.write_files(["/home/u/a.txt"])
    assert URI_LIST in run.calls[0][0]


def test_write_gnome_format_on_a_gnome_session(env):
    run = env({"xclip"}, desktop="ubuntu:GNOME")
    _linux.write_files(["/home/u/a.txt"])
    assert GNOME in run.calls[0][0]


def test_write_encodes_multiple_paths(env):
    run = env({"wl-copy", "wl-paste"})
    _linux.write_files(["/home/u/ø.csv", "/home/u/a dir"])
    assert run.calls[0][1] == b"copy\nfile:///home/u/%C3%B8.csv\nfile:///home/u/a%20dir"


def test_write_raises_when_the_tool_fails(env):
    env({"xclip"}, run=_Run(fail={("xclip", GNOME)}))
    with pytest.raises(OSError):
        _linux.write_files(["/home/u/a.txt"])


# ------------------------------------------------------------- URI encoding

@pytest.mark.parametrize("path", [
    "/home/u/plain.txt",
    "/home/u/a file with spaces.txt",
    "/home/u/ø/データ.csv",
    "/home/u/100% done.txt",
    "/home/u/a#b?c.txt",
])
def test_uri_round_trip(path):
    assert _linux.uri_to_path(_linux.path_to_uri(path)) == path


def test_path_to_uri_keeps_separators_unencoded():
    # Encoding "/" would produce a single-segment URI no file manager accepts.
    assert _linux.path_to_uri("/home/u/a b") == "file:///home/u/a%20b"


def test_uri_to_path_accepts_a_localhost_authority():
    # file://localhost/... is as legal as file:///... and some toolkits emit
    # it; a fixed 7-char slice would yield the relative "localhost/home/x",
    # which the contract then drops silently.
    assert _linux.uri_to_path("file://localhost/home/x") == "/home/x"


def test_uri_to_path_refuses_a_remote_authority():
    # A real host is a path we have no local answer for — refuse rather than
    # invent one.
    assert _linux.uri_to_path("file://server/share/x") is None


# ------------------------------------------------- the daemonizing-tool trap

# Everything above fakes subprocess, which models the helper as a function
# call — so no test above can see anything about PROCESS LIFECYCLE. That gap
# hid a real bug: `xclip -i` and `wl-copy` fork a resident daemon to own the
# selection (this module's whole premise), the daemon inherits any captured
# stdout/stderr pipes and holds them open indefinitely, and subprocess.run
# waits for EOF on those pipes — so every copy stalled for the full timeout
# and then reported itself failed. This test uses a REAL forking process, so
# it fails against a write path that captures output. It needs no clipboard
# and runs on any POSIX platform.

_FORKING_TOOL = """\
import os, sys, time
sys.stdin.buffer.read()
if os.fork() == 0:
    # The "daemon": outlives the parent still holding stdout/stderr, exactly
    # as a real clipboard owner does.
    time.sleep(30)
    os._exit(0)
sys.exit(0)
"""


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="needs fork()")
def test_write_does_not_wait_on_a_daemonizing_tool(tmp_path, monkeypatch):
    import os
    import time

    # Real executables on a PATH we own, so `which` and the exec are both the
    # genuine article — the point of this test is that nothing is faked
    # between write_files and a forking process.
    for name in ("wl-copy", "wl-paste"):
        tool = tmp_path / name
        tool.write_text(f"#!/bin/sh\nexec {sys.executable} -c '{_FORKING_TOOL}'\n")
        tool.chmod(0o755)

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "")

    started = time.monotonic()
    _linux.write_files(["/home/u/a.txt"])  # must not raise TimeoutExpired
    elapsed = time.monotonic() - started

    # The real bug took the full _TIMEOUT_S and then raised; the parent here
    # exits as soon as it isn't blocked on a pipe nobody will close.
    assert elapsed < _linux._TIMEOUT_S, (
        f"the write waited {elapsed:.2f}s on a forking tool — stdout/stderr "
        "are being captured, so subprocess.run is blocking on pipes the "
        "clipboard daemon holds open")
