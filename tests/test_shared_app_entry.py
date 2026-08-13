"""The shared entry-html rule (`templates/shared/app_entry.py`): which page a
folder resolves to.

The rule: `index.html` if the folder has one, else the FIRST non-hidden
top-level `.html` in name order, else nothing. Only ONE consumer is left —
`templates/claude/app.py`, whose split view frames a folder's page beside the
chat — plus `templates/history`, which asks the same predicate of an extracted
snapshot tree to decide whether that revision renders as a page or is browsed as
a directory. The other consumer, the `app` template, is gone (D264) and took its
own tests with it; this file is what survived, because the rule did.

Deliberately still SHARED rather than folded into claude/app.py: history asks it
of a materialised tree through `shared/`, so a copy inside one template would be
a second answer to "which page is this folder's page".

The SERVER keeps a second implementation (`fused_render.app_listing.app_entry`,
behind GET /api/apps) because neither side can import the other, and as of D269
it answers identically — the parity test at the bottom of this file is what holds
the two together, and it is the reason a change here is never a change to one
file. Its only remaining divergence is that it RAISES on an unreadable directory
where this one returns None; see its docstring.

The module is exec'd standalone, exactly as production does (a template must not
import `fused_render`, SPEC PY-15 / D166), so nothing here goes through a
package import.
"""
import importlib.util
import os

import pytest

TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates")


def _load(directory, name, alias):
    path = os.path.join(directory, name + ".py")
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def entry_of():
    shared = _load(os.path.join(TEMPLATES_DIR, "shared"), "app_entry",
                   "test_app_template_shared_entry")
    return shared.entry_html


def test_index_html_wins_over_its_siblings(tmp_path, entry_of):
    for name in ("index.html", "about.html", "zzz.html"):
        (tmp_path / name).write_text("<html></html>")
    assert entry_of(str(tmp_path)) == str(tmp_path / "index.html")


def test_a_single_html_is_the_entry_whatever_it_is_called(tmp_path, entry_of):
    (tmp_path / "dashboard.html").write_text("<html></html>")
    assert entry_of(str(tmp_path)) == str(tmp_path / "dashboard.html")


def test_a_folder_with_no_html_at_all_resolves_to_nothing(tmp_path, entry_of):
    assert entry_of(str(tmp_path)) is None
    (tmp_path / "data.csv").write_text("a,b\n")
    assert entry_of(str(tmp_path)) is None


def test_several_htmls_without_an_index_resolve_to_the_first(tmp_path, entry_of):
    # This used to be None as "ambiguous", and every consumer dead-ended on it:
    # the `app` mode and the chat pane drew "no entry page" over a folder plainly
    # full of pages. First in NAME order, so the answer does not depend on
    # readdir order and two consumers cannot land on different pages.
    for name in ("zzz.html", "about.html", "middle.html"):
        (tmp_path / name).write_text("<html></html>")
    assert entry_of(str(tmp_path)) == str(tmp_path / "about.html")
    # ...and index.html still outranks all of them, wherever it sorts.
    (tmp_path / "index.html").write_text("<html></html>")
    assert entry_of(str(tmp_path)) == str(tmp_path / "index.html")


def test_hidden_and_nested_html_files_are_ignored(tmp_path, entry_of):
    (tmp_path / ".hidden.html").write_text("<html></html>")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "index.html").write_text("<html></html>")
    assert entry_of(str(tmp_path)) is None
    # ...and a hidden index.html does not count as THE index either.
    (tmp_path / ".index.html").write_text("<html></html>")
    (tmp_path / "real.html").write_text("<html></html>")
    assert entry_of(str(tmp_path)) == str(tmp_path / "real.html")


def test_a_missing_or_unreadable_directory_resolves_to_nothing(tmp_path, entry_of):
    assert entry_of(str(tmp_path / "nope")) is None
    f = tmp_path / "a.html"
    f.write_text("<html></html>")
    assert entry_of(str(f)) is None  # a file, not a directory


# The one remaining CONSUMER, pinned to the rule: the split view's left pane
# must frame the same page anything else would call the folder's page.
def test_the_claude_backend_resolves_through_the_shared_rule(tmp_path):
    split = _load(os.path.join(TEMPLATES_DIR, "claude"), "app",
                  "test_shared_app_entry_claude_backend")
    entry_of = _load(os.path.join(TEMPLATES_DIR, "shared"), "app_entry",
                     "test_shared_app_entry_direct").entry_html
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "other.html").write_text("<html></html>")
    assert split.main(dir=str(tmp_path)) == {"entry": entry_of(str(tmp_path))}
    assert split.main(dir=str(tmp_path / "nope")) == {"entry": None}
    # No argument at all must not blow up in the /api/run worker: the template
    # always passes _file, but a missing param must answer, not raise.
    assert "entry" in split.main()


# ---------------------------------------------------------------- the parity
#
# The shell's THIRD copy of this rule (frontend/src/apps/explorer/lib/app-entry.ts,
# read by the listing's preview pane) is pinned by its own bun test against the
# same cases; it cannot be exercised from here, so the two Python copies are.


def test_the_server_listing_resolves_the_same_page_as_the_shared_rule(tmp_path):
    """`app_listing.app_entry` and `shared/app_entry.entry_html` are one rule in
    two files (D269). Every shape that has ever been argued about is asked of
    both: a lone page, an index beside siblings, several pages with no index (the
    case the server used to call ambiguous and answer None), a hidden page, a
    nested one, a DIRECTORY named index.html, and a folder with nothing at all.
    """
    from fused_render import app_listing

    entry_of = _load(os.path.join(TEMPLATES_DIR, "shared"), "app_entry",
                     "test_shared_app_entry_parity").entry_html

    def folder(name, *children, dirs=()):
        d = tmp_path / name
        d.mkdir()
        for c in children:
            (d / c).write_text("<html></html>")
        for c in dirs:
            (d / c).mkdir()
        return str(d)

    cases = [
        folder("lone", "dashboard.html"),
        folder("indexed", "about.html", "index.html", "zzz.html"),
        folder("several", "zzz.html", "about.html", "middle.html"),
        folder("hidden", ".draft.html", "real.html"),
        folder("only_hidden", ".draft.html"),
        folder("nested", dirs=("sub",)),
        folder("dir_named_index", dirs=("index.html",)),
        folder("empty"),
        folder("no_pages", "notes.md"),
    ]
    for path in cases:
        assert app_listing.app_entry(path) == entry_of(path), path

    # The one deliberate divergence, asserted rather than left to drift: the
    # server RAISES for a directory it cannot list (so the walk can skip that
    # folder instead of listing it as entry-less), where the shared copy answers
    # None (a template renders a notice either way).
    gone = str(tmp_path / "gone")
    with pytest.raises(OSError):
        app_listing.app_entry(gone)
    assert entry_of(gone) is None
