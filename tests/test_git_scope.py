"""What the `git` view is FOR, and where it is offered.

Two changes are pinned here, and they are the same change seen from two sides.

**`git` is commit management, not history.** Staging, discarding, stashing,
committing, branches, push/pull — the things you do to a working tree. The
commit LOG is `versions`' story: it renders the same commits with a timeline
the git view never had, and two views of one story is what the peer exclusions
in the two `condition.py` gates used to spend their complexity avoiding. So the
git view no longer draws a History section, no longer selects a commit, and no
longer asks the reader for the log at all.

**Both gates are loose.** `git` was bound to the universal `/` directory key
alone, and its gate additionally refused a fused app folder (that was
`versions`' territory) — while the explorer, since #424, gives a FOLDER no mode
switcher of its own: the only mode surface a browsing user has is the preview
pane's, which acts on the SELECTED ROW. A selected row is usually a file, and
`git` was bound to no file extension, so the view was effectively unreachable
except by typing `?_mode=git`. Now both modes are offered on anything inside a
work tree — every file key that carries `versions` carries `git` beside it, and
neither gate excludes the other's targets.
"""
import importlib.util
import json
import os
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "fused_render", "templates")


def _load(name):
    path = os.path.join(_TEMPLATES, name, "condition.py")
    spec = importlib.util.spec_from_file_location(f"_cond_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def registry():
    with open(os.path.join(_TEMPLATES, "registry.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def source():
    with open(os.path.join(_TEMPLATES, "git", "template.html"),
              encoding="utf-8") as f:
        return f.read()


@pytest.fixture()
def work_tree(tmp_path, monkeypatch):
    """A real repository with a nested folder and a tracked file in it."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@e",
         "commit", "-qm", "init"], check=True, env=env)
    # A workspace elsewhere, so the app-dir rules are not accidentally in play.
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.delenv("FUSED_RENDER_LINKED_APPS", raising=False)
    return root


# ------------------------------------------------------------------ bindings


def test_git_rides_with_versions_on_every_key(registry):
    # The two are the working-tree view and the history view of the same repo,
    # so they are offered together. This is what makes `git` reachable at all
    # from the explorer, whose only mode surface is the preview pane's — and
    # the pane acts on the selected ROW, which is usually a file.
    missing = [
        key for key, value in registry.items()
        if isinstance(value, list) and "versions" in value and "git" not in value
    ]
    assert missing == []


def test_git_is_never_offered_without_versions(registry):
    # The converse, so the pair cannot drift apart from the other end.
    orphans = [
        key for key, value in registry.items()
        if isinstance(value, list) and "git" in value and "versions" not in value
    ]
    assert orphans == []


def test_git_is_still_never_a_default(registry):
    # A gated mode cannot be the default anyway (PT-8); the order is deliberate
    # too — the content view leads every list.
    leads = [key for key, value in registry.items()
             if isinstance(value, list) and value and value[0] == "git"]
    assert leads == []


# --------------------------------------------------------------- loose gates


def test_both_gates_take_a_file_inside_any_work_tree(work_tree):
    target = str(work_tree / "pkg" / "mod.py")
    assert _load("git").main(target) is True
    assert _load("versions").main(target) is True


def test_the_two_folder_gates_ask_different_questions(work_tree):
    # This is where the pair stops being symmetric, and deliberately so.
    #
    # `git` is the WORKING TREE of the repository a folder sits in, which every
    # folder in a work tree has — so it takes them all.
    #
    # `versions` previews the target AS IT WAS, so it also asks whether there is
    # anything to preview: a folder qualifies when it has a top-level page, by
    # the shared entry rule. Without that half it put a history mode in the
    # switcher of every directory of every repository the user opens, whose
    # preview is a listing of a frozen tree.
    #
    # The pair is still bound together in the registry (the tests above): what
    # differs is which targets each GATE answers for, which is the mechanism
    # that exists for exactly this.
    for target in (str(work_tree), str(work_tree / "pkg")):
        assert _load("git").main(target) is True, target
        assert _load("versions").main(target) is False, target

    # ...and the moment such a folder has a page, both take it.
    (work_tree / "pkg" / "page.html").write_text("<html></html>")
    assert _load("git").main(str(work_tree / "pkg")) is True
    assert _load("versions").main(str(work_tree / "pkg")) is True


def test_neither_gate_excludes_the_other_on_an_app_folder(tmp_path, monkeypatch):
    # The old exclusions were symmetric refusals — `git` stepped aside inside a
    # fused app, `versions` stepped aside outside one — held in place by a
    # comment in each file telling the reader to keep the two in step. Both are
    # gone: the modes answer different questions, so a folder gets both.
    workspace = tmp_path / "workspace"
    app = workspace / "tag" / "myapp"
    app.mkdir(parents=True)
    (app / "index.html").write_text("<p>hi</p>")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["git", "init", "-q", str(app)], check=True, env=env)
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE", str(workspace))
    monkeypatch.delenv("FUSED_RENDER_LINKED_APPS", raising=False)

    assert _load("git").main(str(app)) is True
    assert _load("versions").main(str(app)) is True


def test_a_path_outside_any_repository_is_refused_by_both(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.delenv("FUSED_RENDER_LINKED_APPS", raising=False)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("hi")
    assert _load("git").main(str(plain)) is False
    assert _load("versions").main(str(plain)) is False


# ------------------------------------------------- the view drops the history


def test_the_view_draws_no_history_section(source):
    assert "commitLine" not in source
    assert 'listSection("History"' not in source
    assert "more commits" not in source


def test_the_view_does_not_select_a_commit(source):
    # `sel` was the selected-commit param; `pages` grew the log window. Both are
    # history state and both are gone, so the URL grammar documented in the
    # header must not advertise them either.
    assert 'param("sel")' not in source
    assert 'param("pages")' not in source
    assert "sel=<sha>" not in source


def test_the_view_never_asks_the_reader_for_the_log(source):
    # Not merely "does not render it": the overview call opts out, so opening
    # the working-tree view does not run `git log` at all.
    assert "history: " in source, "the overview call must opt out of the log"
    # No commit DIFF read either — that was the selected-commit pane. (The
    # `commit` op on ops.py stays: MAKING a commit is what this view is for,
    # which is why this looks for the reader's channel rather than the word.)
    assert 'chan("commit")' not in source
    assert "READER" in source and 'op: "log"' not in source
