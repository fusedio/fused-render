"""Conflict support in the `git` template: the `conflicts` READ and the
`resolve` WRITE (the two halves behind the view's "Resolve with AI" button).

Driven against a REAL conflicted repository, like the rest of this template's
suite: the modules' whole job is to ask git and parse what git says, so a mocked
subprocess would test our fiction of the porcelain format instead of the format.

What is pinned here:

* `log.py op="conflicts"` reports the unmerged paths WITH their conflict markers
  (that text is the model's only real context), names the operation in flight,
  and says `empty` — a first-class answer, not an error — when there is no
  conflict.
* Content is CAPPED per file and in file count, and the truncation is reported,
  so a prompt built from it cannot be unbounded.
* Binary conflicts are named and NOT read.
* `ops.py op="resolve"` writes one resolved file and does **nothing else**: it
  never stages and never commits, so the user reviews the working tree before
  anything enters the index. Asserted by checking the index directly.
* Every way `resolve` must refuse: a path that is not conflicted, a path outside
  the view's scope, empty content, and content that still carries conflict
  markers (a "resolution" that resolved nothing).
* `resolve` is DESTRUCTIVE — it overwrites a file in the working tree — so it
  must be in `DESTRUCTIVE_OPS`, which is what gives it the view's confirmation.
"""
import importlib.util
import os

import pytest

from _git_repo import git, git_available

_TPL = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "git")

pytestmark = pytest.mark.skipif(not git_available(), reason="git binary not installed")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def reader():
    return _load("git_log_conflicts", os.path.join(_TPL, "log.py"))


@pytest.fixture(scope="module")
def ops():
    return _load("git_ops_conflicts", os.path.join(_TPL, "ops.py"))


def _put(root, rel, payload):
    full = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    mode = "wb" if isinstance(payload, bytes) else "w"
    with open(full, mode) as fh:
        fh.write(payload)


def conflicted_repo(root, path="pkg/mod.py", binary=False):
    """A repo mid-merge with exactly one unmerged path."""
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q", root)
    _put(root, path, b"\x00base\x00" if binary else "one\ntwo\nthree\n")
    _put(root, "README.md", "readme\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "branch", "other")

    _put(root, path, b"\x00ours\x00\x01" if binary else "one\nOURS\nthree\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "ours")

    git(root, "checkout", "-q", "other")
    _put(root, path, b"\x00theirs\x00\x02" if binary else "one\nTHEIRS\nthree\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "theirs")

    git(root, "checkout", "-q", "-")
    git(root, "merge", "other", check=False)     # conflicts on purpose
    return root


def unmerged(root):
    return [n for n in git(
        root, "diff", "--name-only", "--diff-filter=U").splitlines() if n]


def index_stages(root, path):
    """How many unmerged stages the INDEX still holds for `path`.

    This, not `git diff --cached`, is the honest "was it staged" question for a
    conflicted file: an unmerged path always differs from HEAD, so it shows up in
    `--cached` whether or not anyone added it. `git add` is what COLLAPSES the
    three stages to one, so a non-zero count here proves the index was untouched.
    """
    return len([n for n in git(root, "ls-files", "-u", "--", path).splitlines() if n])


# ------------------------------------------------------------------- the read


def test_conflicts_reports_the_unmerged_file_with_its_markers(reader, tmp_path):
    root = conflicted_repo(str(tmp_path / "repo"))
    payload = reader.main(file=root, op="conflicts")
    assert payload["ok"] is True
    assert payload["empty"] is False
    assert payload["operation"] == "merge"
    paths = [f["path"] for f in payload["files"]]
    assert paths == ["pkg/mod.py"]
    body = payload["files"][0]["content"]
    # The markers ARE the context — a payload without them is useless to a model.
    assert "<<<<<<<" in body and "=======" in body and ">>>>>>>" in body
    assert "OURS" in body and "THEIRS" in body


def test_conflicts_is_empty_on_a_clean_repo(reader, tmp_path):
    root = str(tmp_path / "clean")
    os.makedirs(root)
    git(root, "init", "-q", root)
    _put(root, "a.txt", "a\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "only")
    payload = reader.main(file=root, op="conflicts")
    assert payload["ok"] is True          # not an error — an answer (GT-9)
    assert payload["empty"] is True
    assert payload["files"] == []
    assert payload["operation"] is None


def test_conflicts_marks_what_is_outside_the_open_scope(reader, tmp_path):
    root = conflicted_repo(str(tmp_path / "scoped"))
    payload = reader.main(file=os.path.join(root, "README.md"), op="conflicts")
    # A conflict is repository-wide, so it is still LISTED from a scoped view —
    # but the view may not write outside its scope and has to know that.
    assert [f["path"] for f in payload["files"]] == ["pkg/mod.py"]
    assert payload["files"][0]["in_scope"] is False


def test_conflicts_caps_file_content_and_reports_it(reader, tmp_path, monkeypatch):
    root = conflicted_repo(str(tmp_path / "big"))
    monkeypatch.setattr(reader, "MAX_CONFLICT_BYTES", 40)
    payload = reader.main(file=root, op="conflicts")
    entry = payload["files"][0]
    assert len(entry["content"].encode("utf-8")) <= 40
    assert entry["truncated"] is True


def test_conflicts_caps_the_file_count_and_reports_it(reader, tmp_path, monkeypatch):
    root = conflicted_repo(str(tmp_path / "many"))
    monkeypatch.setattr(reader, "MAX_CONFLICT_FILES", 0)
    payload = reader.main(file=root, op="conflicts")
    assert payload["files"] == []
    assert payload["files_truncated"] is True
    assert payload["empty"] is False      # there IS a conflict; we just showed none


def test_conflicts_names_a_binary_conflict_without_reading_it(reader, tmp_path):
    root = conflicted_repo(str(tmp_path / "bin"), binary=True)
    entry = reader.main(file=root, op="conflicts")["files"][0]
    assert entry["binary"] is True
    assert entry["content"] == ""


def test_conflicts_refuses_a_non_repository(reader, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    payload = reader.main(file=str(plain), op="conflicts")
    assert payload["ok"] is False
    assert payload["reason"] == "not-a-repo"


# ------------------------------------------------------------------ the write


def test_resolve_is_declared_destructive(ops):
    # It overwrites a file in the working tree; the view keys its confirmation
    # step off this tuple, so omitting it ships the op without one.
    assert "resolve" in ops.DESTRUCTIVE_OPS


def test_resolve_writes_the_file_and_stages_nothing(ops, tmp_path):
    root = conflicted_repo(str(tmp_path / "apply"))
    payload = ops.main(file=root, op="resolve", paths=["pkg/mod.py"],
                       content="one\nMERGED\nthree\n")
    assert payload["ok"] is True, payload
    with open(os.path.join(root, "pkg/mod.py")) as fh:
        assert fh.read() == "one\nMERGED\nthree\n"
    # The point of the whole feature: the user reviews the working tree. Nothing
    # was added to the index and nothing was committed.
    assert unmerged(root) == ["pkg/mod.py"]
    assert index_stages(root, "pkg/mod.py") == 3            # never `git add`ed
    assert git(root, "log", "--oneline").count("\n") == 2   # base + ours only


def test_resolve_refuses_a_path_that_is_not_conflicted(ops, tmp_path):
    root = conflicted_repo(str(tmp_path / "notconf"))
    payload = ops.main(file=root, op="resolve", paths=["README.md"],
                       content="rewritten\n")
    assert payload["ok"] is False
    assert payload["reason"] == "not-conflicted"
    with open(os.path.join(root, "README.md")) as fh:
        assert fh.read() == "readme\n"          # untouched


def test_resolve_refuses_outside_the_open_scope(ops, tmp_path):
    root = conflicted_repo(str(tmp_path / "outscope"))
    os.makedirs(os.path.join(root, "docs"), exist_ok=True)
    _put(root, "docs/x.md", "x\n")
    payload = ops.main(file=os.path.join(root, "docs"), op="resolve",
                       paths=["pkg/mod.py"], content="one\nMERGED\nthree\n")
    assert payload["ok"] is False
    assert payload["reason"] == "outside-scope"


def test_resolve_refuses_empty_content(ops, tmp_path):
    root = conflicted_repo(str(tmp_path / "empty"))
    payload = ops.main(file=root, op="resolve", paths=["pkg/mod.py"], content="")
    assert payload["ok"] is False
    assert payload["reason"] == "empty-content"


def test_resolve_refuses_content_that_still_has_conflict_markers(ops, tmp_path):
    root = conflicted_repo(str(tmp_path / "markers"))
    payload = ops.main(
        file=root, op="resolve", paths=["pkg/mod.py"],
        content="one\n<<<<<<< HEAD\nOURS\n=======\nTHEIRS\n>>>>>>> other\nthree\n")
    assert payload["ok"] is False
    assert payload["reason"] == "unresolved"


def test_resolve_refuses_more_than_one_path(ops, tmp_path):
    root = conflicted_repo(str(tmp_path / "multi"))
    payload = ops.main(file=root, op="resolve",
                       paths=["pkg/mod.py", "README.md"], content="x\n")
    assert payload["ok"] is False
    assert payload["reason"] == "one-path"


def test_resolve_refuses_an_unknown_op_shape(ops, tmp_path):
    root = conflicted_repo(str(tmp_path / "noop"))
    payload = ops.main(file=root, op="resolve", paths=[], content="x\n")
    assert payload["ok"] is False
    assert payload["reason"] == "missing"
