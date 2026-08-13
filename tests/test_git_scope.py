"""What the `git` view is FOR, and where it is offered.

Two things are pinned here, and they are the same view seen from two sides.

**`git` is commit management, and that INCLUDES the commits.** Staging,
discarding, stashing, committing, branches, push/pull — the things you do to a
working tree — plus the log of what those acts produced, scoped to the same path
the working-tree lists are scoped to. Overlapping with `history` is not the same
as duplicating it: `history` is a repo-wide timeline you go to in order to read
the past, this is the "what just happened under this path" that belongs beside
the tree you are about to change. The section is fed by the overview read's own
`commits`/`has_more`/`capped` fields, so it costs no extra round trip, and
selecting a row fills the SAME diff pane a working-tree row fills.

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
    # Asserted rather than assumed: `spec_from_file_location` returns None for a
    # path with no loader, and `module_from_spec(None)` fails several lines later
    # with an error about NoneType instead of about the file that is missing.
    assert spec is not None and spec.loader is not None, f"no condition.py for {name}"
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


# ------------------------------------------------------ the view draws commits


def test_the_view_draws_a_commits_section(source):
    assert "commitLine" in source
    assert 'listSection(\n    "Commits"' in source or 'listSection("Commits"' in source
    # Built with the section helper every other list uses, so Commits sits in the
    # same column, in the same vocabulary, as Staged/Changes/Untracked/Stashes.
    assert "more commits" in source


def test_the_commits_heading_carries_no_count(source):
    # The other sections' numbers are TOTALS. This list is one window of a
    # history nobody has measured (`limit = PAGE_SIZE * pages`), so a number here
    # would be the window size wearing a total's clothes — and it would grow by
    # PAGE_SIZE on every "load more". `null` is what makes `listSection` omit the
    # pill; `String(commits.length)` is the number that must not come back.
    assert '"Commits", null, null,' in source
    assert '"Commits", String(commits.length)' not in source
    # The real totals above it are untouched.
    for section in ("Staged changes", "Changes", "Untracked", "Stashes"):
        assert f'"{section}", String(' in source


def test_a_commit_row_is_a_subject_and_a_time(source):
    # The row is what you SKIM: the subject, and how long ago. Drawing the sha
    # and the author on it too turned the list into a table — an id to cross
    # before the sentence, and a column repeating the same name on every row.
    row = source[source.index("function commitLine("):]
    row = row[:row.index("\nfunction ")]
    assert 'className: "subject"' in row
    assert 'className: "when"' in row
    assert 'className: "sha"' not in row, "the id belongs to the selected state"
    assert 'className: "who"' not in row, "the author belongs to the selected state"
    # Not lost, though: the row's tooltip still answers "which commit is this?"
    assert "entry.short" in row and "entry.author" in row


def test_the_identity_moves_into_the_selected_state(source):
    # …and it is a node the CALLER builds, passed into the shared pane, so what
    # a commit selection shows can change without touching how a diff renders.
    assert "function commitMeta(" in source
    assert "function diffPane(title, sub, payload, meta)" in source
    assert "commitMeta(meta, rev)" in source
    # The placeholder pane is built from the row the user clicked, so the
    # heading does not swap under them when the read lands.
    assert "commitMeta(known, rev)" in source


def test_the_commit_selection_is_an_addressable_param(source):
    # `rev` is the view's public handle on "which commit is selected" — the one
    # piece of its state another layer (the explorer sidebar) is meant to read
    # and drive — so it lives in the URL and is documented in the header grammar.
    assert 'param("rev")' in source
    assert "rev=<sha>" in source
    # And it is the FULL object name, never the abbreviation the row displays.
    assert "rev: selected ? null : entry.sha" in source


def test_the_pane_has_exactly_one_master(source):
    # `rev` and `wt` both mean "what the right pane shows", so every writer of
    # one clears the other. Missing either half leaves two selections claiming
    # one pane, and the pane then answers to whichever the loader tested first.
    assert "setParams({ wt: change.path, rev: null })" in source
    assert "rev: selected ? null : entry.sha, wt: null" in source
    assert "setParams({ wt: null, rev: null })" in source
    assert "setParams({ rev: null, wt: null })" in source


def test_the_view_asks_the_reader_for_the_log(source):
    # The overview carries the log again, so it must NOT opt out — `history: ""`
    # would leave the Commits section permanently empty while the section
    # itself still rendered its "no commits under this path" reading.
    assert 'history: ""' not in source
    # The window grows rather than pages: `limit` scales with `pages`, `page`
    # stays 0, and both `has_more` and `capped` gate the "load more" affordance.
    assert "limit: String(PAGE_SIZE * pages)" in source
    assert 'page: "0"' in source
    assert "data.has_more && !data.capped" in source
    # The selected-commit pane is a second read on its OWN channel: runPython
    # supersedes by channel and a superseded call never settles, so sharing the
    # worktree channel would deadlock the two reads against each other.
    assert 'chan("commit")' in source
    assert 'op: "commit", sha: rev' in source
