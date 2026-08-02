"""Tests for the Linux pasteboard backend (shell/pasteboard/_linux.py).

`shutil.which` and `subprocess.run` are faked throughout, so every case here
runs on any platform — including the real behaviours we most need pinned
(tool preference order, which clipboard target gets written on which desktop,
URI encoding). Nothing here launches a process or touches a selection.
"""
import subprocess

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
    def _setup(tools, run=None, desktop=None):
        runner = run or _Run()
        monkeypatch.setattr(
            _linux.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None)
        monkeypatch.setattr(_linux.subprocess, "run", runner)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop or "")
        return runner
    return _setup


URI_LIST = "text/uri-list"
GNOME = "x-special/gnome-copied-files"


# ---------------------------------------------------------------- tool choice

def test_prefers_wl_clipboard_over_xclip(env):
    run = env({"wl-copy", "wl-paste", "xclip"})
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
    # No verb line in the uri-list format, and CRLF per RFC 2483.
    assert payload == b"file:///home/u/a.txt"


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
