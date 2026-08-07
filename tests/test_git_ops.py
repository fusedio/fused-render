"""The `git` template's write ops (`fused_render/templates/git/ops.py`, SPEC §33).

Driven against REAL throwaway repositories (tests/_git_repo.py) and a REAL bare
remote, never a mocked `subprocess` — same reason as the reader's suite: the
module's whole job is to hand git the right bounded argv and turn the answer into
JSON, so a fake git would test our fiction of git instead of git.

What this file is really for is the security posture. A mutating module reached
from a URL is a different animal from a reader, and every one of these is a way
it could go wrong:

* **Scope.** Stage / unstage / discard / stash are restricted to the opened path
  (GT-13). A path outside it is refused even when it is a perfectly ordinary file
  inside the repository.
* **Containment.** An absolute path, a `..` segment and a symlink resolving out
  of the work tree are each refused BEFORE the path can become an argv entry.
* **`discard` never touches ignored files** — `git clean` is run without `-x`,
  which is the difference between "throw away the edit I just made" and "throw
  away my `.env` and my virtualenv".
* **Honesty.** An empty commit message and a nothing-staged commit are this
  module's own refusals with readable text, not raw git errors; a
  non-fast-forward pull is a refusal that points at a terminal rather than an
  automatic merge.
* **No history rewriting.** `-D`, `--force`, `--amend`, `reset --hard` and
  `rebase` never appear in an argv this module builds.
* **The mount refusal is the module's** (MD-11 / GT-4), for the write path too: a
  hand-written `?_mode=git` URL must never reach a MUTATING git call across an
  rclone/NFS mount.
"""
import importlib.util
import os
import subprocess

import pytest

from _git_repo import git, git_available, with_remote, write

OPS = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "git", "ops.py")
READER = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "git", "log.py")

pytestmark = pytest.mark.skipif(not git_available(), reason="git binary not installed")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ops():
    return _load(OPS, "git_ops_module")


@pytest.fixture(scope="module")
def reader():
    return _load(READER, "git_log_reader_for_ops")


def _seed(root):
    """A repo with an identity of its OWN, one commit, and a `pkg/` subtree.

    The identity is written into `.git/config` rather than passed per command:
    `ops.py` deliberately does NOT inject one (it is the user's repository, and
    inventing an author for their commits would be a lie), so the fixture has to
    supply what a real user's machine would.
    """
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture Author")
    git(root, "config", "user.email", "fixture@example.com")
    git(root, "config", "commit.gpgsign", "false")
    write(root, "pkg/mod.py", "one\n")
    write(root, "pkg/keep.py", "keep\n")
    write(root, "top.txt", "top\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed", when="2026-10-01T10:00:00+00:00")
    return root


@pytest.fixture()
def repo(tmp_path):
    return _seed(str(tmp_path / "ops-repo"))


def status(root):
    """`{path: XY}` from git itself — the ops' effect is asserted against git."""
    out = git(root, "status", "--porcelain")
    return {line[3:]: line[:2] for line in out.split("\n") if line.strip()}


# ------------------------------------------------------------ stage / unstage


def test_stage_then_unstage_round_trips_a_tracked_file(ops, repo):
    write(repo, "pkg/mod.py", "two\n")
    assert status(repo)["pkg/mod.py"] == " M"

    staged = ops.main(os.path.join(repo, "pkg"), op="stage", paths=["pkg/mod.py"])
    assert staged["ok"] is True and staged["op"] == "stage"
    assert staged["detail"]
    assert status(repo)["pkg/mod.py"] == "M "

    back = ops.main(os.path.join(repo, "pkg"), op="unstage", paths=["pkg/mod.py"])
    assert back["ok"] is True
    assert status(repo)["pkg/mod.py"] == " M"


def test_stage_covers_an_untracked_file(ops, repo):
    # `git add` is deliberately the one verb for both: the UI's "+" means "make
    # this part of the next commit", and a user does not think of a new file as a
    # different operation from an edited one.
    write(repo, "pkg/fresh.txt", "new\n")
    assert ops.main(os.path.join(repo, "pkg"), op="stage",
                    paths=["pkg/fresh.txt"])["ok"] is True
    assert status(repo)["pkg/fresh.txt"] == "A "


def test_stage_covers_a_deletion(ops, repo):
    os.remove(os.path.join(repo, "pkg", "keep.py"))
    assert ops.main(os.path.join(repo, "pkg"), op="stage",
                    paths=["pkg/keep.py"])["ok"] is True
    assert status(repo)["pkg/keep.py"] == "D "


def test_unstage_works_before_the_first_commit(ops, tmp_path):
    # The unborn-HEAD case: `git restore --staged` and `git reset -- <path>` both
    # need a HEAD to restore FROM, and there is none, so they exit 128. Removing
    # the entry from the index is what "unstage" means when the index has no
    # baseline to fall back to.
    root = str(tmp_path / "unborn")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "draft.md", "hello\n")
    git(root, "add", "-A")
    assert status(root)["draft.md"] == "A "

    got = ops.main(root, op="unstage", paths=["draft.md"])
    assert got["ok"] is True, got
    assert status(root)["draft.md"] == "??"


def test_stage_all_and_unstage_all_use_the_scope(ops, repo):
    write(repo, "pkg/mod.py", "two\n")
    write(repo, "top.txt", "changed\n")

    assert ops.main(os.path.join(repo, "pkg"), op="stage_all")["ok"] is True
    after = status(repo)
    assert after["pkg/mod.py"] == "M ", "in scope: staged"
    assert after["top.txt"] == " M", "out of scope: untouched"

    assert ops.main(os.path.join(repo, "pkg"), op="unstage_all")["ok"] is True
    assert status(repo)["pkg/mod.py"] == " M"


def test_unstage_all_before_the_first_commit(ops, tmp_path):
    # The unborn-HEAD path meets the `:/` scope pathspec here, and `git rm
    # --cached` treats a pathspec matching nothing as exit 128 — so this is the
    # one combination where the two correct-in-isolation pieces fail together.
    root = str(tmp_path / "unborn-all")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "draft.md", "hello\n")
    git(root, "add", "-A")
    assert ops.main(root, op="unstage_all")["ok"] is True
    assert status(root)["draft.md"] == "??"
    # …and again, with nothing left in the index: "already how you asked for it"
    # is not a failure.
    assert ops.main(root, op="unstage_all")["ok"] is True


def test_discard_all_where_git_tracks_nothing_yet(ops, tmp_path):
    # `git restore --worktree` calls a pathspec matching nothing tracked an ERROR
    # (exit 1), which is wrong for a scope holding only untracked files — there
    # the `clean` half is the entire operation.
    root = str(tmp_path / "untracked-only")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "loose.txt", "junk\n")
    got = ops.main(root, op="discard_all")
    assert got["ok"] is True, got
    assert not os.path.exists(os.path.join(root, "loose.txt"))


def test_a_git_without_switch_falls_back_to_checkout(ops, repo, monkeypatch):
    # `switch` needs git >= 2.23. The fallback is detected from the attempt's own
    # answer rather than by parsing `git --version` — a version string is one
    # more format to get wrong, and "did this git understand the verb" is the
    # question that matters.
    git(repo, "branch", "legacy-target")
    real_run = ops._run
    seen = []

    def old_git(root, *args):
        seen.append(list(args))
        if args and args[0] == "switch":
            return 1, b"", "git: 'switch' is not a git command. See 'git --help'."
        return real_run(root, *args)

    monkeypatch.setattr(ops, "_run", old_git)
    got = ops.main(repo, op="branch_checkout", name="legacy-target")
    assert got["ok"] is True, got
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "legacy-target"
    assert any(a and a[0] == "switch" for a in seen), "switch is tried first"


def test_the_legacy_fallback_spells_branch_creation_with_b_not_c(ops, repo, monkeypatch):
    # `-c` is `switch`'s flag; `checkout`'s is `-b`. Translating it is the only
    # difference between the two spellings for the calls this module makes.
    real_run = ops._run
    seen = []

    def old_git(root, *args):
        if args and args[0] == "switch":
            return 1, b"", "git: 'switch' is not a git command."
        seen.append(list(args))
        return real_run(root, *args)

    monkeypatch.setattr(ops, "_run", old_git)
    assert ops.main(repo, op="branch_create", name="legacy-new",
                    checkout=True)["ok"] is True
    checkouts = [a for a in seen if a and a[0] == "checkout"]
    assert checkouts, seen
    assert "-b" in checkouts[0] and "-c" not in checkouts[0]
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "legacy-new"


def test_stage_all_at_the_repository_root_stages_everything(ops, repo):
    write(repo, "pkg/mod.py", "two\n")
    write(repo, "top.txt", "changed\n")
    assert ops.main(repo, op="stage_all")["ok"] is True
    after = status(repo)
    assert after["pkg/mod.py"] == "M " and after["top.txt"] == "M "


# ------------------------------------------------------------------- discard


def test_discard_reverts_a_tracked_file(ops, repo):
    write(repo, "pkg/mod.py", "wrecked\n")
    got = ops.main(os.path.join(repo, "pkg"), op="discard", paths=["pkg/mod.py"])
    assert got["ok"] is True
    assert "pkg/mod.py" not in status(repo)
    with open(os.path.join(repo, "pkg", "mod.py"), encoding="utf-8") as handle:
        assert handle.read() == "one\n"


def test_discard_removes_an_untracked_file(ops, repo):
    write(repo, "pkg/fresh.txt", "new\n")
    got = ops.main(os.path.join(repo, "pkg"), op="discard", paths=["pkg/fresh.txt"])
    assert got["ok"] is True
    assert not os.path.exists(os.path.join(repo, "pkg", "fresh.txt"))


def test_discard_removes_an_untracked_directory(ops, repo):
    # `--untracked-files=normal` collapses a wholly untracked directory to one
    # `dir/` row, so the row the user clicks IS a directory — `clean -fd`, not
    # `-f` alone, which silently does nothing for a directory.
    write(repo, "pkg/scratch/a.txt", "a\n")
    write(repo, "pkg/scratch/deep/b.txt", "b\n")
    got = ops.main(os.path.join(repo, "pkg"), op="discard", paths=["pkg/scratch/"])
    assert got["ok"] is True, got
    assert not os.path.exists(os.path.join(repo, "pkg", "scratch"))


def test_discard_handles_a_mixed_batch_of_tracked_and_untracked(ops, repo):
    write(repo, "pkg/mod.py", "wrecked\n")
    write(repo, "pkg/fresh.txt", "new\n")
    got = ops.main(os.path.join(repo, "pkg"), op="discard",
                   paths=["pkg/mod.py", "pkg/fresh.txt"])
    assert got["ok"] is True
    assert status(repo) == {}


def test_discard_never_touches_an_ignored_file(ops, repo):
    # `git clean -x` is what would delete these, and it is FORBIDDEN here: a
    # `.env`, a `node_modules` or a virtualenv is exactly what people keep in an
    # ignored path, and "discard my edit" must never be able to mean that.
    write(repo, ".gitignore", "secrets.env\nbuild/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ignore", when="2026-10-02T10:00:00+00:00")
    write(repo, "secrets.env", "TOKEN=hunter2\n")
    write(repo, "build/artifact.bin", "junk\n")
    write(repo, "loose.txt", "untracked\n")

    got = ops.main(repo, op="discard_all")
    assert got["ok"] is True
    assert not os.path.exists(os.path.join(repo, "loose.txt")), "untracked went"
    assert os.path.exists(os.path.join(repo, "secrets.env")), "ignored file kept"
    assert os.path.exists(os.path.join(repo, "build", "artifact.bin"))


def test_no_op_ever_passes_the_x_flag_to_clean(ops, repo, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy_run(argv, **kwargs):
        seen.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    write(repo, "loose.txt", "x\n")
    write(repo, "pkg/mod.py", "y\n")
    assert ops.main(repo, op="discard_all")["ok"] is True
    cleans = [argv for argv in seen if "clean" in argv]
    assert cleans, "the untracked half must actually run clean"
    for argv in cleans:
        assert "-x" not in argv and "-X" not in argv
        assert not any(a.startswith("-") and "x" in a.lstrip("-") for a in argv)


def test_discard_all_is_scoped(ops, repo):
    write(repo, "pkg/mod.py", "wrecked\n")
    write(repo, "top.txt", "also wrecked\n")
    assert ops.main(os.path.join(repo, "pkg"), op="discard_all")["ok"] is True
    remaining = status(repo)
    assert "pkg/mod.py" not in remaining
    assert remaining["top.txt"] == " M", "out of scope survives"


# ------------------------------------------------------- scope and containment


def test_a_path_outside_the_open_scope_is_refused(ops, repo):
    # `top.txt` is a perfectly ordinary file inside the repository — it is out of
    # SCOPE, which is a different and stricter rule (GT-13). Mutations are
    # restricted to what the view is actually showing.
    write(repo, "top.txt", "changed\n")
    for op in ("stage", "unstage", "discard"):
        got = ops.main(os.path.join(repo, "pkg"), op=op, paths=["top.txt"])
        assert got["ok"] is False, op
        assert got["reason"] == "outside-scope", (op, got)
    assert status(repo)["top.txt"] == " M", "and nothing happened"


def test_an_absolute_path_is_refused_before_git_runs(ops, repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("git invoked with an unvalidated path")))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("git invoked with an unvalidated path")))
    got = ops.main(repo, op="stage", paths=[os.path.join(repo, "top.txt")])
    assert got["ok"] is False and got["reason"] == "outside-repo"


def test_a_dot_dot_path_is_refused_before_git_runs(ops, repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("git invoked with an unvalidated path")))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("git invoked with an unvalidated path")))
    for bad in ("../elsewhere.txt", "pkg/../../nope", ".."):
        got = ops.main(repo, op="discard", paths=[bad])
        assert got["ok"] is False, bad
        assert got["reason"] == "outside-repo", bad


def test_an_option_shaped_path_is_refused(ops, repo):
    # `--` guards the pathspec, so this cannot become an option — but a name
    # starting with `-` is refused anyway, because nothing legitimate in this UI
    # produces one and defence in depth is free here.
    got = ops.main(repo, op="stage", paths=["--force"])
    assert got["ok"] is False and got["reason"] in ("outside-repo", "bad-path")


def test_a_symlink_escaping_the_repository_is_refused(ops, repo, tmp_path):
    # The string checks pass — every segment is repo-relative — but the
    # consumers of that name RESOLVE it, so a link is how a repo-relative-looking
    # name reaches outside the work tree.
    outside = tmp_path / "secret.txt"
    outside.write_text("SENSITIVE\n", encoding="utf-8")
    os.symlink(str(outside), os.path.join(repo, "leak.txt"))
    got = ops.main(repo, op="discard", paths=["leak.txt"])
    assert got["ok"] is False and got["reason"] == "outside-repo"
    assert outside.read_text(encoding="utf-8") == "SENSITIVE\n"


def test_a_file_scope_restricts_operations_to_that_one_file(ops, repo):
    write(repo, "pkg/mod.py", "two\n")
    write(repo, "pkg/keep.py", "changed\n")
    scope = os.path.join(repo, "pkg", "mod.py")
    assert ops.main(scope, op="stage", paths=["pkg/mod.py"])["ok"] is True
    sibling = ops.main(scope, op="stage", paths=["pkg/keep.py"])
    assert sibling["ok"] is False and sibling["reason"] == "outside-scope"


def test_the_number_of_paths_in_one_call_is_bounded(ops, repo, monkeypatch):
    monkeypatch.setattr(ops, "MAX_PATHS", 2)
    got = ops.main(repo, op="stage", paths=["a", "b", "c"])
    assert got["ok"] is False and got["reason"] == "too-many-paths"


# -------------------------------------------------------------------- commit


def test_an_empty_commit_message_is_our_own_refusal(ops, repo):
    write(repo, "pkg/mod.py", "two\n")
    ops.main(repo, op="stage", paths=["pkg/mod.py"])
    for blank in ("", "   ", "\n\t "):
        got = ops.main(repo, op="commit", message=blank)
        assert got["ok"] is False, repr(blank)
        assert got["reason"] == "empty-message", repr(blank)
        assert "message" in got["message"].lower()


def test_committing_with_nothing_staged_is_refused_cleanly(ops, repo):
    write(repo, "pkg/mod.py", "two\n")  # dirty, but NOT staged
    got = ops.main(repo, op="commit", message="nothing here")
    assert got["ok"] is False and got["reason"] == "nothing-staged"
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "1"


def test_a_commit_lands_and_returns_its_short_sha_and_subject(ops, repo):
    write(repo, "pkg/mod.py", "two\n")
    ops.main(os.path.join(repo, "pkg"), op="stage", paths=["pkg/mod.py"])
    got = ops.main(os.path.join(repo, "pkg"), op="commit", message="a real commit")
    assert got["ok"] is True, got
    assert got["subject"] == "a real commit"
    assert got["short"] and len(got["short"]) >= 4
    assert git(repo, "rev-parse", "--short", "HEAD").strip() == got["short"]
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "2"


def test_a_commit_is_index_based_and_carries_out_of_scope_staged_work(ops, repo):
    # This is the honesty rule's OTHER half (GT-14): the commit really does take
    # the whole index, which is why the reader reports `staged_outside` and the
    # view warns. `git commit -- <paths>` is what we refuse to do, because it
    # commits the WORKING TREE for those paths and bypasses the index — a silent
    # data surprise, and not what the word "commit" means.
    write(repo, "pkg/mod.py", "two\n")
    write(repo, "top.txt", "changed\n")
    git(repo, "add", "-A")
    assert ops.main(os.path.join(repo, "pkg"), op="commit",
                    message="everything staged")["ok"] is True
    touched = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(touched) == ["pkg/mod.py", "top.txt"]


def test_a_multiline_commit_message_survives_verbatim(ops, repo):
    # The message is ONE argv element to `-m`. It may hold newlines, quotes,
    # backticks, a `$(...)` — none of it is ever interpreted, because there is no
    # shell anywhere in this module.
    body = "subject line\n\nbody with $(touch /tmp/pwned) and `backticks` and 'quotes'"
    write(repo, "pkg/mod.py", "two\n")
    ops.main(repo, op="stage", paths=["pkg/mod.py"])
    assert ops.main(repo, op="commit", message=body)["ok"] is True
    assert git(repo, "log", "-1", "--format=%B").rstrip("\n") == body
    assert not os.path.exists("/tmp/pwned")


# ------------------------------------------------------------------ branches


def test_branch_create_with_checkout_switches_to_it(ops, repo):
    got = ops.main(repo, op="branch_create", name="feature/x", checkout=True)
    assert got["ok"] is True, got
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "feature/x"


def test_branch_create_without_checkout_leaves_head_alone(ops, repo):
    assert ops.main(repo, op="branch_create", name="later",
                    checkout=False)["ok"] is True
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert "later" in git(repo, "for-each-ref", "--format=%(refname:short)",
                          "refs/heads/")


def test_branch_checkout_moves_head(ops, repo):
    git(repo, "branch", "other")
    got = ops.main(repo, op="branch_checkout", name="other")
    assert got["ok"] is True, got
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "other"


def test_branch_checkout_is_repo_wide_even_from_a_scoped_view(ops, repo):
    # A branch IS a repository concept — there is no such thing as checking one
    # out "just for pkg/". Deliberate and documented (GT-13).
    git(repo, "branch", "other")
    assert ops.main(os.path.join(repo, "pkg"), op="branch_checkout",
                    name="other")["ok"] is True
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "other"


def test_branch_delete_is_safe_only_and_surfaces_gits_refusal(ops, repo):
    git(repo, "checkout", "-q", "-b", "unmerged")
    write(repo, "pkg/mod.py", "divergent\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "divergent", when="2026-10-03T10:00:00+00:00")
    git(repo, "checkout", "-q", "main")

    got = ops.main(repo, op="branch_delete", name="unmerged")
    assert got["ok"] is False, got
    assert "not fully merged" in got["message"].lower()
    # And the branch is still there — `-D` is never reachable from this module.
    assert "unmerged" in git(repo, "for-each-ref", "--format=%(refname:short)",
                             "refs/heads/")


def test_branch_delete_removes_a_merged_branch(ops, repo):
    git(repo, "branch", "merged")
    assert ops.main(repo, op="branch_delete", name="merged")["ok"] is True
    assert "merged" not in git(repo, "for-each-ref", "--format=%(refname:short)",
                               "refs/heads/").split()


def test_a_malformed_branch_name_is_refused_by_git_not_by_a_regex(ops, repo):
    # `git check-ref-format --branch` is the authority: hand-rolling the rules
    # (no `..`, no `~^:?*[`, no trailing `.lock`, no leading/trailing slash, no
    # control characters, no "@{") is how a name that git accepts gets rejected
    # and, worse, one it rejects gets through.
    for bad in ("bad name", "with..dots", "tail.lock", "has~tilde", "trailing/",
                "", "   ", "ctrl\x01char"):
        got = ops.main(repo, op="branch_create", name=bad)
        assert got["ok"] is False, repr(bad)
        assert got["reason"] == "bad-branch", (bad, got)


def test_a_branch_name_starting_with_a_dash_is_refused_before_git_sees_it(
        ops, repo):
    # `check-ref-format` has no `--` terminator, so a leading `-` would be read
    # as one of ITS options. This is the one rule that cannot be delegated.
    for bad in ("-f", "--force", "-"):
        got = ops.main(repo, op="branch_create", name=bad)
        assert got["ok"] is False, bad
        assert got["reason"] == "bad-branch", bad


def test_a_branch_op_never_forces_anything(ops, repo, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy_run(argv, **kwargs):
        seen.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    git(repo, "branch", "gone")
    ops.main(repo, op="branch_create", name="fresh", checkout=True)
    ops.main(repo, op="branch_checkout", name="main")
    ops.main(repo, op="branch_delete", name="gone")
    for argv in seen:
        assert "-D" not in argv
        assert "--force" not in argv and "-f" not in argv
        assert "--amend" not in argv and "rebase" not in argv
        assert "--hard" not in argv


# -------------------------------------------------------------------- stashes


def test_stash_push_is_scoped_and_apply_brings_it_back(ops, reader, repo):
    write(repo, "pkg/mod.py", "stashable\n")
    write(repo, "top.txt", "not stashable from here\n")

    pushed = ops.main(os.path.join(repo, "pkg"), op="stash_push",
                      message="scoped work")
    assert pushed["ok"] is True, pushed
    after = status(repo)
    assert "pkg/mod.py" not in after, "the in-scope change was stashed"
    assert after["top.txt"] == " M", "the out-of-scope change stayed put"

    listed = reader.main(repo, op="stashes")
    assert listed["stashes"][0]["message"] == "scoped work"

    applied = ops.main(os.path.join(repo, "pkg"), op="stash_apply", index=0,
                       sha=listed["stashes"][0]["sha"])
    assert applied["ok"] is True
    assert status(repo)["pkg/mod.py"] == " M"
    # apply KEEPS the entry; that is the difference from pop.
    assert len(reader.main(repo, op="stashes")["stashes"]) == 1


def test_stash_push_can_include_untracked_files(ops, repo):
    write(repo, "pkg/fresh.txt", "new\n")
    assert ops.main(os.path.join(repo, "pkg"), op="stash_push",
                    include_untracked=True)["ok"] is True
    assert not os.path.exists(os.path.join(repo, "pkg", "fresh.txt"))


def test_stash_push_with_nothing_to_stash_is_a_clean_refusal(ops, repo):
    got = ops.main(os.path.join(repo, "pkg"), op="stash_push")
    assert got["ok"] is False and got["reason"] == "nothing-to-stash"


def test_stash_pop_restores_and_removes_the_entry(ops, reader, repo):
    write(repo, "pkg/mod.py", "stashable\n")
    ops.main(repo, op="stash_push", message="popme")
    sha = reader.main(repo, op="stashes")["stashes"][0]["sha"]
    got = ops.main(repo, op="stash_pop", index=0, sha=sha)
    assert got["ok"] is True, got
    assert status(repo)["pkg/mod.py"] == " M"
    assert reader.main(repo, op="stashes")["stashes"] == []


def test_stash_drop_removes_the_entry_without_restoring_it(ops, reader, repo):
    write(repo, "pkg/mod.py", "stashable\n")
    ops.main(repo, op="stash_push", message="dropme")
    sha = reader.main(repo, op="stashes")["stashes"][0]["sha"]
    got = ops.main(repo, op="stash_drop", index=0, sha=sha)
    assert got["ok"] is True, got
    assert status(repo) == {}, "dropped, not restored"
    assert reader.main(repo, op="stashes")["stashes"] == []


def test_a_negative_or_absurd_stash_index_is_refused(ops, repo):
    for bad in (-1, -99):
        got = ops.main(repo, op="stash_drop", index=bad, sha="0" * 40)
        assert got["ok"] is False and got["reason"] == "bad-index", bad
    missing = ops.main(repo, op="stash_apply", index=7, sha="0" * 40)
    assert missing["ok"] is False and missing["reason"] == "no-such-stash"


def test_a_stash_op_without_an_entry_id_is_refused(ops, repo):
    # A bare index is a POSITION, and a position is not an identity. Refusing the
    # id-less call is what makes the identity check unskippable.
    write(repo, "pkg/mod.py", "stashable\n")
    ops.main(repo, op="stash_push", message="anon")
    for op in ("stash_apply", "stash_pop", "stash_drop"):
        got = ops.main(repo, op=op, index=0)
        assert got["ok"] is False, op
        assert got["reason"] == "bad-stash-id", (op, got)


def test_a_stash_that_shifted_under_the_request_is_refused_not_dropped(
        ops, reader, repo):
    # THE bug this guards. `stash@{0}` means "the newest entry right now", so a
    # `stash push` from anywhere — this view, another tab, a terminal — renumbers
    # every entry. A confirmation can sit in the URL indefinitely, so a
    # bounds-checked index alone would drop whatever happens to be at position 0
    # when the user finally clicks Yes: a different, still-wanted stash, gone for
    # good.
    write(repo, "pkg/mod.py", "first\n")
    ops.main(repo, op="stash_push", message="the one the user looked at")
    seen = reader.main(repo, op="stashes")["stashes"][0]
    assert seen["index"] == 0

    # …and now something else is stashed, so index 0 is a DIFFERENT entry.
    write(repo, "pkg/keep.py", "second\n")
    ops.main(repo, op="stash_push", message="an unrelated, newer stash")
    shifted = reader.main(repo, op="stashes")
    assert shifted["stashes"][0]["message"] == "an unrelated, newer stash"
    assert shifted["stashes"][1]["sha"] == seen["sha"], "it moved to index 1"

    got = ops.main(repo, op="stash_drop", index=0, sha=seen["sha"])
    assert got["ok"] is False, got
    assert got["reason"] == "stash-moved"
    # Both entries survive: the one the user meant AND the one they did not.
    after = reader.main(repo, op="stashes")["stashes"]
    assert len(after) == 2
    assert {s["sha"] for s in after} == {seen["sha"], shifted["stashes"][0]["sha"]}


def test_a_stash_id_that_is_not_an_object_name_never_reaches_git(ops, repo):
    write(repo, "pkg/mod.py", "stashable\n")
    ops.main(repo, op="stash_push", message="x")
    for bad in ("--upload-pack=touch /tmp/x", "stash@{0}", "zzzz", "HEAD"):
        got = ops.main(repo, op="stash_drop", index=0, sha=bad)
        assert got["ok"] is False, bad
        assert got["reason"] == "bad-stash-id", (bad, got)


# -------------------------------------------------------------- fetch / pull / push


@pytest.fixture()
def wired(tmp_path):
    """A repo with a real bare `origin`, and a second clone to move it from."""
    root = _seed(str(tmp_path / "local"))
    remote = with_remote(root, str(tmp_path / "origin.git"))
    other = str(tmp_path / "other")
    git(str(tmp_path), "clone", "-q", remote, other)
    git(other, "config", "user.name", "Other Author")
    git(other, "config", "user.email", "other@example.com")
    git(other, "config", "commit.gpgsign", "false")
    return root, remote, other


def test_push_publishes_local_commits(ops, wired):
    root, remote, _ = wired
    write(root, "pkg/mod.py", "pushed\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "to push", when="2026-10-04T10:00:00+00:00")
    got = ops.main(root, op="push")
    assert got["ok"] is True, got
    assert git(remote, "log", "-1", "--format=%s").strip() == "to push"


def test_push_publishes_a_branch_that_has_no_upstream_yet(ops, wired):
    # The decision this pins: a push from a branch with no upstream SETS one
    # (`--set-upstream <remote> <branch>`) rather than failing with git's
    # "has no upstream branch" advice, which no button in a GUI can act on.
    # It is still never a force: it can only create a ref that does not exist.
    root, remote, _ = wired
    git(root, "checkout", "-q", "-b", "published")
    write(root, "pkg/mod.py", "on a new branch\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "new branch work",
        when="2026-10-05T10:00:00+00:00")
    got = ops.main(root, op="push")
    assert got["ok"] is True, got
    assert "published" in git(remote, "for-each-ref", "--format=%(refname:short)",
                              "refs/heads/").split()
    assert git(root, "rev-parse", "--abbrev-ref",
               "published@{upstream}").strip() == "origin/published"


def test_fetch_updates_the_remote_tracking_ref_without_touching_the_worktree(
        ops, reader, wired):
    root, _, other = wired
    write(other, "pkg/mod.py", "from elsewhere\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "elsewhere", when="2026-10-06T10:00:00+00:00")
    git(other, "push", "-q")

    assert reader.main(root)["repo"]["behind"] == 0, "not fetched yet"
    got = ops.main(root, op="fetch")
    assert got["ok"] is True, got
    assert reader.main(root)["repo"]["behind"] == 1
    assert git(root, "log", "-1", "--format=%s").strip() == "seed", "HEAD unmoved"


def test_pull_fast_forwards(ops, reader, wired):
    root, _, other = wired
    write(other, "pkg/mod.py", "from elsewhere\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "elsewhere", when="2026-10-07T10:00:00+00:00")
    git(other, "push", "-q")

    got = ops.main(root, op="pull")
    assert got["ok"] is True, got
    assert git(root, "log", "-1", "--format=%s").strip() == "elsewhere"


def test_pull_refuses_a_non_fast_forward_instead_of_merging(ops, wired):
    # Diverged histories are a decision only a human can make, and both automatic
    # answers (merge, rebase) rewrite or complicate history behind their back.
    # `--ff-only` turns that into a refusal, and the refusal names a terminal.
    root, _, other = wired
    write(other, "pkg/mod.py", "theirs\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "theirs", when="2026-10-08T10:00:00+00:00")
    git(other, "push", "-q")
    write(root, "pkg/mod.py", "mine\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "mine", when="2026-10-09T10:00:00+00:00")

    got = ops.main(root, op="pull")
    assert got["ok"] is False, got
    assert got["reason"] == "not-fast-forward"
    assert "terminal" in got["message"].lower()
    # Nothing was merged, nothing was rebased.
    assert git(root, "log", "-1", "--format=%s").strip() == "mine"
    assert git(root, "rev-list", "--count", "HEAD").strip() == "2"


def test_a_repository_with_no_remote_refuses_the_network_ops(ops, repo):
    for op in ("fetch", "pull", "push"):
        got = ops.main(repo, op=op)
        assert got["ok"] is False, op
        assert got["reason"] == "no-remote", (op, got)


def test_several_remotes_with_no_origin_and_no_upstream_is_refused(ops, repo, tmp_path):
    # This branch decides whether fetch/pull/push are offered at all, and there
    # is no defensible guess here — picking one for the user is how work lands on
    # the wrong server. The refusal names the situation instead.
    for name in ("upstream", "fork"):
        git(repo, "remote", "add", name, str(tmp_path / (name + ".git")))
    for op in ("fetch", "pull", "push"):
        got = ops.main(repo, op=op)
        assert got["ok"] is False, op
        assert got["reason"] == "no-remote", (op, got)
        assert "several" in got["message"].lower(), got


def test_a_sole_remote_that_is_not_called_origin_is_used(ops, repo, tmp_path):
    # The other side of the same rule: one remote is not a guess.
    remote = str(tmp_path / "elsewhere.git")
    from _git_repo import bare_repo

    bare_repo(remote)
    git(repo, "remote", "add", "elsewhere", remote)
    got = ops.main(repo, op="push")
    assert got["ok"] is True, got
    assert "elsewhere" in got["detail"]


# ------------------------------------------------------------------ refusals


def test_a_mount_backed_target_is_refused_for_a_MUTATING_op(ops, repo, monkeypatch):
    # The write path's half of MD-11 / GT-4. The gate keeps the mode from being
    # offered; this keeps a hand-written `?_mode=git` URL from reaching a
    # mutating git call across an rclone/NFS mount.
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", repo)
    write(repo, "pkg/mod.py", "two\n")
    for call in (dict(op="stage", paths=["pkg/mod.py"]),
                 dict(op="discard_all"),
                 dict(op="commit", message="nope"),
                 dict(op="push")):
        got = ops.main(repo, **call)
        assert got["ok"] is False, call
        assert got["reason"] == "mount", (call, got)
    assert status(repo)["pkg/mod.py"] == " M", "and nothing happened"


def test_an_unavailable_mount_detector_refuses_a_mutating_op(ops, repo, monkeypatch):
    import builtins
    import sys

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "appenv":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.delitem(sys.modules, "appenv", raising=False)
    got = ops.main(repo, op="stage_all")
    assert got["ok"] is False and got["reason"] == "mount"


def test_a_non_repository_is_refused(ops, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    got = ops.main(str(plain), op="stage_all")
    assert got["ok"] is False and got["reason"] == "not-a-repo"


def test_an_unknown_op_is_reported_not_raised(ops, repo):
    got = ops.main(repo, op="rewrite_history")
    assert got["ok"] is False and got["reason"] == "bad-op"


def test_an_empty_op_is_refused_rather_than_defaulting_to_something(ops, repo):
    # There is no safe default for a mutation, so the parameter has no default
    # behaviour at all — an omitted `op` is a bug in the caller, not a request.
    got = ops.main(repo)
    assert got["ok"] is False and got["reason"] == "bad-op"


def test_a_missing_git_binary_is_a_calm_refusal(ops, repo, monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(subprocess, "run", no_git)
    got = ops.main(repo, op="stage_all")
    assert got["ok"] is False and got["reason"] == "no-git"


# ------------------------------------------------------------------ hardening


def test_every_mutating_invocation_is_pinned_hardened_and_bounded(
        ops, repo, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy_run(argv, **kwargs):
        seen.append((list(argv), kwargs))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    write(repo, "pkg/mod.py", "two\n")
    write(repo, "pkg/fresh.txt", "new\n")
    assert ops.main(os.path.join(repo, "pkg"), op="stage_all")["ok"] is True
    assert ops.main(os.path.join(repo, "pkg"), op="commit",
                    message="hardened")["ok"] is True
    assert ops.main(repo, op="branch_create", name="hardening")["ok"] is True

    assert seen, "no git invocation was observed"
    root = os.path.realpath(repo)
    for argv, kwargs in seen:
        assert isinstance(argv, list), "argv list only — never a shell string"
        assert kwargs.get("shell") in (None, False)
        assert argv[0] == "git"
        assert "--no-pager" in argv
        # Exactly one invocation is pinned to the TARGET rather than the root:
        # the `--show-toplevel` bootstrap that discovers the root in the first
        # place. Everything that MUTATES is pinned to the resolved root.
        if "--show-toplevel" not in argv:
            assert argv[argv.index("-C") + 1] == root, argv
        env = kwargs.get("env") or {}
        assert env.get("GIT_TERMINAL_PROMPT") == "0"
        # A pathspec never appears before `--`.
        if "--" in argv:
            head = argv[: argv.index("--")]
            assert not any(a.startswith(":(literal)") for a in head)
        assert 0 < kwargs.get("timeout", 0) <= 30


def test_optional_locks_are_not_disabled_for_a_mutating_command(ops, repo, monkeypatch):
    # `GIT_OPTIONAL_LOCKS=0` is a READ-side nicety: it tells git not to take the
    # index lock merely to refresh stat information while answering a question.
    # A mutating command takes the lock it needs regardless, so carrying the
    # variable here would state a promise this module cannot keep — and would
    # suppress the opportunistic index refresh that makes the very next `git
    # status` accurate. It is dropped, deliberately.
    seen = []
    real_run = subprocess.run

    def spy_run(argv, **kwargs):
        seen.append(kwargs.get("env") or {})
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    write(repo, "pkg/mod.py", "two\n")
    assert ops.main(repo, op="stage_all")["ok"] is True
    assert seen
    for env in seen:
        assert env.get("GIT_OPTIONAL_LOCKS") != "0"


# ------------------------------------------- argument injection from the REPO
#
# The rule that user-typed names may not start with a dash has to apply just as
# hard to names GIT hands back, because git echoes them verbatim out of files
# inside `.git`, and those files are content. Neither shape below is reachable
# through git's own porcelain (`git branch` and `git remote add` both reject
# them), so a repository carrying one arrived some other way — a tarball, a zip,
# a shared drive; `git clone` does not copy config. That is a malformed or
# hostile repository, and the right answer is to stop.


def test_a_dash_leading_branch_name_from_git_head_is_refused(ops, repo):
    # `git symbolic-ref --short HEAD` prints whatever `.git/HEAD` names, so a
    # hand-written ref yields the string `-evil` — which, in the argv position
    # `push` puts a branch in, is an option.
    # No space in the payload: a refname may not contain one, so git would refuse
    # to resolve HEAD at all and the interesting case would never be reached. An
    # option-shaped name without a space resolves fine, which is the point.
    with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as handle:
        handle.write("ref: refs/heads/--upload-pack=cmd\n")
    for op in ("fetch", "pull", "push"):
        got = ops.main(repo, op=op)
        assert got["ok"] is False, op
        assert got["reason"] == "unsafe-name", (op, got)
        assert "--upload-pack=cmd" in got["message"]


def test_a_dash_leading_remote_name_from_git_config_is_refused(ops, repo):
    # `git remote` prints the config SECTION names verbatim, so a hand-written
    # `[remote "--receive-pack=<cmd>"]` is a name this list can contain. Without
    # the guard, one click of Push runs that command locally and the view reports
    # success.
    with open(os.path.join(repo, ".git", "config"), "a", encoding="utf-8") as handle:
        handle.write('[remote "--receive-pack=cmd"]\n'
                     "\turl = /tmp/nowhere\n"
                     "\tfetch = +refs/heads/*:refs/remotes/x/*\n")
    for op in ("fetch", "pull", "push"):
        got = ops.main(repo, op=op)
        assert got["ok"] is False, op
        assert got["reason"] == "unsafe-name", (op, got)
        assert "--receive-pack=cmd" in got["message"]


def test_a_hostile_remote_name_refuses_the_whole_repository_not_just_that_remote(
        ops, repo, tmp_path):
    # Filtering the bad name out and quietly using the good remote would hide
    # the fact that the repository is malformed — and would make the guard
    # depend on which remote happened to be picked.
    with_remote(repo, str(tmp_path / "good.git"))
    with open(os.path.join(repo, ".git", "config"), "a", encoding="utf-8") as handle:
        handle.write('[remote "--upload-pack=cmd"]\n\turl = /tmp/nowhere\n')
    got = ops.main(repo, op="fetch")
    assert got["ok"] is False and got["reason"] == "unsafe-name", got


def test_every_repo_derived_name_reaching_argv_is_preceded_by_a_terminator(
        ops, wired, monkeypatch):
    # The other half of the same guarantee. `_repo_name` refuses an option-shaped
    # value; `--` means an ordinary one can never be read as an option either.
    # Depending on only one of them is how a single missing terminator turns a
    # hostile repository into local command execution, so both are pinned.
    root, _, _ = wired
    seen = []
    real_run = subprocess.run

    def spy_run(argv, **kwargs):
        seen.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    ops.main(root, op="fetch")
    ops.main(root, op="pull")
    ops.main(root, op="push")
    network = [argv for argv in seen
               if {"fetch", "pull", "push"} & set(argv[:6] + argv[-6:])]
    assert network, "no network command was observed"
    for argv in network:
        verb = next(a for a in argv if a in ("fetch", "pull", "push"))
        tail = argv[argv.index(verb) + 1:]
        # Every positional (remote / refspec) sits after `--`. A command with no
        # positionals at all is fine; one with positionals and no `--` is not.
        positional = [a for a in tail if not a.startswith("-")]
        if positional:
            assert "--" in tail, argv
            assert all(a in tail[tail.index("--") + 1:] for a in positional), argv


def test_no_network_command_lets_config_decide_what_it_does(ops, wired, monkeypatch):
    # A bare `git push` means whatever `push.default` / `remote.pushDefault` say,
    # which can be "every matching branch" or "a different remote than the
    # success message names". Every network command here names its remote and its
    # refspec instead.
    root, _, _ = wired
    seen = []
    real_run = subprocess.run

    def spy_run(argv, **kwargs):
        seen.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    assert ops.main(root, op="push")["ok"] is True
    pushes = [argv for argv in seen if "push" in argv]
    assert pushes
    for argv in pushes:
        tail = argv[argv.index("push") + 1:]
        assert "origin" in tail, argv
        assert any(a.startswith("HEAD:refs/heads/") for a in tail), argv


def test_no_op_can_reach_a_history_rewriting_verb(ops):
    # A grep is a blunt instrument, and that is the point: these strings must not
    # be constructible from this module at all, so the file itself must not name
    # them. If a future change needs one, this test is the conversation.
    with open(OPS, encoding="utf-8") as handle:
        body = "\n".join(line.split("#", 1)[0] for line in handle)
    for forbidden in ('"--amend"', '"--hard"', '"rebase"', '"-D"', '"--force"',
                      '"--force-with-lease"', '"filter-branch"', '"reset"'):
        assert forbidden not in body, forbidden
