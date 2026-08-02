"""Tests for the Windows pasteboard backend (shell/pasteboard/_win32.py).

The DROPFILES payload and the slash normalisation are pure functions with no
Win32 involvement, so they're tested here on every platform — that's the whole
reason they're factored out of the ctypes code. Only the real clipboard
round-trip is Windows-only.
"""
import sys

import pytest

from fused_render.shell.pasteboard import _win32

win_only = pytest.mark.skipif(
    sys.platform != "win32", reason="CF_HDROP round-trip is Windows-only")


# ------------------------------------------------------------- normalisation

def test_to_native_uses_backslashes():
    assert _win32.to_native("C:/Users/u/a file.csv") == "C:\\Users\\u\\a file.csv"


def test_to_native_leaves_backslash_paths_alone():
    assert _win32.to_native("C:\\Users\\u\\a.csv") == "C:\\Users\\u\\a.csv"


def test_to_native_handles_unc_paths():
    assert _win32.to_native("//server/share/f.txt") == "\\\\server\\share\\f.txt"


def test_to_shell_uses_forward_slashes():
    # The shell's canonical path form is forward-slash even on Windows
    # (frontend/src/lib/fs-actions.ts), so what we hand back must match or
    # every downstream comparison breaks.
    assert _win32.to_shell("C:\\Users\\u\\a file.csv") == "C:/Users/u/a file.csv"


def test_to_shell_round_trips():
    p = "C:/Users/u/deep/dir/name with spaces.parquet"
    assert _win32.to_shell(_win32.to_native(p)) == p


# ---------------------------------------------------------- DROPFILES buffer

def test_dropfiles_buffer_layout():
    buf = _win32.build_dropfiles(["C:/a.txt", "C:/dir"])
    raw = bytes(buf)

    # DROPFILES header: pFiles (DWORD offset to the file list), pt.x, pt.y
    # (POINT, unused), fNC (BOOL), fWide (BOOL = TRUE for the wide list).
    import struct
    p_files, x, y, f_nc, f_wide = struct.unpack_from("<IiiII", raw, 0)
    assert p_files == 20  # sizeof(DROPFILES)
    assert (x, y, f_nc) == (0, 0, 0)
    assert f_wide == 1

    names = raw[p_files:].decode("utf-16-le")
    # Double-NUL terminated list of NUL-separated native paths.
    assert names == "C:\\a.txt\x00C:\\dir\x00\x00"


def test_dropfiles_buffer_preserves_order_and_spaces():
    buf = _win32.build_dropfiles(["C:/b b.txt", "C:/a.txt"])
    names = bytes(buf)[20:].decode("utf-16-le")
    assert names.split("\x00")[:2] == ["C:\\b b.txt", "C:\\a.txt"]


def test_dropfiles_buffer_handles_non_ascii():
    buf = _win32.build_dropfiles(["C:/データ/ø.csv"])
    names = bytes(buf)[20:].decode("utf-16-le")
    assert names.startswith("C:\\データ\\ø.csv\x00")


def test_dropfiles_buffer_of_one_file_is_double_nul_terminated():
    raw = bytes(_win32.build_dropfiles(["C:/a.txt"]))
    assert raw[-4:] == "\x00\x00".encode("utf-16-le")


def test_unicode_text_payload_is_newline_joined_native_paths():
    # A terminal / editor paste should get the paths, in the form Windows
    # users expect to retype (backslashes).
    assert _win32.text_payload(["C:/a.txt", "C:/dir"]) == "C:\\a.txt\r\nC:\\dir"


# ------------------------------------------------------------- round-trip

@win_only
def test_round_trip_file_and_directory_with_spaces(tmp_path):
    from fused_render.shell import pasteboard

    f = tmp_path / "a file with spaces.csv"
    f.write_text("x,y\n")
    d = tmp_path / "a folder"
    d.mkdir()
    want = [str(f).replace("\\", "/"), str(d).replace("\\", "/")]

    token, supported = pasteboard.write_files(want)
    assert supported is True

    paths, read_token, read_supported = pasteboard.read_files()
    assert read_supported is True
    assert paths == want
    assert read_token == token


@win_only
def test_read_of_a_text_clipboard_yields_no_files():
    from fused_render.shell import pasteboard

    _win32._set_text_only("just some text")
    assert pasteboard.read_files() == ([], "", True)
