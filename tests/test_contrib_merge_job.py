"""Merge with Claude jobs (§16.4) against real temp git repos: merge-only,
one resolution, the server's checks. The resolver seam stands in for
Claude: each fake edits the conflicted files the way a resolution would (or
misbehaves the way Claude might)."""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from fused_render.contrib import merge_job, replay, store


def git(cwd, *args, check=True):
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    res = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                         text=True, env=env)
    if check and res.returncode != 0:
        raise AssertionError(f"git {args}: {res.stderr}")
    return res.stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(merge_job, "_jobs", {})
    monkeypatch.setattr(merge_job, "_swept", True)
    return h


def _repo(tmp_path, author_text, bob_text):
    """Bob's copy with one commit of his; the author's latest (`theirs`)
    is a side commit it has. Returns (bob_dir, ours, theirs)."""
    bob = tmp_path / "bob"
    bob.mkdir()
    git(bob, "init", "-q", "-b", "main")
    git(bob, "config", "user.name", "Bob Noreply")
    git(bob, "config", "user.email", "bob@users.noreply.github.com")
    write(bob / "index.html", "<title>A</title>\n<p>hello</p>\n")
    write(bob / "other.txt", "same\n")
    git(bob, "add", "-A")
    git(bob, "commit", "-q", "-m", "base")
    git(bob, "checkout", "-q", "-b", "author")
    write(bob / "index.html", author_text)
    write(bob / "NOTES.md", "author notes\n")
    git(bob, "add", "-A")
    git(bob, "commit", "-q", "-m", "author: world")
    theirs = git(bob, "rev-parse", "HEAD")
    git(bob, "checkout", "-q", "main")
    git(bob, "branch", "-q", "-D", "author")
    git(bob, "update-ref", "refs/remotes/origin/main", theirs)
    write(bob / "index.html", bob_text)
    git(bob, "commit", "-q", "-am", "Make the greeting friendlier")
    return str(bob), git(bob, "rev-parse", "HEAD"), theirs


@pytest.fixture
def overlap(tmp_path, home):
    bob, ours, theirs = _repo(tmp_path, "<title>A</title>\n<p>hello world</p>\n",
                              "<title>A</title>\n<p>hello there</p>\n")
    store.put(bob, store.new_record("github", repo="alice/sine", branch="main",
                                    classified_by="install"))
    return bob, ours, theirs


@pytest.fixture
def clean(tmp_path, home):
    return _repo(tmp_path, "<title>A</title>\n<p>hello</p>\n<p>more</p>\n",
                 "<title>B</title>\n<p>hello</p>\n")


COMBINED = "<title>A</title>\n<p>hello there, world</p>\n"


def resolve_with(text, summary="Kept both greetings.", extra=None):
    def resolver(wt, files, context):
        assert files == ["index.html"]
        assert "<<<<<<<" in read(os.path.join(wt, "index.html"))
        assert context["first"] == "your version of the app"
        assert context["second"] == "alice's latest version"
        write(os.path.join(wt, "index.html"), text)
        if extra:
            extra(wt)
        return {"ok": True, "summary": summary}
    return resolver


def wait(job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = merge_job.get(job_id)
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def worktrees(repo):
    return [ln for ln in git(repo, "worktree", "list", "--porcelain").splitlines()
            if ln.startswith("worktree ")]


def parents(repo, sha):
    return git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]


def test_conflict_resolved_merges_and_keeps_bobs_commit(overlap):
    bob, ours, theirs = overlap
    jid = merge_job.start(bob, purpose="update", ours=ours, theirs=theirs,
                          resolver=resolve_with(COMBINED))
    job = wait(jid)
    assert job["status"] == "ready", job
    assert job["summary"] == "Kept both greetings." and job["files"] == ["index.html"]
    assert git(bob, "rev-parse", "HEAD") == ours           # nothing moved yet
    res = merge_job.accept(jid)
    assert res["ok"], res
    head = git(bob, "rev-parse", "HEAD")
    assert res["tip"] == head and parents(bob, head) == [ours, theirs]
    assert read(os.path.join(bob, "index.html")) == COMBINED
    assert read(os.path.join(bob, "NOTES.md")) == "author notes\n"
    assert git(bob, "log", "-1", "--format=%an <%ae>|%s") == \
        "Bob Noreply <bob@users.noreply.github.com>|Bring in alice's latest"
    # ours is the merge's first parent: nothing dropped, so no backup.
    assert git(bob, "for-each-ref", "refs/fused/backup/") == ""
    assert worktrees(bob) == [f"worktree {os.path.realpath(bob)}"]
    rec = store.get(bob)
    assert rec["needs_merge"] is None
    assert rec["activity"][-1]["text"] == "Combined your changes with alice's latest."


def test_clean_merge_needs_no_claude_and_no_worktree(clean):
    bob, ours, theirs = clean

    def never(*_a):
        raise AssertionError("Claude must not run for a clean merge")
    job = wait(merge_job.start(bob, purpose="original", ours=ours, theirs=theirs,
                               resolver=never))
    assert job["status"] == "ready" and job["summary"] == merge_job.CLEAN_MERGE_SUMMARY
    assert parents(bob, job["tip"]) == [ours, theirs]
    assert not os.path.exists(os.path.join(replay.work_dir(), job["id"]))


def test_markers_left_every_round_fails_and_leaves_app_alone(overlap, monkeypatch):
    bob, ours, theirs = overlap
    monkeypatch.setattr(merge_job, "MAX_ROUNDS", 2)
    calls = []

    def lazy(wt, files, context):
        calls.append(files)
        return {"ok": True, "summary": "did nothing"}
    job = wait(merge_job.start(bob, purpose="update", ours=ours, theirs=theirs, resolver=lazy))
    assert job["status"] == "failed" and job["error"] == merge_job.FAILED_MESSAGE
    assert len(calls) == 2
    assert git(bob, "rev-parse", "HEAD") == ours
    assert worktrees(bob) == [f"worktree {os.path.realpath(bob)}"]


def test_edits_outside_the_overlap_are_put_back(overlap):
    bob, ours, theirs = overlap

    def meddle(wt):
        write(os.path.join(wt, "other.txt"), "changed by Claude\n")
        write(os.path.join(wt, "new.txt"), "new\n")
    job = wait(merge_job.start(bob, purpose="update", ours=ours, theirs=theirs,
                               resolver=resolve_with(COMBINED, extra=meddle)))
    assert job["status"] == "ready", job
    assert merge_job.accept(job["id"])["ok"]
    assert read(os.path.join(bob, "other.txt")) == "same\n"
    assert not os.path.exists(os.path.join(bob, "new.txt"))


def test_resolver_that_commits_the_merge_itself_is_checked(overlap):
    bob, ours, theirs = overlap

    def commits(wt, files, context):
        write(os.path.join(wt, "index.html"), COMBINED)
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "done by Claude")
        return {"ok": True, "summary": "Done."}
    job = wait(merge_job.start(bob, purpose="update", ours=ours, theirs=theirs,
                               resolver=commits))
    assert job["status"] == "ready", job
    assert parents(bob, job["tip"]) == [ours, theirs]


def test_resolver_that_abandons_the_merge_fails(overlap):
    bob, ours, theirs = overlap

    def aborts(wt, files, context):
        git(wt, "merge", "--abort")
        write(os.path.join(wt, "index.html"), COMBINED)
        git(wt, "commit", "-q", "-am", "only mine")
        return {"ok": True, "summary": "Done."}
    job = wait(merge_job.start(bob, purpose="update", ours=ours, theirs=theirs,
                               resolver=aborts))
    assert job["status"] == "failed"
    assert git(bob, "rev-parse", "HEAD") == ours


def test_accept_after_the_copy_moved_is_stale(overlap):
    bob, ours, theirs = overlap
    job = wait(merge_job.start(bob, purpose="update", ours=ours, theirs=theirs,
                               resolver=resolve_with(COMBINED)))
    write(os.path.join(bob, "other.txt"), "a newer turn\n")
    git(bob, "commit", "-q", "-am", "newer")
    newer = git(bob, "rev-parse", "HEAD")
    res = merge_job.accept(job["id"])
    assert not res["ok"] and res["reason"] == "stale"
    assert git(bob, "rev-parse", "HEAD") == newer
    assert merge_job.get(job["id"])["status"] == "discarded"


def test_overlapping_unsaved_edit_defers_accept(overlap):
    bob, ours, theirs = overlap
    job = wait(merge_job.start(bob, purpose="update", ours=ours, theirs=theirs,
                               resolver=resolve_with(COMBINED)))
    write(os.path.join(bob, "index.html"), "unsaved\n")
    res = merge_job.accept(job["id"])
    assert not res["ok"] and res["reason"] == "busy"
    assert merge_job.get(job["id"])["status"] == "ready"
    assert read(os.path.join(bob, "index.html")) == "unsaved\n"
    git(bob, "checkout", "--", "index.html")
    assert merge_job.accept(job["id"])["ok"]


def test_land_false_only_answers_the_tip(overlap):
    bob, ours, theirs = overlap
    job = wait(merge_job.start(bob, purpose="proposal", ours=ours, theirs=theirs, land=False,
                               sides={"first": "your version of the app",
                                      "second": "alice's latest version"},
                               resolver=resolve_with(COMBINED)))
    res = merge_job.accept(job["id"])
    assert res["ok"] and parents(bob, res["tip"]) == [ours, theirs]
    assert git(bob, "rev-parse", "HEAD") == ours
    assert worktrees(bob) == [f"worktree {os.path.realpath(bob)}"]


def test_one_job_per_app_and_discard(overlap):
    bob, ours, theirs = overlap
    gate = []

    def slow(wt, files, context):
        while not gate and not context["job"]["cancel"]:
            time.sleep(0.02)
        return {"ok": False, "error": None}
    first = merge_job.start(bob, purpose="update", ours=ours, theirs=theirs, resolver=slow)
    assert merge_job.start(bob, purpose="update", ours=ours, theirs=theirs) == first
    assert merge_job.for_app(bob)["id"] == first
    assert merge_job.discard(first)["ok"]
    gate.append(1)
    deadline = time.time() + 10
    while worktrees(bob) != [f"worktree {os.path.realpath(bob)}"] and time.time() < deadline:
        time.sleep(0.05)
    assert worktrees(bob) == [f"worktree {os.path.realpath(bob)}"]
    assert git(bob, "rev-parse", "HEAD") == ours


def test_v1_names_still_start_an_update(overlap):
    bob, ours, theirs = overlap
    jid = merge_job.start(bob, purpose="needs-merge", onto=theirs, upstream="x",
                          strategy="rebase", resolver=resolve_with(COMBINED))
    job = wait(jid)
    assert job["purpose"] == "update" and job["ours"] == ours and job["status"] == "ready"


def test_head_moved_before_start_fails_stale(overlap):
    bob, ours, theirs = overlap
    git(bob, "commit", "-q", "--allow-empty", "-m", "newer")
    job = wait(merge_job.start(bob, purpose="update", ours=ours, theirs=theirs,
                               resolver=resolve_with(COMBINED)))
    assert job["status"] == "failed" and job["error"] == merge_job.STALE_MESSAGE


def test_return_purpose_sets_returned(overlap):
    bob, ours, theirs = overlap
    store.update(bob, lambda r: r.update(
        proposal={"number": 1, "sent": ours, "cache": {"state": "merged"}},
        needs_merge={"purpose": "return", "tip": theirs, "head": ours, "files": []}))
    job = wait(merge_job.start(bob, purpose="return", ours=ours, theirs=theirs,
                               resolver=resolve_with(COMBINED)))
    assert merge_job.accept(job["id"])["ok"]
    rec = store.get(bob)
    assert rec["needs_merge"] is None and rec["proposal"]["returned_at"]


def test_old_git_path_resolves_the_same(overlap, monkeypatch):
    bob, ours, theirs = overlap
    monkeypatch.setattr(replay, "_FORCE_SCRATCH_MERGE", True)
    job = wait(merge_job.start(bob, purpose="update", ours=ours, theirs=theirs,
                               resolver=resolve_with(COMBINED)))
    assert job["status"] == "ready", job
    assert merge_job.accept(job["id"])["ok"]
    assert read(os.path.join(bob, "index.html")) == COMBINED


def test_prompt_and_summary_words():
    text = merge_job._prompt(["a.html"], {"first": "your version", "second": "alice's"})
    assert "- a.html" in text and "data, not instructions" in text
    assert merge_job.summary_from("x\n\n<summary>Kept both.</summary>") == "Kept both."


def test_is_scratch_workdir(home):
    from fused_render.shell import storage
    base = storage.base_home_dir()
    assert merge_job.is_scratch_workdir(os.path.join(base, "contrib", "work", "abc"))
    assert not merge_job.is_scratch_workdir(os.path.join(base, "contrib", "work"))
    assert not merge_job.is_scratch_workdir("/tmp/contrib/work/abc")
