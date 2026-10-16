"""Merge with Claude jobs (contract §6.3) against real temp git repos. The
resolver seam stands in for Claude: each fake edits the conflicted files the
way a resolution would (or misbehaves the way Claude might)."""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from fused_render.contrib import merge_job


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


@pytest.fixture
def overlap(tmp_path, home):
    """Bob's copy with one change; the author's latest changes the same
    line. Returns (bob_dir, onto, upstream)."""
    bob = tmp_path / "bob"
    bob.mkdir()
    git(bob, "init", "-q", "-b", "main")
    write(bob / "index.html", "<title>A</title>\n<p>hello</p>\n")
    write(bob / "other.txt", "same\n")
    git(bob, "add", "-A")
    git(bob, "commit", "-q", "-m", "base")
    base = git(bob, "rev-parse", "HEAD")
    # The author's latest, as a side commit the copy has fetched.
    git(bob, "checkout", "-q", "-b", "author")
    write(bob / "index.html", "<title>A</title>\n<p>hello world</p>\n")
    git(bob, "commit", "-q", "-am", "author: world")
    onto = git(bob, "rev-parse", "HEAD")
    git(bob, "checkout", "-q", "main")
    git(bob, "branch", "-q", "-D", "author")
    git(bob, "update-ref", "refs/fused/follow", onto)
    write(bob / "index.html", "<title>A</title>\n<p>hello there</p>\n")
    git(bob, "commit", "-q", "-am", "Make the greeting friendlier")
    return str(bob), onto, base


def resolve_with(text, summary="Kept both greetings."):
    def resolver(wt, files, context):
        assert files == ["index.html"]
        assert "<<<<<<<" in read(os.path.join(wt, "index.html"))
        assert context["intent"].startswith("Make the greeting friendlier")
        write(os.path.join(wt, "index.html"), text)
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


def test_accept_lands_both_versions_and_backs_up(overlap):
    bob, onto, base = overlap
    before = git(bob, "rev-parse", "HEAD")
    combined = "<title>A</title>\n<p>hello there, world</p>\n"
    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base,
                          resolver=resolve_with(combined))
    job = wait(jid)
    assert job["status"] == "ready", job
    assert job["summary"] == "Kept both greetings."
    assert job["files"] == ["index.html"]
    # Nothing in the real folder moved yet.
    assert git(bob, "rev-parse", "HEAD") == before
    assert "hello there" in read(os.path.join(bob, "index.html"))

    res = merge_job.accept(jid)
    assert res["ok"], res
    assert read(os.path.join(bob, "index.html")) == combined
    head = git(bob, "rev-parse", "HEAD")
    assert git(bob, "merge-base", "--is-ancestor", onto, head, check=False) == ""
    assert git(bob, "log", "-1", "--format=%s") == "Make the greeting friendlier"
    backups = git(bob, "for-each-ref", "--format=%(objectname)", "refs/fused/backup/")
    assert before in backups.split()
    assert len(worktrees(bob)) == 1  # scratch worktree gone
    assert merge_job.get(jid)["status"] == "accepted"


def test_discard_leaves_app_untouched(overlap):
    bob, onto, base = overlap
    before = git(bob, "rev-parse", "HEAD")
    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base,
                          resolver=resolve_with("<p>x</p>\n"))
    assert wait(jid)["status"] == "ready"
    assert merge_job.discard(jid)["ok"]
    assert git(bob, "rev-parse", "HEAD") == before
    assert git(bob, "status", "--porcelain") == ""
    assert len(worktrees(bob)) == 1
    assert merge_job.get(jid)["status"] == "discarded"
    assert merge_job.accept(jid)["ok"] is False


def test_accept_refuses_when_app_moved(overlap):
    bob, onto, base = overlap
    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base,
                          resolver=resolve_with("<p>x</p>\n"))
    assert wait(jid)["status"] == "ready"
    write(os.path.join(bob, "other.txt"), "bob again\n")
    git(bob, "commit", "-q", "-am", "another edit")
    moved = git(bob, "rev-parse", "HEAD")
    res = merge_job.accept(jid)
    assert res == {"ok": False, "reason": "stale", "message": merge_job.STALE_MESSAGE}
    assert git(bob, "rev-parse", "HEAD") == moved
    assert read(os.path.join(bob, "other.txt")) == "bob again\n"


def test_one_job_per_app(overlap):
    bob, onto, base = overlap
    gate = {"go": False}

    def slow(wt, files, context):
        while not gate["go"]:
            time.sleep(0.02)
        write(os.path.join(wt, "index.html"), "<p>x</p>\n")
        return {"ok": True, "summary": "s"}

    a = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base, resolver=slow)
    b = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base, resolver=slow)
    assert a == b
    assert merge_job.current(bob)["status"] == "running"
    gate["go"] = True
    assert wait(a)["status"] == "ready"
    assert merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base) == a


def test_markers_left_are_capped_and_fail(overlap, monkeypatch):
    bob, onto, base = overlap
    monkeypatch.setattr(merge_job, "MAX_ROUNDS", 2)
    calls = []

    def lazy(wt, files, context):
        calls.append(files)
        return {"ok": True, "summary": "did nothing"}

    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base, resolver=lazy)
    job = wait(jid)
    assert job["status"] == "failed"
    assert job["error"] == merge_job.FAILED_MESSAGE
    assert len(calls) == 2
    assert len(worktrees(bob)) == 1


def test_edits_outside_the_list_are_put_back(overlap):
    bob, onto, base = overlap

    def sprawling(wt, files, context):
        write(os.path.join(wt, "index.html"), "<p>both</p>\n")
        write(os.path.join(wt, "other.txt"), "sneaky\n")
        write(os.path.join(wt, "new.py"), "import os\n")
        return {"ok": True, "summary": "s"}

    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base,
                          resolver=sprawling)
    assert wait(jid)["status"] == "ready"
    assert merge_job.accept(jid)["ok"]
    assert read(os.path.join(bob, "other.txt")) == "same\n"
    assert not os.path.exists(os.path.join(bob, "new.py"))
    assert read(os.path.join(bob, "index.html")) == "<p>both</p>\n"


def test_resolver_that_aborts_fails_cleanly(overlap):
    bob, onto, base = overlap
    before = git(bob, "rev-parse", "HEAD")

    def aborter(wt, files, context):
        git(wt, "rebase", "--abort")
        return {"ok": True, "summary": "s"}

    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base,
                          resolver=aborter)
    assert wait(jid)["status"] == "failed"
    assert git(bob, "rev-parse", "HEAD") == before


def test_resolver_that_continues_itself_is_verified(overlap):
    """Claude ran git anyway and committed a file that still holds markers:
    the final check refuses the result."""
    bob, onto, base = overlap

    def cheater(wt, files, context):
        git(wt, "add", "-A")
        git(wt, "-c", "core.editor=true", "rebase", "--continue")
        return {"ok": True, "summary": "s"}

    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base,
                          resolver=cheater)
    assert wait(jid)["status"] == "failed"


def test_two_overlapping_steps_take_two_rounds(tmp_path, home):
    bob = tmp_path / "bob2"
    bob.mkdir()
    git(bob, "init", "-q", "-b", "main")
    write(bob / "a.txt", "a\n")
    write(bob / "b.txt", "b\n")
    git(bob, "add", "-A")
    git(bob, "commit", "-q", "-m", "base")
    base = git(bob, "rev-parse", "HEAD")
    git(bob, "checkout", "-q", "-b", "author")
    write(bob / "a.txt", "a-author\n")
    write(bob / "b.txt", "b-author\n")
    git(bob, "commit", "-q", "-am", "author")
    onto = git(bob, "rev-parse", "HEAD")
    git(bob, "checkout", "-q", "main")
    write(bob / "a.txt", "a-bob\n")
    git(bob, "commit", "-q", "-am", "bob a")
    write(bob / "b.txt", "b-bob\n")
    git(bob, "commit", "-q", "-am", "bob b")
    seen = []

    def resolver(wt, files, context):
        seen.append(list(files))
        for f in files:
            write(os.path.join(wt, f), f"{f}: both\n")
        return {"ok": True, "summary": f"Combined {files[0]}."}

    jid = merge_job.start(str(bob), purpose="needs-merge", onto=onto, upstream=base,
                          resolver=resolver)
    job = wait(jid)
    assert job["status"] == "ready", job
    assert seen == [["a.txt"], ["b.txt"]]
    assert job["files"] == ["a.txt", "b.txt"]
    assert job["summary"] == "Combined a.txt. Combined b.txt."
    assert merge_job.accept(jid)["ok"]
    assert read(bob / "a.txt") == "a.txt: both\n"
    assert git(bob, "rev-list", "--count", f"{onto}..HEAD") == "2"


def test_clean_replay_is_ready_without_claude(tmp_path, home):
    bob = tmp_path / "bob3"
    bob.mkdir()
    git(bob, "init", "-q", "-b", "main")
    write(bob / "a.txt", "a\n")
    git(bob, "add", "-A")
    git(bob, "commit", "-q", "-m", "base")
    base = git(bob, "rev-parse", "HEAD")
    git(bob, "checkout", "-q", "-b", "author")
    write(bob / "z.txt", "z\n")
    git(bob, "add", "-A")
    git(bob, "commit", "-q", "-m", "author")
    onto = git(bob, "rev-parse", "HEAD")
    git(bob, "checkout", "-q", "main")
    write(bob / "a.txt", "a-bob\n")
    git(bob, "commit", "-q", "-am", "bob")

    def never(*a):
        raise AssertionError("no resolver for a clean replay")

    jid = merge_job.start(str(bob), purpose="needs-merge", onto=onto, upstream=base,
                          resolver=never)
    job = wait(jid)
    assert job["status"] == "ready" and job["files"] == []
    assert merge_job.accept(jid)["ok"]
    assert os.path.exists(bob / "z.txt") and read(bob / "a.txt") == "a-bob\n"


def test_merge_variant_for_a_remix(tmp_path, home):
    """Purpose "original": a MERGE (published history kept), ours = the remix."""
    remix = tmp_path / "remix"
    remix.mkdir()
    git(remix, "init", "-q", "-b", "main")
    write(remix / "a.txt", "one\n")
    git(remix, "add", "-A")
    git(remix, "commit", "-q", "-m", "base")
    git(remix, "checkout", "-q", "-b", "orig")
    write(remix / "a.txt", "one-original\n")
    git(remix, "commit", "-q", "-am", "original moved")
    original = git(remix, "rev-parse", "HEAD")
    git(remix, "checkout", "-q", "main")
    git(remix, "branch", "-q", "-D", "orig")
    write(remix / "a.txt", "one-remix\n")
    git(remix, "commit", "-q", "-am", "remix edit")
    mine = git(remix, "rev-parse", "HEAD")

    def resolver(wt, files, context):
        assert context["first"] == "your version of the app"
        write(os.path.join(wt, "a.txt"), "one-remix-and-original\n")
        return {"ok": True, "summary": "Brought in the update."}

    jid = merge_job.start(str(remix), purpose="original", onto=original, upstream=original,
                          resolver=resolver)
    job = wait(jid)
    assert job["status"] == "ready", job
    assert merge_job.accept(jid)["ok"]
    parents = git(remix, "log", "-1", "--format=%P").split()
    assert parents == [mine, original]
    assert read(remix / "a.txt") == "one-remix-and-original\n"


def test_merge_variant_clean_still_asks(tmp_path, home):
    remix = tmp_path / "remix2"
    remix.mkdir()
    git(remix, "init", "-q", "-b", "main")
    write(remix / "a.txt", "one\n")
    git(remix, "add", "-A")
    git(remix, "commit", "-q", "-m", "base")
    git(remix, "checkout", "-q", "-b", "orig")
    write(remix / "b.txt", "new\n")
    git(remix, "add", "-A")
    git(remix, "commit", "-q", "-m", "original moved")
    original = git(remix, "rev-parse", "HEAD")
    git(remix, "checkout", "-q", "main")
    before = git(remix, "rev-parse", "HEAD")
    jid = merge_job.start(str(remix), purpose="original", onto=original, upstream=original,
                          resolver=lambda *a: pytest.fail("no resolver needed"))
    job = wait(jid)
    assert job["status"] == "ready" and job["summary"] == merge_job.CLEAN_MERGE_SUMMARY
    assert git(remix, "rev-parse", "HEAD") == before
    assert merge_job.accept(jid)["ok"]
    assert os.path.exists(remix / "b.txt")


def test_orphan_sweep_respects_age_and_ownership(tmp_path, home, overlap):
    bob, onto, base = overlap
    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base,
                          resolver=resolve_with("<p>x</p>\n"))
    assert wait(jid)["status"] == "ready"
    work = os.path.join(str(home), "contrib", "work")
    orphan = os.path.join(work, "deadbeef")
    git(bob, "worktree", "add", "--detach", "-q", orphan, "HEAD")
    young = os.path.join(work, "young")
    os.makedirs(young)
    old = time.time() - merge_job.ORPHAN_AGE_S - 10
    owned = os.path.join(work, jid)
    for p in (orphan, owned):
        os.utime(p, (old, old))
    assert merge_job._sweep_orphans() == 1
    assert not os.path.exists(orphan)
    assert os.path.isdir(owned) and os.path.isdir(young)
    assert all(orphan not in w for w in worktrees(bob))
    assert merge_job.accept(jid)["ok"]


def test_summary_from_reply():
    text = "I edited it.\n\n<summary>Kept **both** lines. Push alerts still work.</summary>"
    assert merge_job.summary_from(text) == "Kept both lines. Push alerts still work."
    assert merge_job.summary_from("first\n\nlast para") == "last para"


def test_prompt_names_files_sides_and_rules():
    p = merge_job._prompt(["a.txt"], {"first": "alice's latest version",
                                      "second": "your change", "intent": "Add a button"})
    assert "- a.txt" in p and "alice's latest version" in p
    assert "Do not run git" in p and "<summary>" in p and "Add a button" in p


def test_landing_job_is_still_open(overlap, monkeypatch):
    bob, onto, base = overlap
    monkeypatch.setattr(merge_job, "_run", lambda job, resolver: None)
    first = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base)
    with merge_job._lock:
        merge_job._jobs[first]["status"] = "landing"
    assert merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base) == first
    assert merge_job.current(bob)["id"] == first and merge_job.for_app(bob)["id"] == first


def test_failed_job_shows_only_while_the_merge_is_needed(overlap, monkeypatch):
    from fused_render.contrib import store

    bob, onto, base = overlap
    store.put(bob, {**store.new_record("github", repo="alice/x", branch="main"),
                    "needs_merge": {"onto": onto, "since": 1.0}})
    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base,
                          resolver=lambda *a: {"ok": False, "error": "nope"})
    assert wait(jid)["status"] == "failed"
    assert merge_job.current(bob)["id"] == jid
    store.update(bob, lambda r: r.__setitem__("needs_merge", None))
    assert merge_job.current(bob) is None
    # current() prunes too: a day-old failure is dropped from the registry.
    with merge_job._lock:
        merge_job._jobs[jid]["ended_at"] -= merge_job.EXPIRE_S + 1
    merge_job.current(bob)
    assert jid not in merge_job._jobs


def test_merge_sessions_are_not_tasks(home, monkeypatch):
    from fused_render import tasks_store
    from fused_render.server.routers import tasks

    work = os.path.join(str(home), "contrib", "work", "abc123")
    assert merge_job.is_scratch_workdir(work)
    assert merge_job.is_scratch_workdir(
        os.path.join(str(home), "branches", "feat", "contrib", "work", "x"))
    assert not merge_job.is_scratch_workdir(os.path.join(str(home), "contrib", "work"))
    assert not merge_job.is_scratch_workdir("/Users/a/projects/contrib/work/x")
    assert not merge_job.is_scratch_workdir(None)
    heads = {"/t/merge.jsonl": work, "/t/real.jsonl": "/Users/a/Fused/app/x"}
    monkeypatch.setattr(tasks_store, "head",
                        lambda path, size=None: (heads[path], None, "", "", None))
    assert tasks._merge_session({"path": "/t/merge.jsonl"}) is True
    assert tasks._merge_session({"path": "/t/real.jsonl"}) is False
    assert tasks._merge_session({"path": None}) is False


def test_resolver_that_drops_a_change_fails(overlap):
    """Claude skipping the user's step leaves `onto` with no markers: the
    count check is what refuses that as "combined"."""
    bob, onto, base = overlap

    def skipper(wt, files, context):
        git(wt, "-c", "core.editor=true", "rebase", "--skip")
        return {"ok": True, "summary": "Combined everything."}

    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base,
                          resolver=skipper)
    assert wait(jid)["status"] == "failed"


def test_unsaved_overlapping_edit_defers_accept(overlap):
    bob, onto, base = overlap
    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base,
                          resolver=resolve_with("<p>both</p>\n"))
    assert wait(jid)["status"] == "ready"
    write(os.path.join(bob, "index.html"), "<p>typing...</p>\n")  # not saved as a change
    res = merge_job.accept(jid)
    assert res["ok"] is False and res["reason"] == "busy"
    assert read(os.path.join(bob, "index.html")) == "<p>typing...</p>\n"
    assert merge_job.get(jid)["status"] == "ready"
    assert merge_job.for_app(bob)["id"] == jid
    git(bob, "checkout", "--", "index.html")
    assert merge_job.accept(jid)["ok"]
    assert merge_job.for_app(bob) is None


def test_public_shape(overlap):
    bob, onto, base = overlap
    jid = merge_job.start(bob, purpose="needs-merge", onto=onto, upstream=base,
                          resolver=resolve_with("<p>x</p>\n"))
    job = wait(jid)
    assert set(job) == {"id", "app_dir", "status", "summary", "files", "error",
                        "purpose", "started_at"}
    assert job["app_dir"] == os.path.realpath(bob) and job["started_at"] <= time.time()


def test_merge_variant_clean_builds_commit_without_worktree(tmp_path, home):
    remix = tmp_path / "remix3"
    remix.mkdir()
    git(remix, "init", "-q", "-b", "main")
    write(remix / "a.txt", "one\n")
    git(remix, "add", "-A")
    git(remix, "commit", "-q", "-m", "base")
    git(remix, "checkout", "-q", "-b", "orig")
    write(remix / "b.txt", "new\n")
    git(remix, "add", "-A")
    git(remix, "commit", "-q", "-m", "original moved")
    original = git(remix, "rev-parse", "HEAD")
    git(remix, "checkout", "-q", "main")
    write(remix / "a.txt", "mine\n")
    git(remix, "commit", "-q", "-am", "remix edit")
    mine = git(remix, "rev-parse", "HEAD")
    jid = merge_job.start(str(remix), purpose="original", onto=original, upstream=mine,
                          resolver=lambda *a: pytest.fail("no resolver needed"))
    job = wait(jid)
    assert job["summary"] == merge_job.CLEAN_MERGE_SUMMARY
    assert len(worktrees(remix)) == 1
    assert merge_job.accept(jid)["ok"]
    assert git(remix, "log", "-1", "--format=%P").split() == [mine, original]
    assert git(remix, "log", "-1", "--format=%s") == merge_job.MERGE_MESSAGE
    assert read(remix / "a.txt") == "mine\n" and read(remix / "b.txt") == "new\n"


def test_own_sync_accept_schedules_a_send(overlap, monkeypatch):
    from fused_render import git_upstream

    bob, onto, base = overlap
    sent = []
    monkeypatch.setattr(git_upstream, "schedule_sync",
                        lambda path, action, *, push=True: sent.append((path, push)))
    jid = merge_job.start(bob, purpose="own-sync", onto=onto, upstream=base,
                          resolver=resolve_with("<p>x</p>\n"))
    assert wait(jid)["status"] == "ready"
    assert merge_job.accept(jid)["ok"]
    assert sent == [(os.path.realpath(bob), True)]
