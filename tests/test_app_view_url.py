"""Tests for fused_render.app.view_url_path — the Finder-open URL mapping.
Module-level and AppKit-free by design (rumps is imported lazily inside
main()), so it is testable anywhere.
"""
from fused_render.app import view_url_path


def test_regular_file_opens_as_view_path():
    assert view_url_path("/data/report.parquet") == "/explorer/view/data/report.parquet"


def test_path_segments_are_url_encoded():
    assert view_url_path("/data/my report.html") == "/explorer/view/data/my%20report.html"


def test_shell_safe_punctuation_stays_literal():
    # The shared codec matches the frontend's encodeURIComponent safe set
    # (!*'()): these characters round-trip literally, unlike the old app.py
    # body which percent-encoded them (e.g. ! -> %21).
    assert view_url_path("/data/a!b'c(d).txt") == "/explorer/view/data/a!b'c(d).txt"
