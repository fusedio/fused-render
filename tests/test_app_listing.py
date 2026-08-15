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


def _app(tag_dir, name, entry: str | None = "index.html",
         body="<html><body>hi</body></html>"):
    d = tag_dir / name
    d.mkdir(parents=True)
    if entry:
        (d / entry).write_text(body, encoding="utf-8")
    return d


# ------------------------------------------------------------------- the walk


def _names(root):
    return [a["name"] for a in app_listing.workspace_apps(root)]


def test_a_loose_file_in_a_tag_folder_is_not_an_app(tmp_path):
    """A workspace is a folder a user drops things into, so a stray file lands
    beside the app folders sooner or later (`notes.txt` in `local/`). Only
    DIRECTORIES are apps; a file must not become a card whose path opens
    nothing."""
    tag = tmp_path / "local"
    _app(tag, "real")
    (tag / "notes.txt").write_text("scratch", encoding="utf-8")
    (tag / "index.html").write_text("<html></html>", encoding="utf-8")

    # `local` lists too — it has a page (that `index.html`), which is what makes
    # a top-level folder an app; `notes.txt` is a file, so it is nothing.
    assert _names(tmp_path) == ["local", "real"]


def test_a_loose_file_at_the_top_level_is_not_a_tag(tmp_path):
    """The mirror of the above one level up: a file in the workspace root is not
    a tag folder to walk into."""
    _app(tmp_path / "local", "real")
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")

    assert [a["tag"] for a in app_listing.workspace_apps(tmp_path)] == ["local"]


# ------------------------------------------------------------- the depth bound


def test_a_folder_dropped_straight_into_the_workspace_is_an_app(tmp_path):
    """Depth 1 lists WHEN IT HAS A PAGE. This is the case the two-level walk
    could not see at all: a user saves `~/Documents/Fused/sine/sine.html` and the
    apps page stayed empty, because only `<workspace>/<tag>/<name>` counted.

    Any `*.html`, not `index.html` — the depth-3 requirement does not apply here.
    `sine/sine.html` and `how_it_works/explainer.html` are both real folders in a
    real workspace and neither is named index.
    """
    _app(tmp_path, "sine", entry="sine.html")

    (app,) = app_listing.workspace_apps(tmp_path)
    assert app["name"] == "sine"
    assert app["entry"] == os.path.abspath(str(tmp_path / "sine" / "sine.html"))
    # Its own folder IS the top-level segment, so it is its own tag. Not "":
    # an empty tag adds a nameless chip to the page's Repo facet and a `?tag=`
    # that filters on nothing.
    assert app["tag"] == "sine"


def test_a_page_less_top_level_folder_is_a_shelf_not_an_app(tmp_path):
    """The guard for the mistake this rule was written to correct.

    A folder at depth 1 with no page of its own is where apps LIVE —
    `examples/`, `local/`, `showcase/` — not an app. Listing it puts a blank,
    title-less card on the apps page for every tag folder the user has, named
    after a chip that is already in the Repo facet right above the grid. Depth 2
    now answers the same way, for the same reason (see the shelf test below); it
    is asserted separately because it was the level that got this wrong twice.
    """
    _app(tmp_path / "local", "real")
    (tmp_path / "empty").mkdir()

    assert _names(tmp_path) == ["real"]


def test_a_top_level_folder_with_a_page_lists_and_is_still_walked(tmp_path):
    """A page at depth 1 makes the folder an app; it does NOT stop the walk.

    This is the one place the "don't descend into an app" rule is deliberately
    not applied, and it is load-bearing: a tag folder can perfectly well hold a
    landing page (a `showcase/index.html` in a cloned repo), and treating that
    page as proof its subfolders are mere assets would delete every app in the
    repo from the listing. Both claims are asserted together so neither can be
    "simplified" away on its own.
    """
    _app(tmp_path / "showcase", "bar")
    (tmp_path / "showcase" / "index.html").write_text("<html></html>",
                                                      encoding="utf-8")

    apps = {a["name"]: a for a in app_listing.workspace_apps(tmp_path)}
    assert set(apps) == {"showcase", "bar"}
    assert apps["showcase"]["entry"].endswith("index.html")
    assert apps["bar"]["tag"] == "showcase"


def test_a_third_level_folder_with_an_index_html_is_an_app(tmp_path):
    """Depth 3 lists — but only on an explicit `index.html` (see the next two
    tests). The tag is still the FIRST path segment, so a third-level app files
    under the same Repo chip as its second-level neighbours."""
    _app(tmp_path / "showcase" / "sub", "bar")

    apps = {a["name"]: a for a in app_listing.workspace_apps(tmp_path)}
    # `showcase` and `sub` are page-less shelves; only `bar` is an app.
    assert set(apps) == {"bar"}
    assert apps["bar"]["tag"] == "showcase"
    assert apps["bar"]["entry"].endswith(os.path.join("sub", "bar", "index.html"))


def test_a_third_level_folder_with_another_html_is_not_an_app(tmp_path):
    """The permissive "any top-level html" rule stops before depth 3. A code
    repo checked out into the workspace is full of third-level folders holding
    some .html file; only `index.html` is an author saying "this is a page"."""
    _app(tmp_path / "repo" / "docs", "guide", entry="other.html")

    # `repo` and `docs` are page-less shelves, and `guide`'s page is not an
    # index — so this tree has no app in it at all.
    assert _names(tmp_path) == []


def test_a_third_level_folder_with_no_html_is_not_an_app(tmp_path):
    """And a page-less third-level folder is nothing at all — same answer the
    shallower levels give, reached by a stricter rule: at depth 3 even a page is
    not enough unless it is `index.html`."""
    _app(tmp_path / "repo" / "src", "utils", entry=None)

    assert _names(tmp_path) == []


def test_the_walk_stops_at_the_third_level(tmp_path):
    """Depth 4 is never looked at, index.html or not. The bound is what keeps a
    listing that runs on every page load from being a full recursive crawl."""
    _app(tmp_path / "repo" / "a" / "b", "deep")

    # `repo` and `a` are page-less shelves; `b` is at depth 3 with no page of
    # its own; `deep` sits at depth 4, which is never looked at.
    assert _names(tmp_path) == []


def test_an_apps_own_subfolder_is_not_a_second_app(tmp_path):
    """An app's subfolders are its assets and its extra pages, so the walk does
    not descend into a folder that already has a page. Without this, an app with
    a `sub/index.html` would list twice — once as itself and once as its own
    subfolder — and a multi-page app would scatter its pages across the grid."""
    app_dir = _app(tmp_path / "showcase", "foo")
    _app(app_dir, "sub")
    (app_dir / "assets").mkdir()

    assert _names(tmp_path) == ["foo"]


def test_a_stray_html_at_the_second_level_does_not_hide_the_apps_below_it(tmp_path):
    """A page-named-anything must NOT claim the folder's whole subtree.

    Only `index.html` is an author declaring "this folder IS the page and what is
    below it is my assets". A repo cloned to `<ws>/local/<repo>/` routinely ships
    a `coverage.html`, a `docs.html`, a report — and treating one of those as that
    declaration deleted EVERY app in the repo from the grid, which is the exact
    failure the depth-1 always-descend exception exists to prevent, one level
    further down where user-cloned repos actually land.
    """
    repo = tmp_path / "local" / "repo"
    _app(repo, "dash")
    _app(repo, "maps")
    (repo / "coverage.html").write_text("<html></html>", encoding="utf-8")

    # The repo itself is a card (depth 2 lists anything, and it does have a
    # page), and its two apps are still there.
    assert _names(tmp_path) == ["repo", "dash", "maps"]


def test_an_index_html_at_the_second_level_does_claim_the_subtree(tmp_path):
    """The other side of the rule: rename that page to `index.html` and the
    folder owns what is below it. This is the pair — the two tests together are
    the whole rule, and either one alone reads as an arbitrary choice."""
    repo = tmp_path / "local" / "repo"
    _app(repo, "dash")
    (repo / "index.html").write_text("<html></html>", encoding="utf-8")

    assert _names(tmp_path) == ["repo"]


def test_a_second_level_folder_with_any_html_still_lists(tmp_path):
    """Regression guard: depth 2 was the WHOLE of the old listing, and its rule
    is unchanged — a page under any name is an app's entry there."""
    _app(tmp_path / "local", "board", entry="dashboard.html")

    (app,) = app_listing.workspace_apps(tmp_path)
    assert app["entry"].endswith("dashboard.html")


def test_a_second_level_folder_with_no_page_is_a_shelf_not_an_app(tmp_path):
    """INVERTED on purpose — this test used to assert the opposite, and the
    reason it flipped is worth more than the assertion.

    The two-level walk emitted an entry-less folder as a card that opened a
    directory, and the recursive walk kept that at depth 2 to avoid retiring
    anyone's cards. A real workspace showed what the rule actually produced: a
    `sandbox/` holding ten PEOPLE's folders, with the 14 real apps one level
    inside them, drew a blank title-less card for every person right beside the
    apps it now also found. Depth 2 is as often a shelf level as depth 1 is.

    So: a page is what makes a folder an app, at every level. Do not "restore"
    this — the entry-less card was never a feature, it was the two-level walk
    having nowhere deeper to look.
    """
    _app(tmp_path / "local", "bare", entry=None)
    _app(tmp_path / "local", "real")

    assert _names(tmp_path) == ["real"]


def test_the_shelf_of_people_folders_lists_the_apps_and_not_the_people(tmp_path):
    """The user-reported shape, staged exactly: `sandbox/<person>/<app>/`.

    Nothing at the person level, `index.html` in each app. The apps list; the
    person does not; `sandbox` does not.
    """
    aman = tmp_path / "sandbox" / "Aman"
    _app(aman, "alphaearth")
    _app(aman, "clay")

    apps = app_listing.workspace_apps(tmp_path)
    assert [a["name"] for a in apps] == ["alphaearth", "clay"]
    # Both still file under the top-level segment, so one Repo chip covers them.
    assert {a["tag"] for a in apps} == {"sandbox"}


def test_vendor_dirs_contribute_nothing_whatever_their_case(tmp_path):
    """Pruned by name, neither listed nor descended: a workspace holds
    checked-out repos, and `node_modules` is neither an app nor 40k files worth
    walking on a page load.

    Case-folded, which the index's own set lookup is not: a `NODE_MODULES` that
    got walked while `node_modules` was pruned is a difference nobody can see
    until the same folder lists differently on two machines.

    The two spellings go under DIFFERENT parents on purpose. Created as siblings
    they are one directory on a case-insensitive filesystem (macOS, Windows), so
    the second `mkdir` lands inside the first and the case half of this test
    asserts nothing at all — which is exactly how it passed with the rule
    mutated back to case-sensitive.
    """
    _app(tmp_path / "lower" / "node_modules", "pkg")
    _app(tmp_path / "upper" / "NODE_MODULES", "pkg2")
    _app(tmp_path / "mixed" / "__PyCache__", "pkg3")
    _app(tmp_path / "lower", "real")

    assert _names(tmp_path) == ["real"]


def test_an_opaque_container_is_dropped_but_a_dot_app_folder_with_a_page_is_not(
    tmp_path,
):
    """The leaf rule is NARROWED here, and this is why.

    `index/ignore.is_leaf_dir` also covers `.app`, because for the INDEX a macOS
    package must be invisible. On the apps page invisibility is the wrong
    failure: a user's folder named `todo.app` would be missing from their grid
    with nothing to explain it. So a `.app` with a page is an app, a `.app`
    without one is a bundle (no card), neither is ever descended, and only the
    suffixes nobody names a project after are dropped outright.
    """
    _app(tmp_path / "local", "todo.app")                    # a folder, with a page
    bundle = _app(tmp_path / "local", "Sample.app", entry=None)  # a real bundle
    _app(bundle, "Contents")                                # its innards
    _app(tmp_path / "local", "Some.framework")
    _app(tmp_path / "local", "Photos.photoslibrary")

    # `todo.app` lists; the bundle, its `Contents`, and the two opaque
    # containers contribute nothing at all.
    assert _names(tmp_path) == ["todo.app"]


def test_symlinked_directories_are_listed_but_never_walked(tmp_path):
    """A link is a card, not a doorway.

    It still LISTS when it resolves to a folder with a page — that worked under
    the two-level walk and linking an app folder into the workspace is a
    reasonable thing to do. But the walk stops there, and at three levels deep
    that matters twice over: `<ws>/loop -> <ws>` would otherwise duplicate the
    whole listing under a bogus tag (and `loop/loop` under that), and a link into
    a remote mount would pay three levels of kernel listing on every page load.
    """
    _app(tmp_path / "local", "real")
    elsewhere = tmp_path.parent / "elsewhere"
    _app(elsewhere, "linked")
    os.symlink(elsewhere / "linked", tmp_path / "local" / "link-to-app")
    os.symlink(tmp_path, tmp_path / "loop")  # an ancestor loop

    names = _names(tmp_path)
    # The link to an app folder is a card; the ancestor loop is not (the
    # workspace root has no page of its own) and, crucially, nothing under it
    # was walked — no second `real`, no `loop/loop`.
    assert names == ["link-to-app", "real"]


def test_nothing_under_a_mount_is_even_stat_ed(tmp_path, monkeypatch):
    """MountGuard, consulted BEFORE the first syscall on a candidate.

    A single kernel listing on a flat remote prefix has wedged an rclone mount in
    production, and a `stat` on a wedged mount blocks the thread serving
    /api/apps. So this asserts the ORDER, not just the outcome: the stand-ins
    below fail the test if the walk touches the guarded path at all.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))  # a home is guarded whole
    _app(tmp_path / "local", "real")
    _app(home / "branches" / "main", "mounts")

    for fn in ("isdir", "islink"):
        real = getattr(os.path, fn)
        monkeypatch.setattr(os.path, fn, lambda p, _r=real, *a, **kw: (
            pytest.fail(f"os.path.{fn} on a guarded path: {p}")
            if str(home) in str(p) else _r(p)
        ))
    real_listdir = os.listdir

    def listdir(path, *a, **kw):
        if str(home) in str(path):
            pytest.fail(f"os.listdir on a guarded path: {path}")
        return real_listdir(path, *a, **kw)

    monkeypatch.setattr(os, "listdir", listdir)

    assert _names(tmp_path) == ["real"]


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
    assert _names(tmp_path) == ["ok"]


def test_a_folder_that_becomes_unreadable_mid_walk_costs_only_itself(tmp_path,
                                                                    monkeypatch):
    """The walk's OWN listing guard, distinct from the one around `app_entry`.

    A page-less folder is a shelf, and a shelf is still WALKED — so its entry
    lookup and its descent are two separate reads, and the second one can fail on
    its own (permissions changed, folder deleted, a network volume going away in
    between). Without the guard the whole page 500s over one folder, so the
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

    # `deep` is lost with the failed descent and `probed` is a page-less shelf,
    # so neither appears — but `ok` is untouched and the listing still answers.
    assert _names(tmp_path) == ["ok"]


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
    suffix. A folder called `index.html` therefore resolves to NO entry, and
    since a page is what makes a folder an app, the folder does not list at all
    rather than producing an `entry` the shell would hand to /render and fail to
    read. (It used to list as an entry-less card; the card is what changed, not
    the entry rule, which is asserted directly here so the two cannot drift.)

    There is no end-to-end test here for an entry that is a real file the server
    cannot open: as root every file is readable, so the only way to stage one is
    a chmod that does nothing for uid 0 — the vacuous-test trap `test_apps_api`
    documents. `entry_title`'s failure path is covered directly above instead.
    """
    app_dir = _app(tmp_path / "local", "odd", entry=None)
    (app_dir / "index.html").mkdir()

    assert app_listing.app_entry(str(app_dir)) is None
    assert app_listing.workspace_apps(tmp_path) == []


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

    (app,) = app_listing.workspace_apps(tmp_path)
    assert app["preview_image"] == os.path.abspath(str(d / "preview.png"))


def test_no_preview_png_reports_none_rather_than_a_missing_path(tmp_path):
    """A path that isn't there would make the card render a broken image; the
    absence has to be visible as an absence."""
    _app(tmp_path / "local", "plain")

    (app,) = app_listing.workspace_apps(tmp_path)
    assert app["preview_image"] is None


def test_a_directory_named_preview_png_is_not_a_preview(tmp_path):
    """Same trap as the entry rule (`index.html` as a folder): the name alone
    is not the file, and an <img> pointed at a directory renders nothing."""
    d = _app(tmp_path / "local", "trap")
    (d / "preview.png").mkdir()

    (app,) = app_listing.workspace_apps(tmp_path)
    assert app["preview_image"] is None


def test_only_that_one_name_is_a_preview(tmp_path):
    """Only `preview.png` — not preview.jpg, not screenshot.png. One name, so a
    user adding one never has to guess which of several the card will pick."""
    d = _app(tmp_path / "local", "other")
    (d / "preview.jpg").write_bytes(b"\xff\xd8\xff")
    (d / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    (app,) = app_listing.workspace_apps(tmp_path)
    assert app["preview_image"] is None


def test_the_name_match_is_case_sensitive_on_every_filesystem(tmp_path):
    """`Preview.png` is not `preview.png`, and it must not become one on a
    case-INSENSITIVE filesystem (macOS, Windows) — an `os.path.isfile` probe
    would say yes there and no on ext4, so the same folder would get a thumbnail
    on one machine and not another. The frontend's own peek rule compares the
    name exactly, and this is the half that has to meet it."""
    d = _app(tmp_path / "local", "shouty")
    (d / "Preview.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    (app,) = app_listing.workspace_apps(tmp_path)
    assert app["preview_image"] is None


def test_an_empty_preview_file_is_no_preview(tmp_path):
    """An interrupted write leaves a zero-byte file. `isfile` is happily True
    for it, and the card would then render a permanently broken <img> with no
    way back to the live render — the fallbacks only exist while this is None."""
    d = _app(tmp_path / "local", "torn")
    (d / "preview.png").write_bytes(b"")

    (app,) = app_listing.workspace_apps(tmp_path)
    assert app["preview_image"] is None


# ------------------------------------------------------------- the category


def test_metadata_category_is_reported(tmp_path):
    """A `metadata.json` with a `category` string — the showcase repo's per-app
    shape — surfaces on the listing entry for the UI's category filter."""
    d = _app(tmp_path / "showcase", "mapped")
    (d / "metadata.json").write_text(
        '{"schema": 1, "name": "Mapped", "category": "geospatial"}', encoding="utf-8"
    )

    (app,) = app_listing.workspace_apps(tmp_path)
    assert app["category"] == "geospatial"


def test_no_metadata_json_means_no_category(tmp_path):
    """An app without metadata.json carries None — it only ever appears under
    the UI's "All" chip, never in a named category."""
    _app(tmp_path / "local", "plain")

    (app,) = app_listing.workspace_apps(tmp_path)
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

    (app,) = app_listing.workspace_apps(tmp_path)
    assert app["category"] is None


# --------------------------------------------------- is_workspace_app_entry
#
# The recents filter's question: "would the walk report this file as some
# app's entry?" — a targeted re-derivation of the walk, so parity with
# `workspace_apps` is the property, held the same way `app_entry`'s
# shared-template copy is.


def _walk_entries(root):
    return {os.path.normcase(a["entry"]) for a in app_listing.workspace_apps(str(root))
            if a["entry"]}


def _assert_parity(root, fs_path):
    """The helper and the walk must agree about `fs_path`."""
    expected = os.path.normcase(os.path.abspath(str(fs_path))) in _walk_entries(root)
    assert app_listing.is_workspace_app_entry(str(fs_path), str(root)) is expected
    return expected


def test_entry_check_true_for_a_listed_apps_entry(tmp_path):
    d = _app(tmp_path / "local", "demo")
    assert _assert_parity(tmp_path, d / "index.html") is True


def test_entry_check_true_for_a_depth_one_app(tmp_path):
    d = tmp_path / "sine"
    d.mkdir()
    (d / "sine.html").write_text("<html></html>", encoding="utf-8")
    assert _assert_parity(tmp_path, d / "sine.html") is True


def test_entry_check_false_for_a_non_entry_page_in_an_app(tmp_path):
    """Only the ENTRY is filtered from recents — a secondary page in the same
    folder still records."""
    d = _app(tmp_path / "local", "demo")
    (d / "other.html").write_text("<html></html>", encoding="utf-8")
    assert _assert_parity(tmp_path, d / "other.html") is False
    assert _assert_parity(tmp_path, d / "index.html") is True


def test_entry_check_false_outside_the_workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    d = tmp_path / "elsewhere"
    d.mkdir()
    (d / "index.html").write_text("<html></html>", encoding="utf-8")
    assert app_listing.is_workspace_app_entry(str(d / "index.html"), str(root)) is False


def test_entry_check_false_for_a_root_level_file(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    assert _assert_parity(tmp_path, tmp_path / "index.html") is False


def test_entry_check_depth_three_requires_index(tmp_path):
    deep = tmp_path / "tag" / "shelf" / "app"
    deep.mkdir(parents=True)
    (deep / "index.html").write_text("<html></html>", encoding="utf-8")
    other = tmp_path / "tag" / "shelf" / "loose"
    other.mkdir()
    (other / "page.html").write_text("<html></html>", encoding="utf-8")
    assert _assert_parity(tmp_path, deep / "index.html") is True
    assert _assert_parity(tmp_path, other / "page.html") is False


def test_entry_check_false_below_max_depth(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "index.html").write_text("<html></html>", encoding="utf-8")
    assert _assert_parity(tmp_path, deep / "index.html") is False


def test_entry_check_false_under_an_index_owned_subtree(tmp_path):
    """A depth-2 folder whose entry is index.html owns its subtree — the walk
    never descends, so a page below it is not an app's entry."""
    owner = _app(tmp_path / "local", "owner")  # local/owner/index.html
    sub = owner / "sub"
    sub.mkdir()
    (sub / "index.html").write_text("<html></html>", encoding="utf-8")
    assert _assert_parity(tmp_path, sub / "index.html") is False
    assert _assert_parity(tmp_path, owner / "index.html") is True


def test_entry_check_descends_through_a_depth_one_index_folder(tmp_path):
    """Depth 1 descends unconditionally, even past its own index.html."""
    shelf = tmp_path / "showcase"
    shelf.mkdir()
    (shelf / "index.html").write_text("<html></html>", encoding="utf-8")
    child = shelf / "app"
    child.mkdir()
    (child / "index.html").write_text("<html></html>", encoding="utf-8")
    assert _assert_parity(tmp_path, shelf / "index.html") is True
    assert _assert_parity(tmp_path, child / "index.html") is True


def test_entry_check_false_in_pruned_and_hidden_dirs(tmp_path):
    hidden = tmp_path / ".secret" / "app"
    hidden.mkdir(parents=True)
    (hidden / "index.html").write_text("<html></html>", encoding="utf-8")
    vend = tmp_path / "node_modules" / "pkg"
    vend.mkdir(parents=True)
    (vend / "index.html").write_text("<html></html>", encoding="utf-8")
    assert _assert_parity(tmp_path, hidden / "index.html") is False
    assert _assert_parity(tmp_path, vend / "index.html") is False


def test_entry_check_false_under_an_unlistable_shelf(tmp_path):
    """The walk resolves each candidate's entry before descending, so an
    unlistable (execute-only) depth-1 shelf ends its subtree there — the
    helper must probe listability at every depth, not just where the index
    rule applies."""
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("needs POSIX directory permissions and a non-root user")
    shelf = tmp_path / "shelf"
    app = shelf / "app"
    app.mkdir(parents=True)
    (app / "index.html").write_text("<html></html>", encoding="utf-8")
    shelf.chmod(0o111)
    try:
        assert _assert_parity(tmp_path, app / "index.html") is False
    finally:
        shelf.chmod(0o755)


def test_entry_check_false_under_an_unlistable_root(tmp_path):
    """The walk's first act is listdir(root) — an execute-only workspace root
    lists no apps, so the helper must answer False for anything under it."""
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("needs POSIX directory permissions and a non-root user")
    root = tmp_path / "ws"
    app = root / "app"
    app.mkdir(parents=True)
    (app / "index.html").write_text("<html></html>", encoding="utf-8")
    root.chmod(0o111)
    try:
        assert _assert_parity(root, app / "index.html") is False
    finally:
        root.chmod(0o755)


def test_entry_check_false_past_a_symlinked_ancestor(tmp_path):
    """A symlinked dir LISTS if it holds a page, but is never walked past."""
    real = tmp_path / "outside"
    (real / "app").mkdir(parents=True)
    (real / "app" / "index.html").write_text("<html></html>", encoding="utf-8")
    (real / "top.html").write_text("<html></html>", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    link = root / "linked"
    os.symlink(str(real), str(link))
    # The link itself has a page -> it lists, and its entry passes the check.
    assert _assert_parity(root, link / "top.html") is True
    # But nothing BELOW a symlink is walked.
    assert _assert_parity(root, link / "app" / "index.html") is False
