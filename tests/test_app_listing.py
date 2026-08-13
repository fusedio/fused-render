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


def _names(root):
    return [a["name"] for a in app_listing.workspace_apps(root)]


def _one(root, name):
    """The single listing entry called `name`.

    The walk lists CONTAINERS too — a page-less folder at depth 1 or 2 is still
    an app card (an entry-less one), so `local/` itself is in the listing beside
    the app inside it. These tests are about one folder's reported facts, so
    they pick it out by name rather than asserting the whole listing.
    """
    (app,) = [a for a in app_listing.workspace_apps(root) if a["name"] == name]
    return app


def test_a_loose_file_in_a_tag_folder_is_not_an_app(tmp_path):
    """A workspace is a folder a user drops things into, so a stray file lands
    beside the app folders sooner or later (`notes.txt` in `local/`). Only
    DIRECTORIES are apps; a file must not become a card whose path opens
    nothing."""
    tag = tmp_path / "local"
    _app(tag, "real")
    (tag / "notes.txt").write_text("scratch", encoding="utf-8")
    (tag / "index.html").write_text("<html></html>", encoding="utf-8")

    # `local` itself is an entry-less card (a page-less folder at depth 1 is
    # still listed); `notes.txt` and `index.html` are files, so neither is.
    assert _names(tmp_path) == ["local", "real"]


def test_a_loose_file_at_the_top_level_is_not_a_tag(tmp_path):
    """The mirror of the above one level up: a file in the workspace root is not
    a tag folder to walk into."""
    _app(tmp_path / "local", "real")
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")

    # `local` (depth 1) is its own tag; the app inside it files under `local`
    # too — the tag is the first path segment at every depth.
    assert [(a["tag"], a["name"]) for a in app_listing.workspace_apps(tmp_path)] == [
        ("local", "local"), ("local", "real"),
    ]


# ------------------------------------------------------------- the depth bound


def test_a_folder_dropped_straight_into_the_workspace_is_an_app(tmp_path):
    """Depth 1 lists. This is the case the two-level walk could not see at all:
    a user saves `~/Documents/Fused/sine/sine.html` and the apps page stayed
    empty, because only `<workspace>/<tag>/<name>` counted."""
    _app(tmp_path, "sine", entry="sine.html")

    (app,) = app_listing.workspace_apps(tmp_path)
    assert app["name"] == "sine"
    assert app["entry"] == os.path.abspath(str(tmp_path / "sine" / "sine.html"))
    # Its own folder IS the top-level segment, so it is its own tag. Not "":
    # an empty tag adds a nameless chip to the page's Repo facet and a `?tag=`
    # that filters on nothing.
    assert app["tag"] == "sine"


def test_a_third_level_folder_with_an_index_html_is_an_app(tmp_path):
    """Depth 3 lists — but only on an explicit `index.html` (see the next two
    tests). The tag is still the FIRST path segment, so a third-level app files
    under the same Repo chip as its second-level neighbours."""
    _app(tmp_path / "showcase" / "sub", "bar")

    apps = {a["name"]: a for a in app_listing.workspace_apps(tmp_path)}
    assert set(apps) == {"showcase", "sub", "bar"}
    assert apps["bar"]["tag"] == "showcase"
    assert apps["bar"]["entry"].endswith(os.path.join("sub", "bar", "index.html"))


def test_a_third_level_folder_with_another_html_is_not_an_app(tmp_path):
    """The permissive "any top-level html" rule stops before depth 3. A code
    repo checked out into the workspace is full of third-level folders holding
    some .html file; only `index.html` is an author saying "this is a page"."""
    _app(tmp_path / "repo" / "docs", "guide", entry="other.html")

    assert _names(tmp_path) == ["repo", "docs"]


def test_a_third_level_folder_with_no_html_is_not_an_app(tmp_path):
    """And a page-less third-level folder is nothing at all — where the same
    folder one level up would still be an entry-less card."""
    _app(tmp_path / "repo" / "src", "utils", entry=None)

    assert _names(tmp_path) == ["repo", "src"]


def test_the_walk_stops_at_the_third_level(tmp_path):
    """Depth 4 is never looked at, index.html or not. The bound is what keeps a
    listing that runs on every page load from being a full recursive crawl."""
    _app(tmp_path / "repo" / "a" / "b", "deep")

    assert _names(tmp_path) == ["repo", "a"]


def test_an_apps_own_subfolder_is_not_a_second_app(tmp_path):
    """An app's subfolders are its assets and its extra pages, so the walk does
    not descend into a folder that already has a page. Without this, an app with
    a `sub/index.html` would list twice — once as itself and once as its own
    subfolder — and a multi-page app would scatter its pages across the grid."""
    app_dir = _app(tmp_path / "showcase", "foo")
    _app(app_dir, "sub")
    (app_dir / "assets").mkdir()

    assert _names(tmp_path) == ["showcase", "foo"]


def test_a_second_level_folder_with_any_html_still_lists(tmp_path):
    """Regression guard: depth 2 was the WHOLE of the old listing, and its rule
    is unchanged — a page under any name is an app's entry there."""
    _app(tmp_path / "local", "board", entry="dashboard.html")

    app = _one(tmp_path, "board")
    assert app["entry"].endswith("dashboard.html")


def test_a_second_level_folder_with_no_html_still_lists_entry_less(tmp_path):
    """The other half of the guard: a page-less second-level folder is still a
    card (one that opens the folder), exactly as before. Anything stricter would
    silently retire cards people already have."""
    _app(tmp_path / "local", "bare", entry=None)

    app = _one(tmp_path, "bare")
    assert app["entry"] is None and app["entry_html"] is None
    assert app["updated_at"] is not None


def test_vendor_and_package_dirs_contribute_nothing(tmp_path):
    """Pruned by name (the index's shared vendor floor) and by the leaf rule
    (macOS packages), neither listed nor descended. A workspace holds checked-out
    repos, and `node_modules` is neither an app nor 40k files worth walking."""
    _app(tmp_path / "repo" / "node_modules", "pkg")
    _app(tmp_path / "repo" / "Bundle.app", "Contents")
    _app(tmp_path / "repo", "real")

    assert _names(tmp_path) == ["repo", "real"]


def test_an_unreadable_folder_does_not_kill_the_listing(tmp_path, monkeypatch):
    """A folder the walk cannot list costs its own subtree and nothing else.

    Monkeypatched rather than chmod'ed so it holds for every uid — mode 0 does
    not stop root, the vacuous-test trap `test_apps_api` documents.
    """
    _app(tmp_path / "local", "ok")
    (tmp_path / "local" / "locked").mkdir()
    _app(tmp_path / "local" / "locked", "hidden-by-the-error")

    real = os.listdir

    def refuse(path, *a, **kw):
        if os.path.basename(str(path)) == "locked":
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "listdir", refuse)

    # `locked` itself is skipped (its entry could not be resolved) along with
    # everything below it; every sibling still lists.
    assert _names(tmp_path) == ["local", "ok"]


def test_a_folder_that_becomes_unreadable_mid_walk_costs_only_itself(tmp_path,
                                                                    monkeypatch):
    """The walk's OWN listing guard, distinct from the one around `app_entry`.

    A page-less folder is listed as a card and then DESCENDED INTO, and those are
    two separate reads: the entry lookup can succeed and the descent still fail
    (permissions changed, folder deleted, a network volume going away in
    between). Without the guard the whole page 500s over one folder — so the
    stand-in refuses `probed`'s SECOND listing, the one the descent does.
    """
    _app(tmp_path / "local", "ok")
    _app(tmp_path / "local" / "probed", "deep")  # `probed` itself has no page

    real = os.listdir
    seen = set()

    def refuse_on_second_look(path, *a, **kw):
        if os.path.basename(str(path)) == "probed":
            if str(path) in seen:
                raise PermissionError(13, "Permission denied", str(path))
            seen.add(str(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "listdir", refuse_on_second_look)

    # `probed` still lists (its entry resolved, as None); `deep` is lost with
    # the failed descent; `ok` is untouched.
    assert _names(tmp_path) == ["local", "ok", "probed"]


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

    listed = _one(tmp_path, "odd")
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

    app = _one(tmp_path, "shot")
    assert app["preview_image"] == os.path.abspath(str(d / "preview.png"))


def test_no_preview_png_reports_none_rather_than_a_missing_path(tmp_path):
    """A path that isn't there would make the card render a broken image; the
    absence has to be visible as an absence."""
    _app(tmp_path / "local", "plain")

    app = _one(tmp_path, "plain")
    assert app["preview_image"] is None


def test_a_directory_named_preview_png_is_not_a_preview(tmp_path):
    """Same trap as the entry rule (`index.html` as a folder): the name alone
    is not the file, and an <img> pointed at a directory renders nothing."""
    d = _app(tmp_path / "local", "trap")
    (d / "preview.png").mkdir()

    app = _one(tmp_path, "trap")
    assert app["preview_image"] is None


def test_only_that_one_name_is_a_preview(tmp_path):
    """Only `preview.png` — not preview.jpg, not screenshot.png. One name, so a
    user adding one never has to guess which of several the card will pick."""
    d = _app(tmp_path / "local", "other")
    (d / "preview.jpg").write_bytes(b"\xff\xd8\xff")
    (d / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    app = _one(tmp_path, "other")
    assert app["preview_image"] is None


def test_the_name_match_is_case_sensitive_on_every_filesystem(tmp_path):
    """`Preview.png` is not `preview.png`, and it must not become one on a
    case-INSENSITIVE filesystem (macOS, Windows) — an `os.path.isfile` probe
    would say yes there and no on ext4, so the same folder would get a thumbnail
    on one machine and not another. The frontend's own peek rule compares the
    name exactly, and this is the half that has to meet it."""
    d = _app(tmp_path / "local", "shouty")
    (d / "Preview.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    app = _one(tmp_path, "shouty")
    assert app["preview_image"] is None


def test_an_empty_preview_file_is_no_preview(tmp_path):
    """An interrupted write leaves a zero-byte file. `isfile` is happily True
    for it, and the card would then render a permanently broken <img> with no
    way back to the live render — the fallbacks only exist while this is None."""
    d = _app(tmp_path / "local", "torn")
    (d / "preview.png").write_bytes(b"")

    app = _one(tmp_path, "torn")
    assert app["preview_image"] is None


# ------------------------------------------------------------- the category


def test_metadata_category_is_reported(tmp_path):
    """A `metadata.json` with a `category` string — the showcase repo's per-app
    shape — surfaces on the listing entry for the UI's category filter."""
    d = _app(tmp_path / "showcase", "mapped")
    (d / "metadata.json").write_text(
        '{"schema": 1, "name": "Mapped", "category": "geospatial"}', encoding="utf-8"
    )

    app = _one(tmp_path, "mapped")
    assert app["category"] == "geospatial"


def test_no_metadata_json_means_no_category(tmp_path):
    """An app without metadata.json carries None — it only ever appears under
    the UI's "All" chip, never in a named category."""
    _app(tmp_path / "local", "plain")

    app = _one(tmp_path, "plain")
    assert app["category"] is None


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        '{"schema": 1}',  # no category field
        '{"category": 7}',  # not a string
        '{"category": "  "}',  # blank after strip
        '["category"]',  # not an object
    ],
)
def test_malformed_or_missing_category_degrades_to_none(tmp_path, body):
    """The listing never fails on a folder's contents: a malformed metadata.json
    or a non-string/blank category is just "no category"."""
    d = _app(tmp_path / "local", "odd")
    (d / "metadata.json").write_text(body, encoding="utf-8")

    app = _one(tmp_path, "odd")
    assert app["category"] is None
