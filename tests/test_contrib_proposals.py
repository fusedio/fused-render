"""Proposals against a real temp clone and FakeGitHub (contract §8, §14):
credit rewrite, send/update/withdraw, Alice editing the proposal on GitHub,
lost-record adoption, the status refresh and the A4 return trigger, plus
gh.py's error mapping and contributors."""
from __future__ import annotations

import os
import subprocess


import pytest

from _contrib_gh_world import (APP_PY, PAGE, claude_commit, edit, git, spy_engine,
                               make_world, read)


@pytest.fixture
def w(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def _p():
    from fused_render.contrib import proposals
    return proposals


def _send(w, note=""):
    return _p().send(w.app, title="Make the wave red", description="Red is nicer.", note=note)


def _proposal(w):
    return w.store.get(w.app)["proposal"]


# ------------------------------------------------------------------ send


def test_send_credits_bob_and_claude_and_changes_no_file(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "make the wave red")
    tree_before = git(w.app, "rev-parse", "HEAD^{tree}")
    view = _send(w, note="Hope you like it")
    assert view["state"] == "waiting" and view["number"] == 1
    p = _proposal(w)
    assert p["head_repo"] == "bob/sine" and p["head_branch"].startswith("fused-sine-")
    # Same files, new commit: Bob authored, Claude credited exactly once.
    head = git(w.app, "rev-parse", "HEAD")
    assert git(w.app, "rev-parse", "HEAD^{tree}") == tree_before
    assert head == p["proposed_head"] == p["pushed_head"]
    assert git(w.app, "log", "-1", "--format=%an <%ae>") == \
        f"Bob <{w.fake.email_of('bob')}>"
    body = git(w.app, "log", "-1", "--format=%B")
    assert body.count("Co-authored-by: Claude <noreply@anthropic.com>") == 1
    # The old tip is pinned (nothing of Bob's lost silently).
    assert git(w.app, "for-each-ref", "refs/fused/backup/")
    # On GitHub: the PR from Bob's own copy, exactly that commit, footer + note.
    pr = w.gh.get_pr("alice/sine", 1)
    assert pr["head_sha"] == head and pr["user"] == "bob"
    assert pr["body"].endswith(w.gh.PROPOSAL_MARKER)
    assert "**Note from bob:** Hope you like it" in pr["body"]
    assert w.fake.tip("bob/sine", p["head_branch"]) == head


def test_send_includes_uncommitted_edits(w):
    claude_commit(w.app, {"app.py": "def amplitude():\n    return 5\n"}, "louder")
    edit(w.app, "index.html", PAGE.replace("blue", "green"))
    edit(w.app, "helper.py", "HELP = 1\n")                     # made by hand, untracked
    edit(w.app, ".gitignore", "out.csv\n")
    edit(w.app, "out.csv", "ignored\n")
    os.makedirs(os.path.join(w.app, "__pycache__"), exist_ok=True)
    edit(w.app, "__pycache__/app.cpython-312.pyc", "bytes")
    os.makedirs(os.path.join(w.app, ".fused"), exist_ok=True)
    edit(w.app, ".fused/meta.json", "{}")
    _send(w)
    p = _proposal(w)
    assert not _p().dirty(w.app)
    ref = f"refs/heads/{p['head_branch']}"
    assert "green" in w.fake.read("bob/sine", "index.html", ref)
    assert w.fake.read("bob/sine", "helper.py", ref) == "HELP = 1\n"
    for never in ("out.csv", "__pycache__/app.cpython-312.pyc", ".fused/meta.json"):
        assert w.fake.read("bob/sine", never, ref) is None, never
    assert git(w.app, "log", "-1", "--format=%s") == "Your edits"


def test_start_send_reports_steps_and_result(w, monkeypatch):
    from fused_render.contrib import jobs
    seen = []
    real = jobs.set_step
    monkeypatch.setattr(jobs, "set_step", lambda jid, key: (seen.append(key), real(jid, key)))
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    out = _p().start_send(w.app, title="Red", description="d")
    job = jobs.wait(out["job"])
    assert job["status"] == "done", job
    assert seen == ["copy", "send", "open"]
    assert job["result"]["number"] == 1 and job["result"]["url"]


def test_signed_out_job_carries_the_reason(w):
    from fused_render.contrib import jobs
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    job_id = _p().start_job("t", w.app, lambda step: (w.fake.sign_out(), _send(w)))
    job = jobs.wait(job_id)
    assert job["status"] == "failed" and job["reason"] == "signed-out"


def test_send_with_nothing_changed_refuses(w):
    from fused_render.contrib.jobs import PlainError
    with pytest.raises(PlainError, match="nothing to send"):
        _send(w)
    assert w.fake.state()["pulls"] == {}


def test_one_open_proposal_per_app(w):
    from fused_render.contrib.jobs import PlainError
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    claude_commit(w.app, {"app.py": "x = 2\n"}, "more")
    with pytest.raises(PlainError, match="Update proposal"):
        _send(w)


def test_lost_record_adopts_the_open_proposal(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.store.update(w.app, lambda r: r.update(proposal=None))
    claude_commit(w.app, {"app.py": "x = 2\n"}, "more")
    view = _send(w)
    assert view["number"] == 1
    assert len(w.fake.state()["pulls"]["alice/sine"]) == 1
    assert w.gh.get_pr("alice/sine", 1)["head_sha"] == git(w.app, "rev-parse", "HEAD")


def test_a_second_copy_never_adopts_the_first_copys_proposal(w, tmp_path):
    from fused_render.contrib import follow, replay
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    # Copy 2 of the same app (same folder name), same machine, same account.
    (tmp_path / "apps2").mkdir()
    two = str(tmp_path / "apps2" / "sine")
    git(tmp_path / "apps2", "clone", "-q", "https://github.com/alice/sine", two)
    rec = w.store.new_record("github", repo="alice/sine", branch="main")
    w.store.put(two, rec)
    two = w.store.key(two)
    follow.fetch(two, follow.for_record(rec))
    git(two, "update-ref", replay.BASE_REF, git(two, "rev-parse", "refs/fused/follow"))
    claude_commit(two, {"app.py": APP_PY_LOUD}, "louder")
    view = _p().send(two, title="Louder", description="d")
    assert view["number"] == 2
    assert w.gh.get_pr("alice/sine", 1)["head_sha"] == _proposal(w)["pushed_head"]


def test_branch_names_carry_a_random_tail(w):
    import re
    a, b = _p()._branch_name(w.app), _p()._branch_name(w.app)
    assert re.fullmatch(r"fused-sine-\d{8}-\d{4}-[0-9a-f]{4}", a) and a != b


@pytest.mark.parametrize("key,value", [
    ("url.https://evil.example/.insteadOf", "https://github.com/"),
    ("url.https://evil.example/.pushInsteadOf", "git@github.com:"),
])
def test_a_local_url_rewrite_onto_github_refuses_transfers(w, key, value):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    git(w.app, "config", key, value)
    sha = git(w.app, "rev-parse", "HEAD")
    with pytest.raises(w.gh.GitHubError) as e:
        w.gh.push(w.app, sha, "bob/sine", "x", lease="")
    assert e.value.reason == "forbidden" and "nothing was sent" in e.value.message
    assert w.gh.fetch(w.app, "alice/sine", "+refs/heads/main:refs/fused/x") is False
    assert w.gh.url_rewrites(w.app)


@pytest.mark.parametrize("setup,flagged", [
    # worktree-scoped config (extensions.worktreeConfig)
    ([("extensions.worktreeConfig", "true"),
      ("--worktree", "url.https://evil.example/.insteadOf", "https://github.com/")], True),
    # anything credential.* — git hands helpers the token on `store`
    ([("credential.https://github.com.helper", "!evil")], True),
    ([("credential.helper", "store")], True),
    # URL-scoped http settings outrank our command-line generic ones
    ([("http.https://github.com/.proxy", "http://evil:8080")], True),
    ([("http.https://github.com/.sslVerify", "false")], True),
    # generic deny-listed http settings
    ([("http.sslCAInfo", "/tmp/evil-ca.pem")], True),
    ([("http.extraHeader", "X-Evil: 1")], True),
    # generic proxy / sslVerify are overridden on the command line, not refused
    ([("http.proxy", "http://127.0.0.1:9")], False),
    ([("http.sslVerify", "false")], False),
    ([("http.https://example.com/.proxy", "http://x:1")], False),
])
def test_repo_config_that_could_take_the_transfer_is_refused(w, setup, flagged):
    for args in setup:
        git(w.app, "config", *args)
    assert bool(w.gh.url_rewrites(w.app, "https://github.com/alice/sine.git")) is flagged


def test_a_rewrite_pulled_in_by_an_include_is_seen(w, tmp_path):
    extra = tmp_path / "extra.cfg"
    extra.write_text('[url "https://evil.example/"]\n\tinsteadOf = https://github.com/\n')
    git(w.app, "config", "include.path", str(extra))
    assert w.gh.url_rewrites(w.app)


@pytest.mark.parametrize("broken", ["no-run", "rc"])
def test_an_unreadable_config_fails_closed(w, monkeypatch, broken):
    """A config read that fails is not a clean config: url_rewrites raises,
    push/fetch refuse, and the follow guard reads it as redirected."""
    from fused_render.contrib import follow
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    sha = git(w.app, "rev-parse", "HEAD")
    real = w.gh._git

    def flaky(repo, *args, **kw):
        if args[:2] == ("config", "--list"):
            if broken == "no-run":
                return None
            return subprocess.CompletedProcess(args, 128, "", "fatal: bad config line 3")
        return real(repo, *args, **kw)

    monkeypatch.setattr(w.gh, "_git", flaky)
    with pytest.raises(w.gh.GitHubError) as e:
        w.gh.url_rewrites(w.app)
    assert e.value.reason == "failed"
    with pytest.raises(w.gh.GitHubError):
        w.gh.push(w.app, sha, "bob/sine", "x", lease="")
    assert w.gh.fetch(w.app, "alice/sine", "+refs/heads/main:refs/fused/x") is False
    assert follow.github_redirected(w.app, "https://github.com/alice/sine") is True


def test_net_args_pin_tls_and_the_environment_proxy(monkeypatch):
    from fused_render import github_setup
    from fused_render.contrib import gh
    monkeypatch.delenv(gh.FAKE_ENV, raising=False)
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    args = gh.git_net_args()
    for kv in ("http.sslVerify=true", "http.https://github.com/.sslVerify=true",
               "http.proxy=", "http.https://github.com/.proxy="):
        assert kv in args, kv
    monkeypatch.setenv("https_proxy", "http://corp:3128")
    args = gh.git_net_args()
    assert "http.proxy=http://corp:3128" in args
    assert "http.https://github.com/.proxy=http://corp:3128" in args


def test_a_rewrite_of_the_exact_transfer_url_refuses_too(w):
    url = w.gh.clone_url("alice/sine")             # the fake's bare path
    git(w.app, "config", "url./tmp/elsewhere/.insteadOf", url[:-8])
    assert w.gh.url_rewrites(w.app, url)
    assert w.gh.fetch(w.app, "alice/sine", "+refs/heads/main:refs/fused/x") is False


def test_signed_out_is_reason_signed_out(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    w.fake.sign_out()
    assert _p().preflight(w.app)["reason"] == "signed-out"
    with pytest.raises(w.gh.GitHubError) as e:
        _send(w)
    assert e.value.reason == "signed-out"
    assert "git" not in e.value.message.lower().split()


def test_offline_identity_is_cached_for_gated_posts_but_not_for_strict(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    w.gh.forget_identity()
    w.fake.fail_next("user", "offline", times=5)

    def asks():
        return w.fake.state()["calls"].count("user")

    assert _p().preflight(w.app)["reason"] == "offline"
    n = asks()
    assert _p().preflight(w.app)["reason"] == "offline"         # from cache
    assert w.gh.identity() is None and asks() == n
    with pytest.raises(w.gh.GitHubError):
        w.gh.identity(strict=True)                               # re-asks
    assert asks() == n + 1


def test_withdraw_waits_for_a_send_in_flight(w):
    import threading
    from fused_render.contrib import jobs
    from fused_render.contrib.jobs import PlainError
    gate = threading.Event()
    job = jobs.start("propose-send", w.app, lambda: gate.wait(10) and {})
    try:
        with pytest.raises(PlainError, match="still being sent") as e:
            _p().withdraw(w.app)
        assert e.value.reason == "busy"
    finally:
        gate.set()
        jobs.wait(job)
    assert _p().withdraw(w.app) == {"ok": True}


def test_disabled_wording(w, monkeypatch):
    from fused_render.contrib import flags, remix
    monkeypatch.setattr(flags, "_raw", lambda: dict.fromkeys(
        ("sources", "changes", "inbox", "loop", "remix"), False))
    assert _p().preflight(w.app)["message"] == "This isn't turned on."
    assert remix.start_draft(w.app)["message"] == "This isn't turned on."


def test_fork_name_collision_uses_the_name_github_picked(w):
    w.fake.create_repo_as("bob", "sine", {"README.md": "mine\n"})
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    assert _proposal(w)["head_repo"] == "bob/sine-1"


def test_fork_creation_is_polled(w):
    w.fake.delay_fork(3)
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    assert _proposal(w)["head_repo"] == "bob/sine"


# ---------------------------------------------------------------- update


def test_update_sends_new_edits_and_is_idempotent(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    first = _proposal(w)["pushed_head"]
    assert _p().view(w.app)["unproposed"] == 0
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    assert _p().view(w.app)["unproposed"] == 1
    _p().update(w.app)
    p = _proposal(w)
    assert p["pushed_head"] != first and p["number"] == 1
    assert w.gh.get_pr("alice/sine", 1)["head_sha"] == p["pushed_head"]
    # The already-sent commit kept its identity (pure-function credit).
    assert git(w.app, "rev-parse", "HEAD~1") == first
    assert _p().view(w.app)["unproposed"] == 0
    again = _p().update(w.app)
    assert again["message"] == "Your proposal is already up to date."


APP_PY_LOUD = "def amplitude():\n    return 9\n\n\ndef frequency():\n    return 2\n"


def test_update_keeps_alices_edit_to_the_proposal(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    p = _proposal(w)
    # Alice fixes something on github.com, straight onto Bob's branch.
    w.fake.commit("bob/sine", p["head_branch"], {"NOTES.md": "thanks!\n"},
                  "Add notes", author="alice")
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    _p().update(w.app)
    p2 = _proposal(w)
    remote = w.gh.get_pr("alice/sine", 1)["head_sha"]
    assert remote == p2["pushed_head"] == git(w.app, "rev-parse", "HEAD")
    assert read(w.app, "NOTES.md") == "thanks!\n"          # Bob now runs her edit
    assert "return 9" in read(w.app, "app.py")             # and his own
    assert git(w.app, "log", "-1", "--format=%an") == "alice"  # her commit is still hers


def test_update_refuses_when_alices_edit_overlaps(w):
    from fused_render.contrib.jobs import PlainError
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    p = _proposal(w)
    remote = w.fake.commit("bob/sine", p["head_branch"],
                           {"app.py": APP_PY.replace("return 1", "return 7")},
                           "Tweak", author="alice")
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    tree = git(w.app, "rev-parse", "HEAD^{tree}")
    with pytest.raises(PlainError, match="alice also edited this proposal"):
        _p().update(w.app)
    assert w.gh.get_pr("alice/sine", 1)["head_sha"] == remote   # hers untouched
    assert git(w.app, "rev-parse", "HEAD^{tree}") == tree       # his files untouched
    assert not _p().dirty(w.app)
    assert "return 9" in read(w.app, "app.py")


def test_withdraw_closes_with_a_note_and_clears(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    head = git(w.app, "rev-parse", "HEAD")
    assert _p().withdraw(w.app) == {"ok": True}
    pr = w.gh.get_pr("alice/sine", 1)
    assert pr["state"] == "closed" and not pr["merged"]
    assert w.gh.pr_comments("alice/sine", 1)[-1]["body"] == "bob withdrew this proposal."
    assert _proposal(w) is None
    assert git(w.app, "rev-parse", "HEAD") == head


# --------------------------------------------------------------- refresh


def test_refresh_shows_alices_newest_comment_only(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.comment_as("alice", "alice/sine", 1, "old", at="2001-01-01T00:00:00Z")
    w.fake.comment_as("bob", "alice/sine", 1, "my own note")
    assert _p().refresh(w.app, force=True)["state"] == "waiting"
    w.fake.comment_as("alice", "alice/sine", 1, "Can you make it darker?")
    w.fake.comment_as("bob", "alice/sine", 1, "sure")
    view = _p().refresh(w.app, force=True)
    assert view["state"] == "commented"
    assert view["comment"]["author"] == "alice"
    assert view["comment"]["body"] == "Can you make it darker?"


def test_refresh_is_throttled(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    calls = len(w.fake.state()["calls"])
    _p().refresh(w.app)
    assert len(w.fake.state()["calls"]) == calls


def test_declined_keeps_changes_and_says_so_once(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    head = git(w.app, "rev-parse", "HEAD")
    w.fake.close("alice/sine", 1)
    assert _p().refresh(w.app, force=True)["state"] == "declined"
    _p().refresh(w.app, force=True)
    events = [e["kind"] for e in w.store.get(w.app)["events"]]
    assert events.count("declined") == 1
    assert git(w.app, "rev-parse", "HEAD") == head
    # Declined is settled: a fresh Send is allowed.
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    assert _send(w)["number"] == 2


def test_declined_shows_the_authors_closing_note(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.comment_as("alice", "alice/sine", 1, "Not for now, thanks!")
    w.fake.close("alice/sine", 1)
    view = _p().refresh(w.app, force=True)
    assert view["state"] == "declined"
    assert view["comment"]["author"] == "alice"
    assert view["comment"]["body"] == "Not for now, thanks!"
    assert view["can_update"] is False and view["can_withdraw"] is False


def _api_calls(w):
    return len([c for c in w.fake.state()["calls"] if c in ("get_pull", "comments")])


def test_a_declined_proposal_stops_being_polled(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.comment_as("alice", "alice/sine", 1, "No thanks.")
    w.fake.close("alice/sine", 1)
    assert _p().refresh(w.app, force=True)["comment"]["body"] == "No thanks."
    before = _api_calls(w)
    _p()._last_refresh.clear()                                  # past the throttle
    view = _p().refresh(w.app)
    assert _api_calls(w) == before and view["state"] == "declined"
    # Bob's explicit "check now" still asks GitHub — and sees a reopen.
    w.fake.reopen("alice/sine", 1)
    view = _p().refresh(w.app, force=True)
    assert view["state"] == "commented" and view["can_update"]   # open again; her note stands
    assert not _proposal(w)["cache"]["note_checked"]


def test_merged_polls_until_returned_then_stops(w, monkeypatch):
    calls = spy_engine(monkeypatch)
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.merge("alice/sine", 1, "squash")
    assert _p().refresh(w.app, force=True)["state"] == "merged"
    assert _proposal(w)["returned_at"] and len(calls) == 1
    before = _api_calls(w)
    _p().refresh(w.app, force=True)
    assert _api_calls(w) == before and len(calls) == 1


def test_stale_follows_what_bob_runs_not_the_commit_count(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    assert _p().view(w.app)["stale"] is False
    edit(w.app, "app.py", APP_PY_LOUD)                           # uncommitted
    assert _p().view(w.app)["stale"] is True
    git(w.app, "checkout", "--", "app.py")
    assert _p().view(w.app)["stale"] is False
    # What a Merge with Claude leaves: the engine carried proposed_head onto a
    # rewritten HEAD (so nothing reads as unproposed), but the files differ
    # from what GitHub has.
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "combined")
    head = git(w.app, "rev-parse", "HEAD")
    w.store.update(w.app, lambda r: r["proposal"].update(proposed_head=head))
    view = _p().view(w.app)
    assert view["unproposed"] == 0 and view["stale"] is True
    _p().update(w.app)
    assert _p().view(w.app)["stale"] is False


def test_edits_made_on_github_are_read_back(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.edit_pr("alice/sine", 1, title="Red wave (edited by Alice)")
    assert _p().refresh(w.app, force=True)["title"] == "Red wave (edited by Alice)"


@pytest.mark.parametrize("method", ["merge", "squash", "rebase"])
def test_merged_returns_to_following_keeping_unproposed_edits(w, monkeypatch, method):
    calls = spy_engine(monkeypatch)
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")      # never proposed
    w.fake.merge("alice/sine", 1, method)
    view = _p().refresh(w.app, force=True)
    assert view["state"] == "merged"
    assert [c[0] for c in calls] == [w.app] and calls[0][1]["ok"], calls
    head = git(w.app, "rev-parse", "HEAD")
    tip = git(w.app, "rev-parse", "refs/fused/follow")
    assert git(w.app, "rev-list", "--count", f"{tip}..{head}") == "1"   # only "louder" left
    assert "red" in read(w.app, "index.html") and "return 9" in read(w.app, "app.py")
    assert w.store.get(w.app)["proposal"]["returned_at"]
    # Once only.
    _p().refresh(w.app, force=True)
    assert len(calls) == 1


def test_return_overlap_waits_on_one_merge_job(w, monkeypatch):
    from fused_render.contrib import merge_job
    calls = spy_engine(monkeypatch)
    started = []
    monkeypatch.setattr(merge_job, "start",
                        lambda app_dir, **kw: started.append(kw) or "job-1")
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")      # never proposed
    w.fake.commit("alice/sine", "main", {"app.py": APP_PY.replace("return 1", "return 7")},
                  "Quieter", author="alice")
    w.fake.merge("alice/sine", 1, "squash")
    tree = git(w.app, "rev-parse", "HEAD^{tree}")
    _p().refresh(w.app, force=True)
    assert len(calls) == 1 and [s["purpose"] for s in started] == ["return"]
    assert w.store.get(w.app)["needs_merge"]
    assert git(w.app, "rev-parse", "HEAD^{tree}") == tree        # nothing moved
    _p().refresh(w.app, force=True)
    assert len(calls) == 1 and len(started) == 1                 # not asked again


def test_merged_but_not_yet_in_the_authors_latest_waits(w, monkeypatch):
    calls = spy_engine(monkeypatch)
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    head = git(w.app, "rev-parse", "HEAD")
    before = w.fake.tip("alice/sine")
    w.fake.merge("alice/sine", 1, "merge")
    # GitHub says merged, but main no longer carries it (Alice reset it).
    git(w.fake.bare_path("alice/sine"), "update-ref", "refs/heads/main", before)
    assert _p().refresh(w.app, force=True)["state"] == "merged"
    assert calls and calls[-1][1]["reason"] == "not-yet"
    assert git(w.app, "rev-parse", "HEAD") == head
    assert not w.store.get(w.app)["proposal"]["returned_at"]


def test_merged_with_loop_off_does_not_return(w, monkeypatch):
    from fused_render.contrib import flags
    calls = spy_engine(monkeypatch)
    monkeypatch.setattr(flags, "_raw", lambda: {"sources": True, "changes": True,
                                                "inbox": True, "loop": False, "remix": True})
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.merge("alice/sine", 1, "squash")
    assert _p().refresh(w.app, force=True)["state"] == "merged"
    assert calls == []


def test_github_trouble_keeps_the_cached_state(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.fail_next("get_pull", "offline")
    assert _p().refresh(w.app, force=True)["state"] == "waiting"
    assert _proposal(w)["cache"]["error"] == "offline"


# ------------------------------------------------------- credit, pure parts


def test_with_trailer_is_idempotent():
    p = _p()
    once = p.with_trailer("Claude: red\n\nbody text\n")
    assert once == p.with_trailer(once)
    assert once.count("Co-authored-by: Claude") == 1
    joined = p.with_trailer("Subject\n\nSigned-off-by: Bob <b@x>\n")
    assert joined.endswith("Signed-off-by: Bob <b@x>\nCo-authored-by: Claude <noreply@anthropic.com>\n")


def test_credit_keeps_other_peoples_commits(w):
    base = git(w.app, "rev-parse", "HEAD")
    claude_commit(w.app, {"a.txt": "1\n"}, "one")
    edit(w.app, "b.txt", "2\n")
    git(w.app, "add", "b.txt")
    git(w.app, "-c", "user.name=Carol", "-c", "user.email=carol@example.com",
        "commit", "-q", "-m", "by hand")
    me = w.gh.identity()
    tip = _p().credit(w.app, base, git(w.app, "rev-parse", "HEAD"), me)
    assert git(w.app, "log", "-1", "--format=%an", tip) == "Carol"
    assert git(w.app, "log", "-1", "--format=%an", f"{tip}~1") == "Bob"
    assert _p().credit(w.app, base, tip, me) == tip


def test_credit_keeps_a_merge_in_bobs_range_as_a_merge(w):
    base = git(w.app, "rev-parse", "HEAD")
    claude_commit(w.app, {"a.txt": "1\n"}, "one")
    git(w.app, "checkout", "-q", "-b", "side", base)
    claude_commit(w.app, {"b.txt": "2\n"}, "two")
    git(w.app, "checkout", "-q", "main")
    git(w.app, "-c", "user.name=Fused", "-c", "user.email=apps@fused.io",
        "merge", "-q", "--no-ff", "-m", "Claude: combine", "side")
    head = git(w.app, "rev-parse", "HEAD")
    me = w.gh.identity()
    tip = _p().credit(w.app, base, head, me)
    assert git(w.app, "rev-parse", f"{tip}^{{tree}}") == git(w.app, "rev-parse", f"{head}^{{tree}}")
    assert len(git(w.app, "rev-list", "--parents", "-n1", tip).split()) == 3   # still a merge
    authors = set(git(w.app, "log", "--format=%an", f"{base}..{tip}").splitlines())
    assert authors == {"Bob"}
    assert _p().credit(w.app, base, tip, me) == tip                          # idempotent


# ----------------------------------------------------------------- gh.py


@pytest.mark.parametrize("err,out,reason", [
    ("gh: Not Found (HTTP 404)", '{"message":"Not Found"}', "not-found"),
    ("gh: Bad credentials (HTTP 401)", "", "signed-out"),
    ("gh: API rate limit exceeded (HTTP 403)", '{"message":"API rate limit exceeded"}',
     "rate-limited"),
    ("gh: Resource not accessible (HTTP 403)", "", "forbidden"),
    ("gh: Validation Failed (HTTP 422)",
     '{"message":"Validation Failed","errors":[{"message":"A pull request already exists"}]}',
     "conflict"),
    ("To get started with GitHub CLI, please run:  gh auth login", "", "signed-out"),
    ('error connecting to api.github.com', "", "offline"),
    ("gh: Server Error (HTTP 502)", "", "failed"),
])
def test_gh_api_errors_are_plain(err, out, reason):
    from fused_render.contrib import gh
    exc = gh._api_failure(1, out, err)
    assert exc.reason == reason
    for word in ("gh", "http", "api", "git"):
        assert word not in exc.message.lower().split()


def test_real_push_args_force_the_gh_credential_helper(monkeypatch):
    from fused_render import github_setup
    from fused_render.contrib import gh
    monkeypatch.delenv(gh.FAKE_ENV, raising=False)
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/opt/x y/gh", "path"))
    args = gh.git_net_args()
    assert args[:2] == ["-c", "credential.helper="]
    assert "credential.https://github.com.helper=!'/opt/x y/gh' auth git-credential" in args
    env = gh.git_net_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert gh.clone_url("git@github.com:Alice/Sine.git") == "https://github.com/alice/sine.git"


def test_split_full_refuses_paths():
    from fused_render.contrib import gh
    with pytest.raises(gh.GitHubError):
        gh.split_full("../../user/repos")


# --------------------------------------------------------------- contributors


def test_contributors_after_a_merge(w):
    from fused_render.contrib import contributors, follow
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.merge("alice/sine", 1, "squash")
    rec = w.store.get(w.app)
    follow.fetch(w.app, follow.for_record(rec))
    people = contributors.list(w.app)
    logins = [x["login"] for x in people]
    assert logins == ["alice", "bob"] or sorted(logins) == ["alice", "bob"]
    assert all(x["name"] != "Fused" for x in people)


def test_contributors_parse_folds_noreply_and_drops_fused():
    from fused_render.contrib import contributors
    rows = contributors.parse("\n".join([
        "Bob\t1001+bob@users.noreply.github.com",
        "Bobby\tbob@users.noreply.github.com",
        "Fused\tapps@fused.io",
        "dependabot[bot]\t49699333+dependabot[bot]@users.noreply.github.com",
        "Carol\tcarol@example.com",
    ]))
    assert rows == [{"login": "bob", "name": "Bob", "commits": 2},
                    {"login": None, "name": "Carol", "commits": 1}]


def test_no_git_words_in_messages():
    import re
    import fused_render.contrib.proposals as m
    src = open(m.__file__, encoding="utf-8").read()
    for msg in re.findall(r'PlainError\(\s*((?:f?"[^"]*"\s*)+)', src):
        low = msg.lower()
        for word in ("branch", "rebase", "fork", "commit", "push", "conflict",
                     "origin", "head", "sha"):
            assert not re.search(rf"\b{word}\b", low), (word, msg)


