"""Remix against a real temp clone and FakeGitHub (contract §11): publish
makes a NEW repo (not a fork) with a Bob-authored credit commit and a new
app id, re-points origin, keeps the original and an open proposal; the
original moving is noticed and brought in by a merge job."""
from __future__ import annotations

import pytest

from _contrib_gh_world import PAGE, claude_commit, git, make_world, read


@pytest.fixture
def w(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def _r():
    from fused_render.contrib import remix
    return remix


CREDIT = "Remixed from [alice/sine](https://github.com/alice/sine) by alice."


def _publish(w, name="sine-remix"):
    return _r().publish(w.app, name=name, credit=CREDIT)


def test_publish_makes_bob_the_author_of_a_new_app(w):
    from fused_render import app_id
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    steps = []
    out = _r().publish(w.app, name="sine-remix", credit=CREDIT, step=steps.append)
    assert steps == ["create", "send"]
    assert out == {"repo": "bob/sine-remix", "url": "https://github.com/bob/sine-remix",
                   "share_url": "https://github.com/bob/sine-remix"}
    row = w.fake.state()["repos"]["bob/sine-remix"]
    assert row["fork_of"] is None and not row["private"]          # a new repo, not a fork
    head = git(w.app, "rev-parse", "HEAD")
    assert w.fake.tip("bob/sine-remix") == head
    # The credit commit: Bob's, README credit, a NEW app id.
    assert git(w.app, "log", "-1", "--format=%an %s") == "Bob Remix of alice/sine"
    assert CREDIT in read(w.app, "README.md")
    new_id = app_id.app_id(f"{w.app}/index.html")
    assert new_id and new_id != "sine-starter-0a1b2c3d" and new_id.startswith("sine-remix-")
    # Bob's Claude change is credited to him on the way out, too.
    assert git(w.app, "log", "-1", "--format=%an", "HEAD~1") == "Bob"
    # Remotes and record.
    assert git(w.app, "config", "--get", "remote.origin.url") == "https://github.com/bob/sine-remix.git"
    assert "alice/sine" in git(w.app, "config", "--get", "remote.original.url")
    assert git(w.app, "rev-parse", "refs/remotes/origin/main") == head
    rec = w.store.get(w.app)
    assert rec["kind"] == "own" and rec["repo"] == "bob/sine-remix"
    assert rec["classified_by"] == "remix"
    assert rec["remix_of"] == {"repo": "alice/sine", "branch": "main"}
    assert [e["kind"] for e in rec["events"]][-1] == "remixed"


def test_publish_keeps_an_open_proposal(w):
    from fused_render.contrib import proposals
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    proposals.send(w.app, title="Red", description="d")
    _publish(w)
    p = w.store.get(w.app)["proposal"]
    assert p["number"] == 1 and w.gh.get_pr("alice/sine", 1)["state"] == "open"
    assert proposals.refresh(w.app, force=True)["state"] == "waiting"
    view = proposals.view(w.app)
    assert view["can_update"] is False and view["can_withdraw"] is True
    from fused_render.contrib.jobs import PlainError
    with pytest.raises(PlainError, match="remix"):
        proposals.update(w.app)
    assert proposals.withdraw(w.app) == {"ok": True}
    assert w.gh.get_pr("alice/sine", 1)["state"] == "closed"
    assert w.store.get(w.app)["proposal"] is None


def test_name_taken_by_something_else_refuses(w):
    from fused_render.contrib.jobs import PlainError
    w.fake.create_repo_as("bob", "sine-remix", {"x": "y\n"})
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    head = git(w.app, "rev-parse", "HEAD")
    with pytest.raises(PlainError, match="Pick another name"):
        _publish(w)
    assert git(w.app, "rev-parse", "HEAD") == head
    assert w.store.get(w.app)["kind"] == "github"


def test_a_failed_push_retries_into_the_same_repo(w, monkeypatch):
    from fused_render.contrib import gh
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    real = gh.push

    def broken(*a, **k):
        raise gh.GitHubError("offline")

    monkeypatch.setattr(gh, "push", broken)
    with pytest.raises(gh.GitHubError):
        _publish(w)
    assert w.store.get(w.app)["kind"] == "github"                 # nothing switched
    monkeypatch.setattr(gh, "push", real)
    _publish(w)
    assert w.store.get(w.app)["repo"] == "bob/sine-remix"
    subjects = git(w.app, "log", "--format=%s").splitlines()
    assert subjects.count("Remix of alice/sine") == 1             # the commit was reused


def test_republish_never_overwrites_what_others_added(w):
    from fused_render.contrib.jobs import PlainError
    # An earlier remix of the same name (same owner, our description) that
    # has since taken someone's merged proposal.
    w.fake.create_repo_as("bob", "sine-remix", {"index.html": PAGE})
    with w.fake._txn() as st:
        st["repos"]["bob/sine-remix"]["description"] = _r().description_for("alice/sine")
    theirs = w.fake.commit("bob/sine-remix", "main", {"carol.txt": "hi\n"},
                           "Carol's proposal", author="carol")
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    with pytest.raises(PlainError, match="already has other changes"):
        _publish(w)
    assert w.fake.tip("bob/sine-remix") == theirs
    assert w.store.get(w.app)["kind"] == "github"


def test_publish_push_is_leased(w, monkeypatch):
    from fused_render.contrib import gh
    seen = []
    real = gh.push
    monkeypatch.setattr(gh, "push", lambda *a, **k: (seen.append(k), real(*a, **k))[1])
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _publish(w)
    assert seen == [{"lease": ""}]                                # "must not exist yet"


def test_remix_of_a_private_app_is_private(w, monkeypatch):
    with w.fake._txn() as st:
        st["repos"]["alice/sine"]["private"] = True
        st["repos"]["alice/sine"]["collaborators"].append("bob")
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _publish(w)
    assert w.fake.state()["repos"]["bob/sine-remix"]["private"] is True


def test_original_update_is_noticed_and_brought_in_by_merge(w):
    from fused_render.contrib import merge_job
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _publish(w)
    assert _r().check_original(w.app) is False
    assert w.store.get(w.app)["original_update"] is None
    w.fake.commit("alice/sine", "main", {"NEWS.md": "v2\n"}, "News", author="alice")
    assert _r().check_original(w.app) is True
    upd = w.store.get(w.app)["original_update"]
    assert upd["sha"] == w.fake.tip("alice/sine")
    head = git(w.app, "rev-parse", "HEAD")
    out = _r().start_bring_in(w.app)
    assert out["ok"] and out["job"]
    import time
    deadline = time.time() + 30
    while merge_job.get(out["job"])["status"] == "running" and time.time() < deadline:
        time.sleep(0.05)
    job = merge_job.get(out["job"])
    assert job["status"] == "ready" and "No overlap" in job["summary"]
    assert git(w.app, "rev-parse", "HEAD") == head                # nothing lands unasked
    assert merge_job.accept(out["job"])["ok"]
    new = git(w.app, "rev-parse", "HEAD")
    parents = git(w.app, "rev-list", "--parents", "-n1", new).split()[1:]
    assert parents == [head, upd["sha"]]                          # a merge: history kept
    assert read(w.app, "NEWS.md") == "v2\n" and "red" in read(w.app, "index.html")
    assert w.store.get(w.app)["original_update"] is None
    assert _r().check_original(w.app) is False
    assert _r().bring_in(w.app)["message"] == "Your remix already has alice's latest."


def test_overlapping_original_goes_to_a_merge_job(w, monkeypatch):
    from fused_render.contrib import merge_job
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _publish(w)
    w.fake.commit("alice/sine", "main", {"index.html": PAGE.replace("blue", "teal")},
                  "Teal", author="alice")
    _r().check_original(w.app)
    seen = {}

    def fake_start(app_dir, **kw):
        seen.update(kw, app_dir=app_dir)
        return "job-1"

    monkeypatch.setattr(merge_job, "start", fake_start)
    assert _r().bring_in(w.app) == {"ok": True, "job": "job-1"}
    assert seen["purpose"] == "original"
    assert seen["onto"] == w.fake.tip("alice/sine")
    assert seen["upstream"] == git(w.app, "rev-parse", "HEAD")


def test_restamp_replaces_only_the_id():
    import os
    import tempfile
    from fused_render import app_id
    d = tempfile.mkdtemp()
    p = os.path.join(d, "index.html")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(PAGE)
    assert _r().restamp(p, "new-app-12345678")
    assert app_id.app_id(p) == "new-app-12345678"
    with open(p, encoding="utf-8") as fh:
        assert fh.read() == PAGE.replace("sine-starter-0a1b2c3d", "new-app-12345678")


def test_publish_needs_a_github_link_copy(w):
    from fused_render.contrib.jobs import PlainError
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _publish(w)
    with pytest.raises(PlainError, match="already your remix"):
        _publish(w, name="again")
    assert _r().start_publish(w.app, name="x", credit=CREDIT)["reason"] == "not-allowed"
