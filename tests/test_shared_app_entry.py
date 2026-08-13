"""The shared entry-html rule (`templates/shared/app_entry.py`): which page a
folder resolves to.

The rule: `index.html` if the folder has one, else the FIRST non-hidden
top-level `.html` in name order, else nothing. Only ONE consumer is left —
`templates/claude/app.py`, whose split view frames a folder's page beside the
chat. The other two are gone: the `app` template (D264, which took its own tests
with it) and the per-path timeline mode, which asked the same predicate of an
extracted snapshot tree to decide whether a revision rendered as a page or was
browsed as a directory. This file is what survived, because the rule did.

Deliberately still SHARED rather than folded into claude/app.py: "which page is
this folder's page" is the kind of question that grows second answers the moment
it lives inside one template, which is exactly what happened while there were
three callers.

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
