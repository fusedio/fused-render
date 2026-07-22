"""Tests for the shared fs-path -> /view URL codec (_view_url_codec.py).

No sys.platform guard: the codec is built on pathlib.PureWindowsPath, a pure
path class that performs no OS calls and is instantiable on any platform, so
these tests run (and must pass) on Windows, macOS, and Linux alike.
"""
from fused_render._view_url_codec import view_url, view_url_path


def test_drive_letter_path():
    assert view_url(8000, "C:\\Users\\x") == "http://127.0.0.1:8000/view/C%3A/Users/x"


def test_drive_letter_path_unicode_and_spaces():
    # Mirrors the Rust supervisor test path C:\data\résumé.xlsx.
    assert view_url_path("C:\\data\\résumé.xlsx") == "/view/C%3A/data/r%C3%A9sum%C3%A9.xlsx"
    assert (
        view_url_path("C:\\data\\my résumé v2.xlsx")
        == "/view/C%3A/data/my%20r%C3%A9sum%C3%A9%20v2.xlsx"
    )


def test_forward_slash_drive_path_matches_backslash_form():
    assert view_url_path("C:/Users/x") == view_url_path("C:\\Users\\x")


def test_unc_path_stays_one_segment():
    # router.ts urlForFsPath only normalizes drive-letter paths, so a UNC path
    # is one percent-encoded segment, backslashes and all.
    assert view_url(8000, "\\\\server\\share\\file") == (
        "http://127.0.0.1:8000/view/%5C%5Cserver%5Cshare%5Cfile"
    )


def test_posix_backslash_filename_untouched():
    # deeplink runs this codec on POSIX servers too: a backslash there is a
    # legal filename character and must not be treated as a separator.
    assert view_url_path("/home/user/back\\slash.txt") == "/view/home/user/back%5Cslash.txt"


def test_bookmark_on_drive_path():
    assert view_url(8000, "C:\\Users\\x\\demo.bookmark") == (
        "http://127.0.0.1:8000/view/_bookmark?file=C%3A%2FUsers%2Fx%2Fdemo.bookmark"
    )


def test_bookmark_on_unc_path():
    assert view_url(8000, "\\\\server\\share\\demo.bookmark") == (
        "http://127.0.0.1:8000/view/_bookmark?file=%5C%5Cserver%5Cshare%5Cdemo.bookmark"
    )


def test_none_path_is_home():
    assert view_url(8000, None) == "http://127.0.0.1:8000/"
