"""The `git` template's reader (`fused_render/templates/git/log.py`, SPEC §33).

Driven against a REAL throwaway repository (tests/_git_repo.py), never a mocked
`subprocess`: the module's entire job is to ask git and parse what git says, so a
fake git would test our own fiction of the porcelain format instead of the format.
The fixture repo has renames, a binary blob, history both inside and outside the
scoped subdirectory, and a dirty working tree spanning that boundary — so
"scoped to the open path" is an assertion and not a hope.

What is pinned here, beyond the happy path:

* **Scoping**, in both directions — a commit that touched only `README.md` must
  not appear in `pkg/`'s log, and a commit inside `pkg/` must not disappear from
  the repo root's.
* **Refusal is the module's own** (MD-11 / GT-4): a non-repo and a mount-backed
  target are refused HERE, not merely un-offered by the gate, and refusal is a
  calm payload rather than an exception — the view renders an empty state, never
  a traceback overlay.
* **The invocations are hardened**: `-C <root>`, `--no-pager`, `--` before every
  pathspec, a timeout on every call, argv lists only, and a `sha` that is not a
  hex object name never reaches git at all.
* **Diffs are capped** in bytes and in lines, with the truncation reported.
* The awkward states: empty repo, detached HEAD, a path with no history, a path
  outside the repo, binary files, renames.
"""
import importlib.util
import os
import subprocess

import pytest

from _git_repo import build_repo, empty_repo, git, git_available, write

READER = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "git", "log.py")

pytestmark = pytest.mark.skipif(not git_available(), reason="git binary not installed")


@pytest.fixture(scope="module")
def reader():
    spec = importlib.util.spec_from_file_location("git_log_reader", READER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    return build_repo(str(tmp_path_factory.mktemp("reader-repo")))


def subjects(payload):
    return [c["subject"] for c in payload["commits"]]


# --------------------------------------------------------------------- overview


def test_overview_of_the_repository_root(reader, repo):
    got = reader.main(repo)
    assert got["ok"] is True
    assert got["repo"]["root"] == os.path.realpath(repo)
    assert got["repo"]["name"] == os.path.basename(repo)
    assert got["repo"]["branch"] == "main"
    assert got["repo"]["detached"] is False
    assert got["repo"]["has_commits"] is True
    assert got["repo"]["dirty"] is True
    assert got["repo"]["rel"] == ""
    assert got["repo"]["is_dir"] is True
    assert got["repo"]["head"] and len(got["repo"]["head"]) >= 7


def test_overview_of_a_subdirectory_scopes_rel_and_kind(reader, repo):
    got = reader.main(os.path.join(repo, "pkg"))
    assert got["ok"] is True
    assert got["repo"]["rel"] == "pkg"
    assert got["repo"]["is_dir"] is True


def test_overview_of_a_file_scopes_to_that_file(reader, repo):
    got = reader.main(os.path.join(repo, "pkg", "core.py"))
    assert got["ok"] is True
    assert got["repo"]["rel"] == "pkg/core.py"
    assert got["repo"]["is_dir"] is False


def test_rel_is_posix_even_on_windows_separators(reader, repo):
    # The UI prints `rel` and every pathspec is built from it, so it is
    # normalized to forward slashes once, here, rather than at each use.
    got = reader.main(os.path.join(repo, "pkg") + os.sep)
    assert got["ok"] is True
    assert got["repo"]["rel"] == "pkg"
    assert "\\" not in got["repo"]["rel"]


# ------------------------------------------------------------------ commit log


def test_the_root_log_carries_every_commit_newest_first(reader, repo):
    got = reader.main(repo)
    assert subjects(got) == [
        "unrelated top change",
        "add a logo",
        "rename the module",
        "edit the module",
        "add the module",
        "add readme",
    ]


def test_commit_fields_are_parsed_from_a_nul_delimited_format(reader, repo):
    head = reader.main(repo)["commits"][0]
    assert head["subject"] == "unrelated top change"
    assert head["author"] == "Fixture Author"
    assert len(head["sha"]) == 40
    assert head["short"] and head["sha"].startswith(head["short"])
    assert head["date"].startswith("2026-01-06")
    assert head["relative"]  # git's own "N months ago" phrasing, not ours


def test_the_log_is_scoped_to_a_subdirectory(reader, repo):
    got = reader.main(os.path.join(repo, "pkg"))
    # Neither the README commits nor the assets commit touched pkg/.
    assert subjects(got) == ["rename the module", "edit the module", "add the module"]


def test_the_log_is_scoped_to_a_single_file(reader, repo):
    got = reader.main(os.path.join(repo, "pkg", "notes.md"))
    assert subjects(got) == ["add the module"]


def test_a_path_with_no_history_gets_an_empty_log_not_an_error(reader, repo):
    # `pkg/fresh.txt` is untracked: it has never been committed, so its log is
    # legitimately empty. That is an empty state, not a failure.
    got = reader.main(os.path.join(repo, "pkg", "fresh.txt"))
    assert got["ok"] is True
    assert got["commits"] == []
    assert got["has_more"] is False


def test_the_log_is_paginated_and_never_unbounded(reader, repo):
    first = reader.main(repo, op="log", limit=2, page=0)
    assert subjects(first) == ["unrelated top change", "add a logo"]
    assert first["has_more"] is True
    second = reader.main(repo, op="log", limit=2, page=1)
    assert subjects(second) == ["rename the module", "edit the module"]
    assert second["has_more"] is True
    last = reader.main(repo, op="log", limit=2, page=2)
    assert subjects(last) == ["add the module", "add readme"]
    assert last["has_more"] is False


def test_an_absurd_limit_is_clamped(reader, repo):
    # A hand-edited URL must not turn into an unbounded log.
    got = reader.main(repo, op="log", limit=10 ** 9)
    assert len(got["commits"]) <= reader.MAX_LOG_LIMIT


# ------------------------------------------------------------ uncommitted work


def test_uncommitted_changes_cover_staged_unstaged_and_untracked(reader, repo):
    got = reader.main(os.path.join(repo, "pkg"))
    by_path = {c["path"]: c for c in got["changes"]}
    assert set(by_path) == {"pkg/core.py", "pkg/staged.txt", "pkg/fresh.txt"}
    assert by_path["pkg/core.py"]["status"] == " M"
    assert by_path["pkg/core.py"]["unstaged"] is True
    assert by_path["pkg/core.py"]["staged"] is False
    assert by_path["pkg/staged.txt"]["status"] == "A "
    assert by_path["pkg/staged.txt"]["staged"] is True
    assert by_path["pkg/fresh.txt"]["status"] == "??"
    assert by_path["pkg/fresh.txt"]["untracked"] is True


def test_uncommitted_changes_are_scoped_to_the_open_path(reader, repo):
    # README.md is dirty too, but it is outside pkg/ — the whole point of a
    # scoped view is that it does not show up here.
    scoped = reader.main(os.path.join(repo, "pkg"))
    assert "README.md" not in {c["path"] for c in scoped["changes"]}
    # …while the repo root sees it.
    whole = reader.main(repo)
    assert "README.md" in {c["path"] for c in whole["changes"]}


def test_uncommitted_changes_for_a_single_file_are_that_file_only(reader, repo):
    got = reader.main(os.path.join(repo, "pkg", "core.py"))
    assert [c["path"] for c in got["changes"]] == ["pkg/core.py"]


def test_dirty_is_repo_wide_while_changes_are_scoped(reader, repo):
    # The header's clean/dirty light describes the REPOSITORY (that is what the
    # word means); the list below it describes the scope.
    got = reader.main(os.path.join(repo, "assets"))
    assert got["changes"] == []          # nothing dirty under assets/
    assert got["repo"]["dirty"] is True  # but the repo is


def test_a_clean_repository_reports_clean(reader, tmp_path):
    root = str(tmp_path / "clean")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "a.txt", "a\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "only commit", when="2026-02-01T10:00:00+00:00")
    got = reader.main(root)
    assert got["repo"]["dirty"] is False
    assert got["changes"] == []


def test_a_rename_reports_both_paths(reader, tmp_path):
    root = str(tmp_path / "renamed")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "old.txt", "x" * 200 + "\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add", when="2026-02-01T10:00:00+00:00")
    git(root, "mv", "old.txt", "new.txt")
    got = reader.main(root)
    entry = next(c for c in got["changes"] if c["path"] == "new.txt")
    assert entry["x"] == "R"
    assert entry["orig"] == "old.txt"


def test_the_change_list_is_capped(reader, tmp_path, monkeypatch):
    root = str(tmp_path / "messy")
    os.makedirs(root)
    git(root, "init", "-q")
    for i in range(12):
        write(root, f"f{i}.txt", "x\n")
    monkeypatch.setattr(reader, "MAX_CHANGES", 5)
    got = reader.main(root)
    assert len(got["changes"]) == 5
    assert got["changes_truncated"] is True


# ------------------------------------------------------------------ commit diff


def test_a_commit_diff_is_restricted_to_the_open_path(reader, repo):
    sha = next(c["sha"] for c in reader.main(repo)["commits"]
               if c["subject"] == "add the module")
    scoped = reader.main(os.path.join(repo, "pkg", "notes.md"), op="commit", sha=sha)
    assert scoped["ok"] is True
    assert "pkg/notes.md" in scoped["diff"]
    assert "pkg/mod.py" not in scoped["diff"]
    # …and unscoped (repo root) it shows both files of that commit.
    whole = reader.main(repo, op="commit", sha=sha)
    assert "pkg/notes.md" in whole["diff"] and "pkg/mod.py" in whole["diff"]


def test_a_commit_diff_carries_its_metadata(reader, repo):
    sha = reader.main(repo)["commits"][0]["sha"]
    got = reader.main(repo, op="commit", sha=sha)
    assert got["commit"]["subject"] == "unrelated top change"
    assert got["commit"]["author"] == "Fixture Author"
    assert got["commit"]["sha"] == sha


def test_a_commit_that_did_not_touch_the_path_yields_an_empty_diff(reader, repo):
    sha = next(c["sha"] for c in reader.main(repo)["commits"]
               if c["subject"] == "add a logo")
    got = reader.main(os.path.join(repo, "pkg"), op="commit", sha=sha)
    assert got["ok"] is True and got["empty"] is True and got["diff"] == ""


def test_a_binary_blob_is_reported_not_dumped(reader, repo):
    sha = next(c["sha"] for c in reader.main(repo)["commits"]
               if c["subject"] == "add a logo")
    got = reader.main(repo, op="commit", sha=sha)
    assert got["ok"] is True
    assert "Binary files" in got["diff"] or "GIT binary patch" in got["diff"]


def test_a_rename_commit_diff_shows_the_rename(reader, repo):
    sha = next(c["sha"] for c in reader.main(repo)["commits"]
               if c["subject"] == "rename the module")
    got = reader.main(repo, op="commit", sha=sha)
    assert "rename from pkg/mod.py" in got["diff"]
    assert "rename to pkg/core.py" in got["diff"]


def test_a_bogus_sha_is_refused_without_invoking_git(reader, repo, monkeypatch):
    # The sha arrives from a URL param. It is validated as a hex object name
    # BEFORE any argv is built, so an option-looking value ("--upload-pack=...")
    # can never become an argument at all.
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("git invoked with an unvalidated sha")))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("git invoked with an unvalidated sha")))
    for bad in ("--upload-pack=touch /tmp/x", "HEAD; rm -rf /", "../../etc/passwd",
                "zzzz", "", "a" * 200):
        got = reader.main(repo, op="commit", sha=bad)
        assert got["ok"] is False, bad
        assert got["reason"] == "bad-sha", bad


def test_an_unknown_but_well_formed_sha_is_a_clean_error(reader, repo):
    got = reader.main(repo, op="commit", sha="0" * 40)
    assert got["ok"] is False and got["reason"] == "no-such-commit"


# ------------------------------------------------------------- working-tree diff


def test_a_tracked_worktree_diff_is_against_head(reader, repo):
    got = reader.main(repo, op="worktree", entry="pkg/core.py")
    assert got["ok"] is True
    assert "return 111" in got["diff"] and "return 11" in got["diff"]


def test_a_staged_worktree_diff_is_included(reader, repo):
    # Staged-but-uncommitted content must show: the entry is "vs HEAD", not
    # "vs the index".
    got = reader.main(repo, op="worktree", entry="pkg/staged.txt")
    assert got["ok"] is True and "staged" in got["diff"]


def test_an_untracked_worktree_diff_shows_the_whole_file(reader, repo):
    got = reader.main(repo, op="worktree", entry="pkg/fresh.txt")
    assert got["ok"] is True
    assert got["untracked"] is True
    assert "brand new" in got["diff"]


def test_a_worktree_entry_escaping_the_repository_is_refused(reader, repo):
    for bad in ("../outside.txt", "/etc/passwd", "pkg/../../nope"):
        got = reader.main(repo, op="worktree", entry=bad)
        assert got["ok"] is False, bad
        assert got["reason"] == "outside-repo", bad


# ---------------------------------------------------------------- diff capping


def test_a_huge_diff_is_capped_by_bytes_and_says_so(reader, tmp_path, monkeypatch):
    root = str(tmp_path / "big")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "big.txt", "".join(f"line {i}\n" for i in range(20000)))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "big", when="2026-03-01T10:00:00+00:00")
    sha = reader.main(root)["commits"][0]["sha"]

    monkeypatch.setattr(reader, "MAX_DIFF_BYTES", 2048)
    got = reader.main(root, op="commit", sha=sha)
    assert got["ok"] is True
    assert got["truncated"] is True
    assert len(got["diff"].encode("utf-8")) <= 2048 + 8  # + a partial-char slack
    assert got["shown_lines"] < 20000


def test_a_huge_diff_is_capped_by_lines_and_says_so(reader, tmp_path, monkeypatch):
    root = str(tmp_path / "tall")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "tall.txt", "".join(f"line {i}\n" for i in range(5000)))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "tall", when="2026-03-02T10:00:00+00:00")
    sha = reader.main(root)["commits"][0]["sha"]

    monkeypatch.setattr(reader, "MAX_DIFF_LINES", 40)
    got = reader.main(root, op="commit", sha=sha)
    assert got["truncated"] is True
    assert got["shown_lines"] == 40
    assert got["diff"].count("\n") <= 40


# ------------------------------------------------------------------ empty repo


def test_an_empty_repository_is_a_first_class_state(reader, tmp_path):
    root = empty_repo(str(tmp_path / "unborn"))
    write(root, "draft.md", "hello\n")
    got = reader.main(root)
    assert got["ok"] is True
    assert got["repo"]["has_commits"] is False
    assert got["repo"]["branch"] == "main"   # the unborn branch still has a name
    assert got["commits"] == []
    assert [c["path"] for c in got["changes"]] == ["draft.md"]


def test_a_worktree_diff_works_before_the_first_commit(reader, tmp_path):
    root = empty_repo(str(tmp_path / "unborn2"))
    write(root, "draft.md", "hello\n")
    got = reader.main(root, op="worktree", entry="draft.md")
    assert got["ok"] is True and "hello" in got["diff"]


# ---------------------------------------------------------------- detached HEAD


def test_a_detached_head_is_reported_by_sha(reader, repo):
    older = reader.main(repo)["commits"][2]["sha"]
    worktree = os.path.join(os.path.dirname(repo), "detached-wt")
    git(repo, "worktree", "add", "-q", "--detach", worktree, older)
    try:
        got = reader.main(worktree)
        assert got["ok"] is True
        assert got["repo"]["detached"] is True
        assert got["repo"]["branch"] is None
        assert older.startswith(got["repo"]["head"])
    finally:
        git(repo, "worktree", "remove", "--force", worktree, check=False)


# -------------------------------------------------------------------- refusals


def test_a_non_repository_is_refused_by_the_module_itself(reader, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    got = reader.main(str(plain))
    assert got["ok"] is False
    assert got["reason"] == "not-a-repo"
    assert got["message"]  # something calm and human, for the empty state


def test_a_missing_path_is_refused(reader, tmp_path):
    got = reader.main(str(tmp_path / "gone" / "file.py"))
    assert got["ok"] is False and got["reason"] == "missing"


def test_a_mount_backed_target_is_refused_by_the_module(reader, repo, monkeypatch):
    # The gate is the UX; this is the guarantee (MD-11) — a hand-written
    # `?_mode=git` URL must not reach git over an rclone-NFS mount.
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", repo)
    got = reader.main(repo)
    assert got["ok"] is False and got["reason"] == "mount"


def test_an_unavailable_mount_detector_refuses(reader, repo, monkeypatch):
    import builtins
    import sys

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "appenv":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.delitem(sys.modules, "appenv", raising=False)
    got = reader.main(repo)
    assert got["ok"] is False and got["reason"] == "mount"


def test_a_missing_git_binary_is_a_calm_empty_state(reader, repo, monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(subprocess, "run", no_git)
    monkeypatch.setattr(subprocess, "Popen", no_git)
    got = reader.main(repo)
    assert got["ok"] is False and got["reason"] == "no-git"
    assert "git" in got["message"].lower()


def test_an_unknown_op_is_reported_not_raised(reader, repo):
    got = reader.main(repo, op="teleport")
    assert got["ok"] is False and got["reason"] == "bad-op"


# ------------------------------------------------------------------- hardening


def test_every_invocation_is_pinned_hardened_and_bounded(reader, repo, monkeypatch):
    seen = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def spy_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return real_run(argv, **kwargs)

    def spy_popen(argv, **kwargs):
        seen.append((argv, kwargs))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    payload = reader.main(os.path.join(repo, "pkg"))
    assert payload["ok"] is True
    sha = payload["commits"][0]["sha"]
    assert reader.main(os.path.join(repo, "pkg"), op="commit", sha=sha)["ok"] is True
    assert reader.main(repo, op="worktree", entry="pkg/core.py")["ok"] is True

    assert seen, "no git invocation was observed"
    root = os.path.realpath(repo)
    for argv, kwargs in seen:
        assert isinstance(argv, list), "argv list only — never a shell string"
        assert kwargs.get("shell") in (None, False)
        assert argv[0] == "git"
        assert "--no-pager" in argv
        assert "-C" in argv
        # Exactly one invocation is pinned to the TARGET rather than the root:
        # the `--show-toplevel` bootstrap that discovers the root in the first
        # place. Every other command is pinned to the resolved root, so a
        # relative pathspec means one thing and only one thing.
        if "--show-toplevel" not in argv:
            assert argv[argv.index("-C") + 1] == root, argv
        env = kwargs.get("env") or {}
        assert env.get("GIT_TERMINAL_PROMPT") == "0"
        assert env.get("GIT_OPTIONAL_LOCKS") == "0"
        assert kwargs.get("stdin") == subprocess.DEVNULL
        # A pathspec never appears before `--`, so a path can never be read as
        # a revision (and a revision never as a path).
        if "--" in argv:
            head = argv[: argv.index("--")]
            assert not any(a.startswith(":(literal)") for a in head)
        # Popen calls are bounded by a watchdog instead of `timeout=`; every
        # blocking `run` carries one.
        if kwargs.get("stdout") is subprocess.PIPE and "timeout" in kwargs:
            assert 0 < kwargs["timeout"] <= 30


def test_a_pathspec_is_literal_so_a_glob_character_is_not_a_glob(reader, tmp_path):
    root = str(tmp_path / "globby")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "a[1].txt", "bracketed\n")
    write(root, "a1.txt", "plain\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "both", when="2026-04-01T10:00:00+00:00")
    write(root, "a[1].txt", "bracketed twice\n")

    got = reader.main(os.path.join(root, "a[1].txt"))
    assert got["ok"] is True
    assert [c["path"] for c in got["changes"]] == ["a[1].txt"]
    assert subjects(got) == ["both"]


def test_an_empty_diff_reports_zero_shown_lines(reader, repo):
    # `"".split("\n")` is one (empty) line, so a patch that touched nothing would
    # otherwise claim to show a line and skew the truncation arithmetic.
    sha = next(c["sha"] for c in reader.main(repo)["commits"]
               if c["subject"] == "add a logo")
    got = reader.main(os.path.join(repo, "pkg"), op="commit", sha=sha)
    assert got["empty"] is True and got["shown_lines"] == 0


def test_an_untracked_diff_header_is_repository_relative(reader, repo):
    # `--no-index` echoes its arguments into the `a/…` / `b/…` header verbatim, so
    # passing the absolute path printed the reader's whole filesystem layout above
    # every untracked diff.
    got = reader.main(repo, op="worktree", entry="pkg/fresh.txt")
    assert got["ok"] is True
    assert "a/pkg/fresh.txt" in got["diff"]
    assert repo not in got["diff"]


# ------------------------------------------------------- caps: status byte bound
#
# The byte cap is reachable only in absurd states (~220k modified files at the
# 4 MB default — measured), so these lower the cap instead of building a fixture
# that big. What they pin is the FRAMING, which is where the first version was
# wrong: it sliced `subprocess.run`'s captured output, so the cap bounded no
# memory and cut at an arbitrary byte, showing a truncated path in the UI.


def _status_bytes(reader, root):
    """The exact status stream the reader parses, for sizing a cap against."""
    raw, truncated = reader._git_stream(
        os.path.realpath(root),
        ("status", "--porcelain=v1", "-z", "--untracked-files=normal",
         "--ignored=no"),
        10 ** 9, allow=(0,))
    assert truncated is False
    return raw


def test_the_status_cap_is_a_real_memory_bound_not_a_slice(reader, repo, monkeypatch):
    # `_git_stream` must stop READING at the cap. Proven by the returned length:
    # a post-hoc slice of captured output would be indistinguishable in content,
    # so the property under test is that the stream itself is bounded.
    raw, truncated = reader._git_stream(
        os.path.realpath(repo),
        ("status", "--porcelain=v1", "-z", "--untracked-files=normal",
         "--ignored=no"),
        24, allow=(0,))
    assert truncated is True
    assert len(raw) <= 24


def test_a_byte_capped_status_never_shows_a_truncated_path(reader, repo, monkeypatch):
    # The blocking defect: a mid-record cut left the last entry carrying half a
    # path — a wrong path in the UI, and a row that fails when clicked. Every
    # path that survives must be one git actually emitted, at every cap length.
    full = _status_bytes(reader, repo)
    real = {chunk.decode()[3:] for chunk in full.split(b"\0")
            if len(chunk) >= 4 and chunk[:2] != b"R "}
    assert real, "fixture must be dirty"

    for cap in range(4, len(full) + 4):
        monkeypatch.setattr(reader, "MAX_STATUS_BYTES", cap)
        got = reader.main(repo)
        assert got["ok"] is True, cap
        for change in got["changes"]:
            assert change["path"] in real, (
                f"cap={cap} produced a path git never emitted: {change['path']!r}")


def test_a_byte_capped_status_reports_truncation_and_stays_dirty(reader, repo, monkeypatch):
    monkeypatch.setattr(reader, "MAX_STATUS_BYTES", 20)
    got = reader.main(repo)
    assert got["ok"] is True
    assert got["changes_truncated"] is True
    # git had more to say than we read, so "clean" is not an answer we may give.
    assert got["repo"]["dirty"] is True


def test_an_uncapped_status_is_not_flagged_truncated(reader, repo):
    got = reader.main(repo)
    assert got["changes_truncated"] is False


def test_a_cut_between_a_renames_two_halves_drops_the_rename(reader, tmp_path, monkeypatch):
    # The subtlest half of the defect: a cut landing between `R <to>` and its
    # `<from>` shifted the pairing by one for everything after it. Cutting at
    # every byte of a rename-bearing status must never mis-pair — an entry either
    # carries its true `orig` or is not shown at all.
    root = str(tmp_path / "renames")
    os.makedirs(root)
    git(root, "init", "-q")
    for i in range(3):
        write(root, f"old{i}.txt", ("x" * 200 + "\n") * (i + 1))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add", when="2026-05-01T10:00:00+00:00")
    for i in range(3):
        git(root, "mv", f"old{i}.txt", f"new{i}.txt")

    truth = {f"new{i}.txt": f"old{i}.txt" for i in range(3)}
    full = _status_bytes(reader, root)
    assert b"R " in full

    for cap in range(4, len(full) + 4):
        monkeypatch.setattr(reader, "MAX_STATUS_BYTES", cap)
        got = reader.main(root)
        assert got["ok"] is True, cap
        for change in got["changes"]:
            if change["x"] == "R":
                assert change["orig"] == truth[change["path"]], (
                    f"cap={cap} mis-paired {change['path']} -> {change['orig']}")


def test_the_change_count_cap_still_reports_truncation(reader, tmp_path, monkeypatch):
    # MAX_CHANGES and the byte cap share one flag; this is the other cause.
    root = str(tmp_path / "many")
    os.makedirs(root)
    git(root, "init", "-q")
    for i in range(9):
        write(root, f"f{i}.txt", "x\n")
    monkeypatch.setattr(reader, "MAX_CHANGES", 4)
    got = reader.main(root)
    assert len(got["changes"]) == 4 and got["changes_truncated"] is True


# --------------------------------------------------- caps: pagination under drops


def test_a_dropped_log_record_does_not_end_pagination(reader, repo, monkeypatch):
    # `has_more` must count the records GIT emitted, not the ones we kept: one
    # dropped record on a full page made `len(commits) == limit`, so the UI said
    # "End of history for this path" while more commits existed.
    real = reader._git

    def corrupt_one(root, *args, **kwargs):
        out = real(root, *args, **kwargs)
        if "log" in args:
            lines = out.split(b"\n")
            if lines and lines[0]:
                lines[0] = b"not\x00enough\x00fields"  # 3 fields, not 6 -> dropped
            return b"\n".join(lines)
        return out

    monkeypatch.setattr(reader, "_git", corrupt_one)
    got = reader.main(repo, op="log", limit=2, page=0)
    assert got["ok"] is True
    # git emitted 3 records (limit + 1); one was dropped, so exactly `limit`
    # survive — the arrangement under which counting KEPT records computed
    # `2 > 2` == False and stopped pagination one page early.
    assert len(got["commits"]) == 2
    assert got["has_more"] is True, "a drop must not end pagination"


def test_the_last_page_still_reports_no_more_when_a_record_drops(reader, repo, monkeypatch):
    # The other direction: counting raw records must not INVENT a next page.
    real = reader._git

    def corrupt_one(root, *args, **kwargs):
        out = real(root, *args, **kwargs)
        if "log" in args:
            lines = [line for line in out.split(b"\n") if line]
            if lines:
                lines[0] = b"not\x00enough\x00fields"
            return b"\n".join(lines) + b"\n"
        return out

    monkeypatch.setattr(reader, "_git", corrupt_one)
    got = reader.main(repo, op="log", limit=2, page=2)  # 6 commits: last page
    assert got["has_more"] is False


# ---------------------------------------------------------- symlink containment


def test_an_untracked_symlink_escaping_the_repository_is_refused(reader, tmp_path):
    # `_check_op`'s string check passes — every segment is repo-relative — but
    # `git diff --no-index` follows the link and would render the target's bytes.
    root = str(tmp_path / "linky")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "seed.txt", "seed\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed", when="2026-06-01T10:00:00+00:00")
    outside = tmp_path / "secret.txt"
    outside.write_text("SENSITIVE\n", encoding="utf-8")
    os.symlink(str(outside), os.path.join(root, "leak.txt"))

    got = reader.main(root, op="worktree", entry="leak.txt")
    assert got["ok"] is False
    assert got["reason"] in ("outside-repo", "symlink")
    assert "SENSITIVE" not in str(got)


def test_a_symlink_whose_target_stays_inside_is_still_refused(reader, tmp_path):
    # Containment passes here, so `islink` is the check that catches it: the diff
    # would show the target's content under the link's name — a different file
    # than the row claims.
    root = str(tmp_path / "inside")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "real.txt", "real\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed", when="2026-06-02T10:00:00+00:00")
    os.symlink(os.path.join(root, "real.txt"), os.path.join(root, "alias.txt"))

    got = reader.main(root, op="worktree", entry="alias.txt")
    assert got["ok"] is False and got["reason"] == "symlink"


def test_a_symlinked_parent_directory_is_refused(reader, tmp_path):
    # The case `os.path.islink` on the final component misses entirely: the entry
    # itself is an ordinary file, reached through a linked directory.
    root = str(tmp_path / "viadir")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "seed.txt", "seed\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed", when="2026-06-03T10:00:00+00:00")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "file.txt").write_text("OUTSIDE\n", encoding="utf-8")
    os.symlink(str(elsewhere), os.path.join(root, "hop"))

    got = reader.main(root, op="worktree", entry="hop/file.txt")
    assert got["ok"] is False and got["reason"] == "outside-repo"
    assert "OUTSIDE" not in str(got)


def test_an_ordinary_entry_is_unaffected_by_the_containment_check(reader, repo):
    # The guard must not cost the happy path: a plain tracked file and a plain
    # untracked file both still diff.
    assert reader.main(repo, op="worktree", entry="pkg/core.py")["ok"] is True
    assert reader.main(repo, op="worktree", entry="pkg/fresh.txt")["ok"] is True


# ----------------------------------------------- A: the log window's own ceiling
#
# The page grows its WINDOW (`limit = PAGE_SIZE * pages`) rather than paging, so
# `has_more` alone cannot drive "load more": once the request is clamped to
# MAX_LOG_LIMIT, `has_more` is honestly true and every further click returns the
# identical rows. `capped` is the signal that separates the two.


@pytest.fixture(scope="module")
def deep_repo(tmp_path_factory):
    """A repo with more commits than the reader will ever hand back at once."""
    root = str(tmp_path_factory.mktemp("deep"))
    git(root, "init", "-q")
    write(root, "f.txt", "0\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "c0", when="2026-01-01T00:00:00+00:00")
    # `git commit` per revision is far too slow at this depth; commit-tree builds
    # the same linear history directly out of one unchanging tree.
    tree = git(root, "rev-parse", "HEAD^{tree}").strip()
    parent = git(root, "rev-parse", "HEAD").strip()
    for i in range(1, 12):
        parent = git(root, "commit-tree", tree, "-p", parent, "-m", f"c{i}",
                     when="2026-01-01T00:00:00+00:00").strip()
    git(root, "reset", "-q", "--hard", parent)
    return root


def test_a_small_window_is_not_reported_as_capped(reader, deep_repo):
    got = reader.main(deep_repo, op="log", limit=4)
    assert len(got["commits"]) == 4
    assert got["has_more"] is True
    assert got["capped"] is False, "a limit under the ceiling is not a clamp"


def test_a_request_over_the_ceiling_is_capped_and_stops_offering_more(
        reader, deep_repo, monkeypatch):
    # The defect: clamped limit -> `--max-count=limit+1` -> more records than the
    # clamp -> `has_more` true forever, so "Load more" never terminated.
    monkeypatch.setattr(reader, "MAX_LOG_LIMIT", 5)
    got = reader.main(deep_repo, op="log", limit=30)
    assert len(got["commits"]) == 5
    assert got["capped"] is True
    assert got["max_commits"] == 5
    # `has_more` stays HONEST — more commits do exist — and `capped` is what tells
    # the UI another click cannot reach them. Conflating the two is the bug.
    assert got["has_more"] is True


def test_growing_the_window_terminates_at_the_ceiling(reader, deep_repo, monkeypatch):
    # Walk the client's actual scheme (limit = page_size * pages, page 0) and
    # assert it reaches a state where the button is no longer offered.
    monkeypatch.setattr(reader, "MAX_LOG_LIMIT", 6)
    page_size, seen = 3, []
    for pages in range(1, 8):
        got = reader.main(deep_repo, op="overview", limit=page_size * pages)
        seen.append((len(got["commits"]), got["has_more"], got["capped"]))
        if not (got["has_more"] and not got["capped"]):
            break
    else:
        raise AssertionError(f"'load more' never terminated: {seen}")
    rows, has_more, capped = seen[-1]
    assert rows == 6 and has_more is True and capped is True, seen


def test_the_ceiling_reached_exactly_reports_end_not_cap_pressure(reader, deep_repo,
                                                                 monkeypatch):
    # 12 commits, ceiling 12: the window is clamped, but there is genuinely
    # nothing beyond it — so `has_more` must be False and the UI must say "end of
    # history" rather than "showing the most recent 12".
    monkeypatch.setattr(reader, "MAX_LOG_LIMIT", 12)
    got = reader.main(deep_repo, op="log", limit=999)
    assert len(got["commits"]) == 12
    assert got["capped"] is True
    assert got["has_more"] is False


def test_the_two_has_more_defects_do_not_mask_each_other(reader, deep_repo, monkeypatch):
    # One drop on a clamped page: the earlier fix (count raw records) must still
    # keep `has_more` true, and the clamp must still be reported — neither signal
    # may swallow the other.
    monkeypatch.setattr(reader, "MAX_LOG_LIMIT", 5)
    real = reader._git

    def corrupt_one(root, *args, **kwargs):
        out = real(root, *args, **kwargs)
        if "log" in args:
            lines = [line for line in out.split(b"\n") if line]
            if lines:
                lines[0] = b"not\x00enough\x00fields"
            return b"\n".join(lines) + b"\n"
        return out

    monkeypatch.setattr(reader, "_git", corrupt_one)
    got = reader.main(deep_repo, op="log", limit=30)
    assert got["has_more"] is True and got["capped"] is True
    # `--max-count=6` (clamp 5 + the probe row) minus the one dropped record fills
    # the page exactly — the arrangement in which counting KEPT records would have
    # computed `5 > 5` == False and reported the end of history at the clamp.
    assert len(got["commits"]) == 5


# ------------------------------------------- B: both sides of a rename are scoped


@pytest.fixture()
def moved_repo(tmp_path):
    """A repo with a file moved OUT of `pkg/`, one moved IN, one renamed inside."""
    root = str(tmp_path / "moves")
    os.makedirs(root)
    git(root, "init", "-q")
    body = ("x" * 200 + "\n")
    write(root, "pkg/leaving.py", body)
    write(root, "pkg/staying.py", body + "stay\n")
    write(root, "outside/arriving.py", body + "arrive\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed", when="2026-07-01T10:00:00+00:00")
    git(root, "mv", os.path.join("pkg", "leaving.py"), os.path.join("outside", "gone.py"))
    git(root, "mv", os.path.join("outside", "arriving.py"), os.path.join("pkg", "here.py"))
    git(root, "mv", os.path.join("pkg", "staying.py"), os.path.join("pkg", "renamed.py"))
    return root


def test_a_rename_out_of_the_scope_is_still_listed(reader, moved_repo):
    # The defect: only the NEW path was scope-tested, so a file moved OUT of the
    # open folder vanished from Uncommitted — which is exactly the change the view
    # exists to surface.
    got = reader.main(os.path.join(moved_repo, "pkg"))
    by_path = {c["path"]: c for c in got["changes"]}
    assert "outside/gone.py" in by_path, "a move out of the scope was dropped"
    gone = by_path["outside/gone.py"]
    assert gone["orig"] == "pkg/leaving.py"
    assert gone["moved"] == "out"
    assert "out" in gone["label"].lower()


def test_a_rename_into_the_scope_is_listed_as_moved_in(reader, moved_repo):
    got = reader.main(os.path.join(moved_repo, "pkg"))
    here = next(c for c in got["changes"] if c["path"] == "pkg/here.py")
    assert here["orig"] == "outside/arriving.py"
    assert here["moved"] == "in"
    assert "into" in here["label"].lower()


def test_a_rename_wholly_inside_the_scope_is_not_a_move(reader, moved_repo):
    # Both sides in scope: an ordinary rename, and it must NOT wear a direction.
    got = reader.main(os.path.join(moved_repo, "pkg"))
    inside = next(c for c in got["changes"] if c["path"] == "pkg/renamed.py")
    assert inside["orig"] == "pkg/staying.py"
    assert inside["moved"] is None
    assert inside["label"] == "Renamed"


def test_a_rename_touching_neither_side_of_the_scope_stays_out(reader, moved_repo):
    # The filter must still filter: from `outside/`, the purely-inside-pkg rename
    # is not this scope's business.
    got = reader.main(os.path.join(moved_repo, "outside"))
    assert "pkg/renamed.py" not in {c["path"] for c in got["changes"]}
    # …while both moves, which each have one side here, are listed.
    assert {"outside/gone.py", "pkg/here.py"} <= {c["path"] for c in got["changes"]}


def test_the_repository_root_sees_every_rename_with_no_direction(reader, moved_repo):
    got = reader.main(moved_repo)
    for change in got["changes"]:
        if change["orig"]:
            assert change["moved"] is None, "nothing is 'moved' relative to the root"


def test_a_moved_out_row_still_opens_its_diff(reader, moved_repo):
    # The row's path is outside the scope, but the pathspec is repo-relative, so
    # clicking it must still resolve — a listed row that cannot be opened is worse
    # than one that was hidden.
    got = reader.main(moved_repo, op="worktree", entry="outside/gone.py")
    assert got["ok"] is True


# --------------------------------------- C: an untracked directory has no diff


@pytest.fixture()
def untracked_dir_repo(tmp_path):
    root = str(tmp_path / "utd")
    os.makedirs(root)
    git(root, "init", "-q")
    write(root, "tracked.txt", "tracked\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed", when="2026-07-02T10:00:00+00:00")
    write(root, "fresh/a.txt", "aaa\n")
    write(root, "fresh/b.txt", "bbb\n")
    write(root, "fresh/deep/c.txt", "ccc\n")
    return root


def test_git_collapses_the_untracked_directory_to_one_row(reader, untracked_dir_repo):
    # The premise: `--untracked-files=normal` reports `fresh/`, not its files.
    got = reader.main(untracked_dir_repo)
    assert [c["path"] for c in got["changes"]] == ["fresh/"]


def test_an_untracked_directory_lists_its_contents_instead_of_an_empty_diff(
        reader, untracked_dir_repo):
    # The defect: `fresh/` fell through to the vs-HEAD branch, `git diff HEAD --
    # fresh/` was empty, and the pane showed the commit-oriented copy — wrong
    # twice, since it is not a commit and the path IS in scope.
    got = reader.main(untracked_dir_repo, op="worktree", entry="fresh/")
    assert got["ok"] is True
    assert got["kind"] == "untracked-dir"
    assert got.get("empty") is None, "a directory must not claim an empty diff"
    assert sorted(got["files"]) == ["fresh/a.txt", "fresh/b.txt", "fresh/deep/c.txt"]
    assert got["truncated"] is False


def test_an_untracked_directory_resolves_without_its_trailing_slash_too(
        reader, untracked_dir_repo):
    # `_check_op` strips the trailing slash (it is a path, and a slash must not
    # change containment), so directory-ness is re-derived by stat.
    got = reader.main(untracked_dir_repo, op="worktree", entry="fresh")
    assert got["kind"] == "untracked-dir" and len(got["files"]) == 3


def test_an_untracked_directory_listing_honours_gitignore(reader, untracked_dir_repo):
    # `ls-files --others --exclude-standard` is asked precisely so the answer is
    # "what git would add", excludes included, rather than a reimplementation.
    write(untracked_dir_repo, ".gitignore", "fresh/b.txt\n")
    got = reader.main(untracked_dir_repo, op="worktree", entry="fresh/")
    assert "fresh/b.txt" not in got["files"]
    assert "fresh/a.txt" in got["files"]


def test_an_untracked_directory_listing_is_capped(reader, untracked_dir_repo, monkeypatch):
    monkeypatch.setattr(reader, "MAX_CHANGES", 2)
    got = reader.main(untracked_dir_repo, op="worktree", entry="fresh/")
    assert len(got["files"]) == 2 and got["truncated"] is True


def test_a_wholly_ignored_untracked_directory_lists_nothing(reader, untracked_dir_repo):
    write(untracked_dir_repo, ".gitignore", "fresh/\n")
    got = reader.main(untracked_dir_repo, op="worktree", entry="fresh/")
    # Reachable from a stale `wt=` deep link after the ignore was added. An empty
    # list, not a lie about a diff — the UI has its own sentence for this.
    assert got["ok"] is True and got["kind"] == "untracked-dir" and got["files"] == []


def test_a_file_entry_still_reports_kind_diff(reader, repo):
    # The `kind` discriminator must be present on the ordinary path too, or the
    # UI cannot tell a real empty diff from a directory.
    assert reader.main(repo, op="worktree", entry="pkg/core.py")["kind"] == "diff"
    assert reader.main(repo, op="worktree", entry="pkg/fresh.txt")["kind"] == "diff"


def test_a_tracked_directory_still_diffs_against_head(reader, repo):
    # Only UNTRACKED directories take the listing branch; a tracked directory has
    # a real diff and must keep it.
    got = reader.main(repo, op="worktree", entry="pkg")
    assert got["ok"] is True and got["kind"] == "diff"
    assert "return 111" in got["diff"]
