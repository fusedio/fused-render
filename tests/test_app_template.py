"""The `app` template: an app folder rendered plainly, full-bleed, for USING it.

Three things are tested here, all of them the parts no typecheck or manual
click-through covers:

  * the condition gate — the mode is bound to the universal "/" directory key,
    so it is offered for every directory the explorer stats and the gate is the
    only thing narrowing that to real app folders (<workspace>/<tag>/<name>);
  * the shared entry rule (`shared/app_entry.py`) the template's `app.py` and
    `claude_split/app.py` now both delegate to — index.html wins, else the
    folder's single non-hidden top-level .html, else nothing;
  * the template document itself: chromeless (the shell's preview header already
    carries the app name and the mode switcher) and pointed at `/render`, not
    `/embed`, which would nest the app inside a second React shell.

The template modules are exec'd standalone, exactly as production does
(conditions via server._run_condition, backends via /api/run), so nothing here
goes through a package import.
"""
import importlib.util
import os

import pytest

TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates")
TEMPLATE_DIR = os.path.join(TEMPLATES_DIR, "app")


def _load(directory, name, alias):
    path = os.path.join(directory, name + ".py")
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE_DIR", str(fdir))
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", str(tmp_path / "home" / "mounts"))
    return fdir


@pytest.fixture()
def gate():
    return _load(TEMPLATE_DIR, "condition", "test_app_template_condition").main


# ------------------------------------------------------------------- the gate


def test_an_app_folder_is_offered(workspace, gate):
    app = workspace / "local" / "demo"
    app.mkdir(parents=True)
    assert gate(str(app)) is True
    # Unlike `versions`, no git repo is required: using an app has nothing to do
    # with whether its history is being tracked.
    assert not (app / ".git").exists()


def test_a_folder_that_does_not_exist_yet_is_still_offered(workspace, gate):
    # Pure path arithmetic, by design (the gate runs for every directory the
    # explorer stats, some of them on remote mounts) — so it never probes.
    assert gate(str(workspace / "local" / "notyet")) is True


@pytest.mark.parametrize("rel", [
    "",                       # the workspace root itself
    "local",                  # a tag folder — one level too shallow
    "local/demo/src",         # inside an app — one level too deep
    "local/demo/src/deeper",
])
def test_anything_other_than_exactly_two_levels_is_refused(workspace, gate, rel):
    target = workspace if rel == "" else workspace / rel
    assert gate(str(target)) is False


def test_a_directory_outside_the_workspace_is_refused(workspace, gate, tmp_path):
    assert gate(str(tmp_path / "elsewhere" / "a" / "b")) is False


@pytest.mark.parametrize("rel", [".hidden/demo", "local/.venv"])
def test_a_dot_prefixed_segment_is_refused(workspace, gate, rel):
    # The apps API skips dot-prefixed tags and projects when listing cards;
    # `tag/.venv` must not sneak the mode in through the gate either.
    assert gate(str(workspace / rel)) is False


def test_a_mount_backed_path_is_refused(workspace, gate, tmp_path, monkeypatch):
    # An app is by definition a local folder, and the shape of a mount path
    # (<mounts>/<remote>/<dir>) is exactly two levels down, so without this
    # refusal every second-level directory on every remote would offer the mode.
    mounts = tmp_path / "home" / "mounts"
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE_DIR", str(mounts))
    target = mounts / "s3demo" / "project"
    assert gate(str(target)) is False


def test_the_gate_fails_closed(workspace, gate):
    # Anything the gate cannot answer must hide the mode, never raise into the
    # stat path — _run_condition calls this for every directory the user opens.
    assert gate(None) is False
    assert gate(b"/bytes/not/str") is False


# ------------------------------------------------- the shared entry-html rule


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


def test_no_html_and_several_htmls_both_resolve_to_nothing(tmp_path, entry_of):
    assert entry_of(str(tmp_path)) is None
    (tmp_path / "data.csv").write_text("a,b\n")
    assert entry_of(str(tmp_path)) is None
    (tmp_path / "a.html").write_text("<html></html>")
    (tmp_path / "b.html").write_text("<html></html>")
    # Ambiguous without an index.html: the UI opens the folder instead.
    assert entry_of(str(tmp_path)) is None


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


# --------------------------------------------------------------- the backends


def test_the_template_backend_reports_the_resolved_entry(tmp_path):
    backend = _load(TEMPLATE_DIR, "app", "test_app_template_backend")
    (tmp_path / "index.html").write_text("<html></html>")
    assert backend.main(dir=str(tmp_path)) == {"entry": str(tmp_path / "index.html")}
    assert backend.main(dir=str(tmp_path / "nope")) == {"entry": None}
    # No argument at all must not blow up in the /api/run worker: the template
    # always passes _file, but a missing param must answer, not raise.
    assert "entry" in backend.main()


def test_claude_split_resolves_entries_the_same_way(tmp_path):
    # The two backends answer the same question and now share one rule; a drift
    # between them means the split view and the plain view open different pages.
    plain = _load(TEMPLATE_DIR, "app", "test_app_template_backend_b")
    split = _load(os.path.join(TEMPLATES_DIR, "claude_split"), "app",
                  "test_app_template_claude_split_backend")
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "other.html").write_text("<html></html>")
    assert plain.main(dir=str(tmp_path)) == split.main(dir=str(tmp_path))


# --------------------------------------------------------------- the document


def _template_html():
    with open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8") as f:
        return f.read()


def test_the_template_renders_the_app_through_render_not_embed():
    html = _template_html()
    assert '"/render?path="' in html
    # /embed serves the React shell, which would nest the app one iframe deeper
    # inside a second copy of the chrome. (Matched as a URL literal: the
    # template's own comment explains the choice and names the route.)
    assert '"/embed' not in html


def test_the_template_adds_no_chrome_of_its_own():
    html = _template_html()
    # The shell's preview header already carries the app name and the pinned
    # mode switcher; a bar in here would be a second one.
    assert "<iframe" in html
    assert html.count("<iframe") == 1
    # Nothing of the sidebar templates' split machinery.
    for absent in ("divider", "col-resize", 'params.get("split")'):
        assert absent not in html, absent


def test_the_template_keeps_live_reload():
    # The point of this mode is using the app while editing it, so unlike the
    # sidebar templates it must not switch auto-reload off.
    assert "autoReload(" not in _template_html()
