"""The `claude_split` gate and left pane across the two target KINDS (D230).

The split view used to be the app builder's alone: its gate offered it for a
project folder only, and its left pane always resolved that folder's app entry.
D230 bound it to 47 file keys as well, because the annotation / app_state
machinery lives here and nowhere else — chatting about a standalone file with
those tools is the whole point — so the gate now answers for a file too and the
pane renders that file in its OWN default view.

Two things are worth pinning down, and each of them broke once:

* the FILE branch of the gate must test `isfile`, not `not isdir`. The loose form
  reads every path that does not exist as "a file", which is how a nonexistent
  child of a linked-app folder got a `True` out of a gate whose entire directory
  rule is "the registered folder itself, never a child".
* the pane must resolve the file's template the way the SHELL does — the first
  non-`conditional` entry from stat — rather than from a per-extension table,
  which drifts from the registry the moment a binding changes and ignores a user
  override entirely (§16).

The gate is exec'd standalone here, the way `server._run_condition` execs it —
never imported as part of a package, since a template may not import
fused_render (SPEC PY-15 / D166).
"""
import importlib.util
import json
import os

import pytest


def _gate():
    path = os.path.join("fused_render", "templates", "claude_split", "condition.py")
    spec = importlib.util.spec_from_file_location("test_claude_split_condition", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A workspace root, so the directory branch's <tag>/<project> rule has
    something to measure against."""
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


# ----------------------------------------------------------------- file branch

def test_any_existing_file_is_offered_the_split_chat(tmp_path, workspace):
    """The point of D230: a file nowhere near the workspace still gets the chat
    with the annotation tools. No repository, no app, no project — a file being
    looked at is enough, because the registry already decided which extensions
    offer the mode and the gate has nothing left to add."""
    f = tmp_path / "elsewhere" / "notes.md"
    f.parent.mkdir()
    f.write_text("# hi")
    assert _gate().main(str(f)) is True


def test_a_path_that_does_not_exist_is_refused(tmp_path, workspace):
    """`isfile`, not `not isdir` — the regression this file exists for.

    A gate cannot tell a missing path from a file, and CT-12 says "cannot tell"
    reads as "refuse". The loose form also silently generalised the directory
    rule from "the app folder itself" to "any name under it", because a
    nonexistent child is not a directory either.
    """
    gate = _gate()
    assert gate.main(str(tmp_path / "nope.md")) is False
    assert gate.main(str(tmp_path / "nope" / "deeper" / "nope.md")) is False


def test_an_empty_path_is_refused(workspace):
    assert _gate().main("") is False


# ------------------------------------------------------------ directory branch

def test_a_project_folder_is_offered_and_its_parent_and_children_are_not(
        tmp_path, workspace):
    """The directory rule is unchanged by D230 and must stay that way: exactly
    <workspace>/<tag>/<project>. This is the same rule `app/condition.py` keeps,
    so a folder never offers one of the two app modes without the other."""
    gate = _gate()
    project = workspace / "local" / "demo"
    project.mkdir(parents=True)
    (project / "sub").mkdir()

    assert gate.main(str(project)) is True
    assert gate.main(str(workspace / "local")) is False      # a tag folder
    assert gate.main(str(workspace)) is False                # the root itself
    assert gate.main(str(project / "sub")) is False           # a nested folder
    assert gate.main(str(tmp_path / "unrelated")) is False    # outside entirely


def test_a_hidden_tag_or_project_is_refused(workspace):
    """The apps API skips dot-prefixed tags and projects when listing Home
    cards; `tag/.venv` and `.hidden/project` must not sneak the mode in through
    the gate either."""
    gate = _gate()
    for rel in (("local", ".venv"), (".hidden", "demo")):
        d = workspace.joinpath(*rel)
        d.mkdir(parents=True)
        assert gate.main(str(d)) is False, rel


def test_an_ordinary_folder_gets_the_plain_claude_chat_not_the_split_view(
        tmp_path, workspace):
    """The complement of D230's other half: an ordinary directory's chat is the
    `claude` mode (directory-only now), whose pane has no app entry to render.
    If this gate ever said True here, the explorer would offer two chat modes on
    every folder — the exact confusion the kind split removes."""
    d = tmp_path / "just-a-folder"
    d.mkdir()
    assert _gate().main(str(d)) is False


# ------------------------------------------------------------------- the pane

def _pane_source() -> str:
    with open(os.path.join("fused_render", "templates", "claude_split",
                           "template.html"), encoding="utf-8") as f:
        return f.read()


def test_the_pane_resolves_a_file_through_stat_not_a_per_extension_table():
    """The pane asks stat which view a file gets and takes the first
    non-`conditional` entry — the shell's own rule (Preview.tsx
    `defaultTemplate`). Hardcoding a template per extension would drift from the
    registry on the next rebinding and would ignore a user override (§16)."""
    page = _pane_source()
    assert "/api/fs/stat?path=" in page
    assert "e.conditional" in page
    assert "_file=" in page


def test_the_pane_never_frames_a_chat_mode():
    """`claude_split` framing itself would nest the split view inside its own
    left pane, recursively; `claude` is not a preview of the target either."""
    page = _pane_source()
    assert 'PANE_SKIP_MODES = new Set(["claude", "claude_split"])' in page


def test_the_pane_renders_a_page_target_as_itself():
    """`_render` is a shell sentinel (PT-12), not a template folder: for an
    `.html` target the file IS the document, so a bare /render on the file is
    the only correct src — routing it through a template would frame the source
    view of a page the user expects to see rendered."""
    page = _pane_source()
    assert 'if (t.mode === "_render") return "/render?path=" + encodeURIComponent(FILE);' in page


def test_a_folder_target_still_resolves_its_app_entry():
    """The builder path must be untouched: a project folder's pane is the app's
    entry html via ./app.py, which is what `HomeHero` lands a newly created app
    on (`?_mode=claude_split` over the FOLDER)."""
    page = _pane_source()
    assert 'fused.runPython("./app.py", { dir: FILE })' in page


# ------------------------------------------------------- the binding it needs

def test_the_registry_binds_the_split_view_to_files_and_keeps_the_directory_key():
    """Both halves of D230's binding, in one place. The file keys are what makes
    the annotation tools reachable while editing a standalone file; the `/` key
    is what keeps the mode in the app-builder view (App.tsx APP_MODES), where
    dropping it would have silently removed the chat from every app and broken
    app creation."""
    with open(os.path.join("fused_render", "templates", "registry.json"),
              encoding="utf-8") as f:
        registry = json.load(f)

    assert "claude_split" in registry["/"]
    for key in (".py", ".md", ".html", ".parquet", ".tsx", ".toml", ".ipynb"):
        assert "claude_split" in registry[key], key
    # Chat then history, adjacent, on every FILE key that has them. The `/` key
    # is excluded deliberately: its order is the directory story (`_listing`,
    # then the app modes, then the directory chat and the two history views) and
    # D230 left it exactly as it was.
    for key, names in registry.items():
        if key.endswith("/") or not isinstance(names, list):
            continue
        if "claude_split" in names and "versions" in names:
            assert names.index("claude_split") + 1 == names.index("versions"), key
