"""The listing's rules, exercised directly rather than through /api/apps.

These four cases are the reason `app_listing` is a module and not a section of
`server/routers/apps.py`: every one of them is a filesystem condition — a stray
file where an app folder is expected, an entry that cannot be read, a directory
that vanishes mid-scan — and reaching them through a `TestClient` means building
a whole app to observe one `except OSError`. They were uncovered while the code
lived in the route handler, for exactly that reason.
"""
import os
import time

import pytest

from fused_render import app_listing


def _app(tag_dir, name, entry="index.html", body="<html><body>hi</body></html>"):
    d = tag_dir / name
    d.mkdir(parents=True)
    if entry:
        (d / entry).write_text(body, encoding="utf-8")
    return d


# ------------------------------------------------------------------- the walk


def test_a_loose_file_in_a_tag_folder_is_not_an_app(tmp_path):
    """A workspace is a folder a user drops things into, so a stray file lands
    beside the app folders sooner or later (`notes.txt` in `local/`). Only
    DIRECTORIES are apps; a file must not become a card whose path opens
    nothing."""
    tag = tmp_path / "local"
    _app(tag, "real")
    (tag / "notes.txt").write_text("scratch", encoding="utf-8")
    (tag / "index.html").write_text("<html></html>", encoding="utf-8")

    assert [a["name"] for a in app_listing.two_level_apps(tmp_path)] == ["real"]


def test_a_loose_file_at_the_top_level_is_not_a_tag(tmp_path):
    """The mirror of the above one level up: a file in the workspace root is not
    a tag folder to walk into."""
    _app(tmp_path / "local", "real")
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")

    assert [a["tag"] for a in app_listing.two_level_apps(tmp_path)] == ["local"]


# ------------------------------------------------------------------ the title


def test_an_unreadable_entry_has_no_title_rather_than_failing(tmp_path):
    """A title is a nicety; the app still lists without one.

    Uses a DIRECTORY at the entry's path to make the read fail for every uid —
    a chmod would be vacuous as root, the trap `test_apps_api` documents.
    """
    entry = tmp_path / "index.html"
    entry.mkdir()  # opening a directory raises OSError (IsADirectoryError)

    assert app_listing.entry_title(str(entry)) is None
    assert app_listing.entry_title(str(tmp_path / "absent.html")) is None


def test_a_directory_named_index_html_is_not_an_entry(tmp_path):
    """An entry has to be a FILE — `app_entry` checks `isfile`, not just the
    suffix. A folder called `index.html` leaves the app entry-less (it opens as a
    folder) rather than producing an `entry` the shell would hand to /render and
    fail to read.

    There is no end-to-end test here for an entry that is a real file the server
    cannot open: as root every file is readable, so the only way to stage one is
    a chmod that does nothing for uid 0 — the vacuous-test trap `test_apps_api`
    documents. `entry_title`'s failure path is covered directly above instead.
    """
    app_dir = _app(tmp_path / "local", "odd", entry=None)
    (app_dir / "index.html").mkdir()

    (listed,) = app_listing.two_level_apps(tmp_path)
    assert listed["name"] == "odd"
    assert listed["entry"] is None and listed["entry_html"] is None
    assert listed["title"] is None


# --------------------------------------------------------------- the recency


def test_recency_of_a_directory_that_is_not_there_is_none(tmp_path):
    """`None`, not an exception and not 0 — the listing sorts these last instead
    of showing a folder as last touched at the epoch."""
    assert app_listing.dir_updated_at(str(tmp_path / "gone")) is None


def test_a_child_that_vanishes_mid_scan_does_not_lose_the_folder(tmp_path,
                                                                monkeypatch):
    """The documented racing-delete case: a file listed by scandir is gone by the
    time it is stat'd. That child contributes nothing, and the scan CONTINUES —
    letting the OSError out instead would abandon the remaining children and
    report the folder as last touched whenever its own mtime says.

    Two details are what keep this from asserting nothing, both learned the hard
    way (the first version of this test passed with the per-child handler
    deleted):

    * `keep.html` is stamped in the FUTURE, so it is strictly newer than the
      directory's own mtime. Against a sentinel older than the directory, the
      assertion is satisfied by the directory alone — which is precisely what
      the outer handler falls back to, so removing the inner one changes
      nothing.
    * the vanishing child is scanned FIRST. `os.scandir` order is arbitrary; if
      the survivor came first its mtime would already be in `latest` when the
      exception escaped, and the outer handler would return the right answer for
      the wrong reason.
    """
    keep = tmp_path / "keep.html"
    keep.write_text("<html></html>", encoding="utf-8")
    (tmp_path / "racing.html").write_text("<html></html>", encoding="utf-8")
    future = time.time() + 10_000
    os.utime(keep, (future, future))

    real_scandir = os.scandir

    class _Vanishing:
        """A DirEntry whose stat fails, standing in for a file deleted between
        the readdir that listed it and the stat that reads it."""

        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name

        def stat(self, *args, **kwargs):
            if self.name == "racing.html":
                raise FileNotFoundError(2, "No such file or directory", self.name)
            return self._entry.stat(*args, **kwargs)

    class _Scandir:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            entries = [_Vanishing(e) for e in self._it.__enter__()]
            # Vanished first, survivors after — see the docstring.
            entries.sort(key=lambda e: e.name != "racing.html")
            return iter(entries)

        def __exit__(self, *exc):
            return self._it.__exit__(*exc)

    monkeypatch.setattr(os, "scandir", _Scandir)

    updated = app_listing.dir_updated_at(str(tmp_path))

    # The survivor's mtime, reached only by continuing past the vanished child.
    assert updated == pytest.approx(future, abs=1)


# --------------------------------------------------------- the preview image


def test_a_root_preview_png_is_reported_as_the_apps_thumbnail(tmp_path):
    """`preview.png` at the app folder's root is the card's picture of it — an
    authored still, chosen over the live render of the entry page."""
    d = _app(tmp_path / "local", "shot")
    (d / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    (app,) = app_listing.two_level_apps(tmp_path)
    assert app["preview_image"] == os.path.abspath(str(d / "preview.png"))


def test_no_preview_png_reports_none_rather_than_a_missing_path(tmp_path):
    """A path that isn't there would make the card render a broken image; the
    absence has to be visible as an absence."""
    _app(tmp_path / "local", "plain")

    (app,) = app_listing.two_level_apps(tmp_path)
    assert app["preview_image"] is None


def test_a_directory_named_preview_png_is_not_a_preview(tmp_path):
    """Same trap as the entry rule (`index.html` as a folder): the name alone
    is not the file, and an <img> pointed at a directory renders nothing."""
    d = _app(tmp_path / "local", "trap")
    (d / "preview.png").mkdir()

    (app,) = app_listing.two_level_apps(tmp_path)
    assert app["preview_image"] is None


def test_the_preview_name_is_exact_and_case_sensitive_extension_aside(tmp_path):
    """Only `preview.png` — not preview.jpg, not screenshot.png. One name, so a
    user adding one never has to guess which of several the card will pick."""
    d = _app(tmp_path / "local", "other")
    (d / "preview.jpg").write_bytes(b"\xff\xd8\xff")
    (d / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    (app,) = app_listing.two_level_apps(tmp_path)
    assert app["preview_image"] is None
