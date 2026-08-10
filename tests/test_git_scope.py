"""What the `git` view is FOR, and where it is offered.

Two changes are pinned here, and they are the same change seen from two sides.

**`git` is commit management, not history.** Staging, discarding, stashing,
committing, branches, push/pull — the things you do to a working tree. The
commit LOG is `history`'s story: it renders the same commits with a timeline
the git view never had, and two views of one story is what the peer exclusions
in the two `condition.py` gates used to spend their complexity avoiding. So the
git view no longer draws a History section, no longer selects a commit, and no
longer asks the reader for the log at all.

**`git` is FOLDER-ONLY.** Staging, discarding, stashing, committing, branches,
push/pull are all repository-level acts — you do not stash a file, you stash a
tree — and the working tree a file sits in is its FOLDER's. So `git` is offered
on the universal `/` directory key and on no file extension at all, and its gate
refuses anything that is not a directory. Per-file history is a different
question with an answer that already ships: `history`, unchanged, still on every
file key it had.

`git` did once ride along on file keys, and the reason is gone. The explorer used
to give a FOLDER no mode switcher of its own — the only mode surface a browsing
user had was the preview pane's, which acted on the SELECTED ROW, always a file —
so a mode bound to `/` alone was unreachable without typing `?_mode=git`, and
riding the file keys was the workaround. The preview pane now selects and
previews FOLDER rows too (the folder peek), so a folder's mode switcher is
reachable and the workaround is retired.
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


def test_git_is_offered_on_the_directory_key_alone(registry):
    # The working tree is a property of the FOLDER, so the folder is where the
    # mode lives: the universal "/" directory key, and nothing else. It used to
    # ride along on every key `history` was on, because a folder had no mode
    # surface to reach it from; the preview pane peeks folders now, so it does
    # not need the ride.
    offered = [key for key, value in registry.items()
               if isinstance(value, list) and "git" in value]
    assert offered == ["/"]


def test_no_file_extension_offers_git(registry):
    # The same rule from the other end, stated over the file keys, so a new
    # extension cannot quietly reintroduce a per-file working-tree view.
    file_keys = [key for key, value in registry.items()
                 if isinstance(value, list) and key != "/" and "git" in value]
    assert file_keys == []


def test_history_is_untouched_on_every_file_key(registry):
    # De-linking is a change to `git` ONLY. Per-file history is `history`'s job
    # and it keeps every key it had — which, before the split, was exactly the
    # set `git` rode along on. Pinned literally: a silent drop here would look
    # like "the split took history with it".
    assert sorted(key for key, value in registry.items()
                  if isinstance(value, list) and "history" in value
                  and key != "/") == [
        ".cfg", ".cjs", ".conf", ".csh", ".css", ".csv", ".cts", ".fish",
        ".geojson", ".hcl", ".htm", ".html", ".ini", ".ipynb", ".jpeg", ".jpg",
        ".js", ".json", ".jsonl", ".jsx", ".latex", ".log", ".ltx", ".markdown",
        ".md", ".mjs", ".mts", ".ndjson", ".parquet", ".plist", ".png", ".ps1",
        ".py", ".sh", ".svg", ".tex", ".tf", ".toml", ".ts", ".tsv", ".tsx",
        ".txt", ".vim", ".yaml", ".yml", ".zsh", ".zsh-theme",
    ]
    # And on the folder key too, where it sits beside `git`.
    assert "history" in registry["/"]


def test_git_is_still_never_a_default(registry):
    # A gated mode cannot be the default anyway (PT-8); the order is deliberate
    # too — the content view leads every list.
    leads = [key for key, value in registry.items()
             if isinstance(value, list) and value and value[0] == "git"]
    assert leads == []


# ------------------------------------------------------ folder-only vs. a file


def test_a_file_in_a_work_tree_is_git_s_no_and_history_yes(work_tree):
    # The whole change in one line. The file is inside a real work tree, so the
    # old gate said True; the working tree it is inside belongs to its FOLDER,
    # so the folder-only gate says False. The question a file DOES have an
    # answer for — what happened to this file — is `history`'s, and `history`
    # still says yes.
    target = str(work_tree / "pkg" / "mod.py")
    assert _load("git").main(target) is False
    assert _load("history").main(target) is True


def test_git_refuses_a_file_even_at_the_repository_root(work_tree):
    # Not a nesting rule: a file directly in the root of the work tree is still
    # a file, so it is still refused.
    (work_tree / "README.md").write_text("hi\n")
    assert _load("git").main(str(work_tree / "README.md")) is False
    # ...while the folder that holds it is exactly what the mode is for.
    assert _load("git").main(str(work_tree)) is True


def test_the_two_folder_gates_ask_different_questions(work_tree):
    # This is where the pair stops being symmetric, and deliberately so.
    #
    # `git` is the WORKING TREE of the repository a folder sits in, which every
    # folder in a work tree has — so it takes them all.
    #
    # `history` previews the target AS IT WAS, so it also asks whether there is
    # anything to preview: a folder qualifies when it has a top-level page, by
    # the shared entry rule. Without that half it put a history mode in the
    # switcher of every directory of every repository the user opens, whose
    # preview is a listing of a frozen tree.
    #
    # On the folder key the two are still offered side by side (the tests
    # above): what differs is which targets each GATE answers for, which is the
    # mechanism that exists for exactly this.
    for target in (str(work_tree), str(work_tree / "pkg")):
        assert _load("git").main(target) is True, target
        assert _load("history").main(target) is False, target

    # ...and the moment such a folder has a page, both take it.
    (work_tree / "pkg" / "page.html").write_text("<html></html>")
    assert _load("git").main(str(work_tree / "pkg")) is True
    assert _load("history").main(str(work_tree / "pkg")) is True


def test_neither_gate_excludes_the_other_on_an_app_folder(tmp_path, monkeypatch):
    # The old exclusions were symmetric refusals — `git` stepped aside inside a
    # fused app, `history` stepped aside outside one — held in place by a
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
    assert _load("history").main(str(app)) is True


def test_a_path_outside_any_repository_is_refused_by_both(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.delenv("FUSED_RENDER_LINKED_APPS", raising=False)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("hi")
    assert _load("git").main(str(plain)) is False
    assert _load("history").main(str(plain)) is False


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
