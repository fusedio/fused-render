"""Tests for the macOS pasteboard backend (shell/pasteboard/_darwin.py).

The round-trip tests drive the *real* NSPasteboard, so they only run on
Darwin with pyobjc present; they clobber the user's clipboard, which is
acceptable on a dev machine and is why they're skipped everywhere else.
The degradation test (no pyobjc -> unsupported) runs on every platform,
since that's the behaviour the contract promises.
"""
import sys

import pytest

from fused_render.shell import pasteboard

darwin_only = pytest.mark.skipif(
    sys.platform != "darwin", reason="NSPasteboard round-trip is macOS-only")


def _appkit_available() -> bool:
    try:
        import AppKit  # noqa: F401
    except Exception:
        return False
    return True


needs_pyobjc = pytest.mark.skipif(
    not _appkit_available(), reason="pyobjc (AppKit) not installed")


# ---------------------------------------------------------------- degradation

def test_missing_pyobjc_degrades_to_unsupported(monkeypatch):
    """An import failure inside the backend must surface as unsupported, not
    as an exception — the whole point of the lazy in-function import."""
    monkeypatch.setattr(pasteboard, "_backend", pasteboard._UNPROBED, raising=False)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _no_appkit(name, *a, **kw):
        if name == "AppKit" or name.startswith("AppKit."):
            raise ImportError("No module named 'AppKit'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _no_appkit)
    if sys.platform != "darwin":
        pytest.skip("dispatch only reaches _darwin on macOS")
    assert pasteboard.read_files() == ([], "", False)
    assert pasteboard.write_files(["/tmp/x"]) == ("", False)


# ------------------------------------------------------------------ round-trip

@darwin_only
@needs_pyobjc
def test_round_trip_file_and_directory_with_spaces(tmp_path):
    f = tmp_path / "a file with spaces.csv"
    f.write_text("x,y\n")
    d = tmp_path / "a folder"
    d.mkdir()

    token, supported = pasteboard.write_files([str(f), str(d)])
    assert supported is True
    assert token

    paths, read_token, read_supported = pasteboard.read_files()
    assert read_supported is True
    assert paths == [str(f), str(d)]
    # The token is a pure function of the paths, so a clean round-trip must
    # reproduce it — that identity is what the frontend's "unchanged?" check
    # relies on.
    assert read_token == token


@darwin_only
@needs_pyobjc
def test_write_also_publishes_plain_text_paths(tmp_path):
    """A terminal paste should yield the path, not nothing — so the write
    carries public.utf8-plain-text alongside the file URLs."""
    from AppKit import NSPasteboard, NSPasteboardTypeString

    a = tmp_path / "one.txt"
    a.write_text("1")
    b = tmp_path / "two.txt"
    b.write_text("2")
    pasteboard.write_files([str(a), str(b)])

    text = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
    assert str(text) == f"{a}\n{b}"


@darwin_only
@needs_pyobjc
def test_write_replaces_the_previous_contents(tmp_path):
    a = tmp_path / "first.txt"
    a.write_text("1")
    b = tmp_path / "second.txt"
    b.write_text("2")
    pasteboard.write_files([str(a)])
    pasteboard.write_files([str(b)])
    paths, _, _ = pasteboard.read_files()
    assert paths == [str(b)]


@darwin_only
@needs_pyobjc
def test_read_of_plain_text_clipboard_yields_no_files():
    """Text on the clipboard is not a file reference — reading it must give
    an empty list (supported, nothing to paste), never a bogus path."""
    from AppKit import NSPasteboard, NSPasteboardTypeString

    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_("just some text", NSPasteboardTypeString)

    paths, token, supported = pasteboard.read_files()
    assert (paths, token, supported) == ([], "", True)
