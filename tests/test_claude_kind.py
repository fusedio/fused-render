"""The `claude` gate across the two target KINDS (D235), and the registry
binding it needs.

The chat used to be the app builder's alone: its gate offered it for a project
folder only. D235 bound it to 47 file keys as well, because the annotation /
app_state machinery lives here and nowhere else — chatting about a standalone
file with those tools is the whole point — so the gate now answers for a file
too. Then the plain chat template was deleted and this became the ONLY chat: the
gate's directory branch widened from "an app folder" to "any directory".

Two things are worth pinning down, and each of them broke once:

* the FILE branch of the gate must test `isfile`, not `not isdir`. The loose form
  reads every path that does not exist as "a file", which is how a nonexistent
  child of a registered folder once got a `True` out of a gate.
* the gate must not walk the directory it is asked about. It runs on every stat
  of every row, so a listdir here is a listing that gets slower with its own
  contents.

The left pane the gate's answer used to be read alongside is the native chat's
now (`frontend/src/apps/claude`), and everything about how it RENDERS — the view
picker, the narrow layout, the annotation overlay — is tested there under
vitest, where the DOM is real.

The gate is exec'd standalone here, the way `server._run_condition` execs it —
never imported as part of a package, since a template may not import
fused_render (SPEC PY-15 / D166).
"""
import importlib.util
import json
import os

import pytest


def _gate():
    path = os.path.join("fused_render", "templates", "claude", "condition.py")
    spec = importlib.util.spec_from_file_location("test_claude_condition", path)
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
    """The point of D235: a file nowhere near the workspace still gets the chat
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

def test_every_directory_is_offered_the_chat(tmp_path, workspace):
    """The directory rule is now "any directory". It used to be exactly
    <workspace>/<tag>/<project> — a narrowing that
    existed only because an ordinary folder's chat was the separate `claude` mode,
    whose pane had no app entry to render. `claude` is deleted, so narrowing here
    would leave an ordinary folder with no chat at all — the capability the delete
    was meant to preserve. D239 has since conceded the pane half of the old
    reasoning (such a folder really does have nothing to frame, and gets no pane)
    without conceding the gate: a folder worth talking to an agent about does not
    become less worth it because there is nothing to render beside the
    conversation."""
    gate = _gate()
    project = workspace / "local" / "demo"
    project.mkdir(parents=True)
    (project / "sub").mkdir()
    hidden = workspace / ".hidden" / "demo"
    hidden.mkdir(parents=True)
    ordinary = tmp_path / "just-a-folder"
    ordinary.mkdir()

    for d in (project, workspace / "local", workspace, project / "sub",
              hidden, ordinary):
        assert gate.main(str(d)) is True, d


def test_a_directory_that_does_not_exist_is_refused(tmp_path):
    """`isdir`, not `not isfile`: "cannot tell" has to read as "refuse" (CT-12),
    or a stat of a path that vanished between listing and gate offers a chat
    about nothing."""
    assert _gate().main(str(tmp_path / "gone")) is False


def test_a_mount_backed_path_is_still_refused(tmp_path, monkeypatch):
    """The ONE thing the gate still answers, and the reason it was not deleted
    outright once the directory branch widened: bytes under the mounts dir come
    from a remote over FUSE, and an agent turned loose there walks and rewrites
    the tree through the mount. Both kinds are refused — a file and a directory
    alike — because the objection is about the transport, not the target."""
    mounts = tmp_path / "home" / "mounts"
    (mounts / "pub").mkdir(parents=True)
    f = mounts / "pub" / "page.html"
    f.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    gate = _gate()
    assert gate.main(str(f)) is False
    assert gate.main(str(mounts / "pub")) is False


def test_the_gate_never_walks_the_directory():
    """It runs for every directory the explorer stats, some of them on remote
    mounts, so listing one would turn a stat into a directory read. Pinned as
    source because the cost is invisible in behaviour."""
    src = open(os.path.join("fused_render", "templates", "claude",
                            "condition.py"), encoding="utf-8").read()
    body = src[src.index("def main("):]
    for banned in ("os.listdir", "os.scandir", "glob", "os.walk", "realpath"):
        assert banned not in body, banned


# ------------------------------------------------------- the binding it needs

def test_the_registry_binds_the_split_view_to_files_and_keeps_the_directory_key():
    """Both halves of D235's binding, in one place. The file keys are what makes
    the annotation tools reachable while editing a standalone file; the `/` key
    is what keeps the mode in the app-builder view (App.tsx APP_MODES), where
    dropping it would have silently removed the chat from every app and broken
    app creation."""
    with open(os.path.join("fused_render", "templates", "registry.json"),
              encoding="utf-8") as f:
        registry = json.load(f)

    assert "claude" in registry["/"]
    for key in (".py", ".md", ".html", ".parquet", ".tsx", ".toml", ".ipynb"):
        assert "claude" in registry[key], key
    # It is also the ONLY chat on the `/` key: the second chat mode that used to
    # sit beside it there is deleted, and a second entry labelled "Chat" on the
    # one target kind where the two differed was the whole reason the surviving
    # mode needed a display name of its own.
    assert registry["/"].count("claude") == 1

    # The chat sits after the CONTENT views on every file key that has it: it is
    # a companion to reading the bytes, never the way you read them. The `/` key
    # is excluded deliberately — its order is the directory story (`_listing`
    # first, then the gated peers).
    for key, names in registry.items():
        if key.endswith("/") or not isinstance(names, list):
            continue
        if "claude" in names:
            assert names.index("claude") > 0, key


def test_the_gates_docstring_does_not_justify_itself_with_the_deleted_pane():
    """The gate's directory branch is wide for ONE reason — the plain chat mode is
    deleted (D237), so narrowing would leave an ordinary folder with no chat at
    all. Its docstring used to give a second reason: that the pane "falls back to
    /embed/<dir> — fused-render's own file browser", citing `paneURL` by name.
    D239 deleted that pane, so the stated justification rested on a branch that now
    returns `null`.

    Pinned because a gate is the first thing read when asking "why is this mode
    offered here", and a reason that points at deleted code is worse than no
    reason: it sends the reader to `paneURL` to find the opposite of what they were
    told. Also pinned: the gate does not open by calling this template "the split
    view", which is true of two of its three target shapes and is not what the
    gate decides anyway."""
    src = open(os.path.join("fused_render", "templates", "claude",
                            "condition.py"), encoding="utf-8").read()
    doc = src[:src.index('"""', 3) + 3]
    assert "/embed" in doc, "the history is kept — it is the citation that goes"
    assert "returns `null`" in doc or "It is gone" in doc, \
        "the embed must be named as REMOVED, not cited as live"
    assert "see paneURL in" not in doc, \
        "the pane is cited as gone, never as a live place to go read"
    assert not doc.lstrip('"').lstrip().startswith("Gate for the `claude` template"
                                                   " — the split view")
    # The delete, not D235, is what widened the branch.
    assert "D237 deleted the second" in doc
