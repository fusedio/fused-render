"""Tests for templates/shared/contrib_guard.py — the ONE copy of the
contribution loop's push/pull/reset guard (docs/CONTRIB_LOOP.md §17.8).

The git behaviour is pinned through the Git panel's own ops/reader in
tests/test_git_ops.py; here: the module's contract as a shared template
utility (stdlib only, never `fused_render`, loaded by path by every
consumer, one value of each constant) and its decisions over literal git
answers (an injected `run`, no repository).
"""
import ast
import importlib.util
import json
import os

import pytest

SHARED = os.path.join(os.path.dirname(__file__), "..", "fused_render", "templates", "shared")
PATH = os.path.join(SHARED, "contrib_guard.py")


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("contrib_guard_under_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake(answers):
    """`run` over a dict `{args tuple: stdout}`; a missing key exits 1."""
    def run(_root, *args):
        out = answers.get(args)
        return (0, out.encode()) if out is not None else (1, b"")
    return run


GUARD = ("config", "--get-all", "remote.origin.pushurl")
URL = ("config", "--get", "remote.origin.url")
KIND = ("config", "--local", "--get", "fused.kind")
UNKNOWN = ("config", "--local", "--get", "fused.ownerunknown")
BRANCH = ("symbolic-ref", "--quiet", "--short", "HEAD")
AHEAD = ("rev-list", "--count", "--no-merges", "@{upstream}..HEAD")

COPY = {GUARD: "no-push://fused\n", URL: "git@github.com:alice/x.git\n",
        BRANCH: "fused/mine\n", AHEAD: "0\n"}


def test_stdlib_only_and_never_fused_render():
    """A template subprocess has no `fused_render` (PY-15) and a user venv
    has only the stdlib: every import here is a stdlib module."""
    import sys

    tree = ast.parse(open(PATH, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names and names <= set(sys.stdlib_module_names), names


def test_one_value_per_constant(guard):
    """classify writes the guard the Git panel tests for: same file, same
    value (§17.8 deletes the three copies)."""
    from fused_render.contrib import classify

    assert classify.NO_PUSH_URL == guard.NO_PUSH_URL == "no-push://fused"
    assert guard.HOME_BRANCH == "fused/mine"
    for name in ("ops.py", "log.py"):
        src = open(os.path.join(SHARED, "..", "git", name), encoding="utf-8").read()
        assert 'no-push://fused' not in src and '_load_shared("contrib_guard")' in src, name


@pytest.mark.parametrize("answers,want", [
    ({}, None),
    ({URL: "https://github.com/alice/x\n"}, None),
    (COPY, ("alice/x", False)),
    ({KIND: "github\n", URL: "https://github.com/alice/x.git\n"}, ("alice/x", False)),
    ({GUARD: "no-push://fused\n", URL: "/tmp/somewhere.git\n"},
     ("its author's version on GitHub", False)),
    ({**COPY, UNKNOWN: "true\n"}, ("alice/x", True)),
])
def test_guarded_who(guard, answers, want):
    assert guard.guarded_who("/r", fake(answers)) == want


def test_labels_use_the_login(guard):
    assert guard.bring_in_label("alice/x") == "Bring in alice's latest"
    assert guard.match_label("alice/x") == "Use alice's version"
    assert guard.bring_in_label(guard.UNNAMED) == "Bring in the latest"
    assert guard.match_label(guard.UNNAMED) == "Use the author's version"


@pytest.mark.parametrize("ahead,blocked", [("0\n", False), ("2\n", True), (None, True)])
def test_pull_is_refused_only_with_commits_of_its_own(guard, ahead, blocked):
    answers = {**COPY, AHEAD: ahead}
    got = guard.pull_blocked("/r", fake(answers))
    assert bool(got) is blocked
    if blocked:
        assert "Bring in alice's latest" in got and "terminal" not in got


@pytest.mark.parametrize("branch,blocked", [("fused/mine\n", False), ("main\n", True),
                                            ("other\n", True), (None, True)])
def test_reset_is_refused_off_the_home_branch(guard, tmp_path, branch, blocked):
    got = guard.reset_blocked("/r", fake({**COPY, BRANCH: branch}), home_dir=str(tmp_path))
    assert bool(got) is blocked


def test_nothing_is_refused_on_an_unguarded_repo(guard, tmp_path):
    run = fake({URL: "https://github.com/alice/x\n", BRANCH: "main\n", AHEAD: "3\n"})
    assert guard.push_blocked("/r", run, home_dir=str(tmp_path)) is None
    assert guard.pull_blocked("/r", run) is None
    assert guard.reset_blocked("/r", run, home_dir=str(tmp_path)) is None


def test_app_page_on_reads_the_switch(guard, tmp_path, monkeypatch):
    assert guard.app_page_on(str(tmp_path)) is False  # no prefs.json
    (tmp_path / "prefs.json").write_text("{not json")
    assert guard.app_page_on(str(tmp_path)) is False
    (tmp_path / "prefs.json").write_text(json.dumps({"contrib_enabled": "yes"}))
    assert guard.app_page_on(str(tmp_path)) is False  # opt-in: literal true only
    (tmp_path / "prefs.json").write_text(json.dumps({"contrib_enabled": True}))
    assert guard.app_page_on(str(tmp_path)) is True
    # No home given: appenv's, loaded beside the module (env vars only).
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", str(tmp_path))
    assert guard.app_page_on() is True


def test_push_sentence_follows_the_switch(guard, tmp_path):
    run = fake(COPY)
    assert guard.push_blocked("/r", run, home_dir=str(tmp_path)) == (
        "This copy of alice/x has changes of its own, and they stay on this computer.")
    (tmp_path / "prefs.json").write_text(json.dumps({"contrib_enabled": True}))
    assert guard.push_blocked("/r", run, home_dir=str(tmp_path)) == (
        "This app follows alice/x. Send your changes from its app page.")
