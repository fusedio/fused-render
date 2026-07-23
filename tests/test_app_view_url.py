"""Tests for fused_render.app.view_url_path — the Finder-open URL mapping
(SB-9, D99). Module-level and AppKit-free by design (rumps is imported lazily
inside main()), so it is testable anywhere.
"""
from fused_render.app import view_url_path


def test_regular_file_opens_as_view_path():
    assert view_url_path("/data/report.parquet") == "/view/data/report.parquet"


def test_path_segments_are_url_encoded():
    assert view_url_path("/data/my report.html") == "/view/data/my%20report.html"


def test_bookmark_file_routes_to_bookmark_sentinel():
    # The abs path travels as one query value: slashes encoded too.
    assert (
        view_url_path("/data/sales.bookmark")
        == "/view/_bookmark?file=%2Fdata%2Fsales.bookmark"
    )


def test_bookmark_extension_is_case_insensitive():
    assert view_url_path("/data/S.BookMark").startswith("/view/_bookmark?file=")


def test_shell_safe_punctuation_stays_literal():
    # The shared codec matches the frontend's encodeURIComponent safe set
    # (!*'()): these characters round-trip literally, unlike the old app.py
    # body which percent-encoded them (e.g. ! -> %21).
    assert view_url_path("/data/a!b'c(d).txt") == "/view/data/a!b'c(d).txt"
