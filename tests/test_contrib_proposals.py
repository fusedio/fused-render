"""Proposals against a real temp clone and FakeGitHub (contract §8, v2
§16.5): credit once, send with ticked new files, Update as a plain push that
merges edits made on GitHub, Withdraw, the status refresh, a collaborator's
direct publish, the Claude-busy gate, and the audit repros (R1 R4 R6 R7 R8
R9 R10 R11 R13) asserting the FIXED behaviour; plus gh.py's error mapping
and contributors."""
from __future__ import annotations

import os
import subprocess

import pytest

from _contrib_gh_world import APP_PY, PAGE, claude_commit, edit, git, make_world, read


@pytest.fixture
def w(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


@pytest.fixture
def sw(tmp_path, monkeypatch):
    """The world with git_upstream's auto-sync live (the engine's v2 table
    runs for real): `sw.sync()` is what an app open does."""
    from fused_render import git_upstream
    from fused_render.contrib import classify, engine
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    for name, value in (("_checked", {}), ("_state", {}), ("_sync_failures", {}),
                        ("_pulled_events", []), ("_pending", {})):
        if hasattr(git_upstream, name):
            monkeypatch.setattr(git_upstream, name, value)
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: True)
    monkeypatch.setattr(classify, "ensure_in_background", lambda app_dir: False)
    getattr(engine, "_status_cache", {}).clear()
    world = make_world(tmp_path, monkeypatch)
    world.sync = lambda: git_upstream.sync_repo(world.app, action="test", push=False)
    return world


def _p():
    from fused_render.contrib import proposals
    return proposals


def _send(w, note="", **kw):
    return _p().send(w.app, title="Make the wave red", description="Red is nicer.", note=note,
                     **kw)


def _proposal(w):
    return w.store.get(w.app)["proposal"]


def _head(w):
    return git(w.app, "rev-parse", "HEAD")


def _tip(w):
    return git(w.app, "rev-parse", "refs/remotes/origin/main")


def _anc(w, a, b) -> bool:
    from fused_render.contrib import replay
    return replay.is_ancestor(w.app, a, b)


def _fetch(w):
    from fused_render.contrib import follow
    got = follow.fetch(w.app, follow.for_record(w.store.get(w.app)))
    return got[0] if isinstance(got, tuple) else got


def _bring_in(w):
    """What the engine's row 11 does with the author's latest: MERGE it into
    Bob's copy as Bob (never a rebase)."""
    _fetch(w)
    me = w.gh.identity()
    git(w.app, "-c", f"user.name={me['name']}", "-c", f"user.email={me['email']}",
        "merge", "-q", "--no-ff", "-m", "Bring in alice's latest", "refs/remotes/origin/main")


def _activity(w):
    return [(a["kind"], a["text"]) for a in w.store.activity(w.app)]


APP_PY_LOUD = "def amplitude():\n    return 9\n\n\ndef frequency():\n    return 2\n"


# ------------------------------------------------------------------ send


def test_send_credits_bob_and_claude_and_changes_no_file(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "make the wave red")
    old = _head(w)
    tree_before = git(w.app, "rev-parse", "HEAD^{tree}")
    view = _send(w, note="Hope you like it")
    assert view["state"] == "waiting" and view["number"] == 1
    p = _proposal(w)
    assert p["head_repo"] == "bob/sine" and p["head_branch"].startswith("fused-sine-")
    # Same files, new commit: Bob authored, Claude credited exactly once.
    head = _head(w)
    assert git(w.app, "rev-parse", "HEAD^{tree}") == tree_before
    assert head == p["sent"] and p["checked_tip"] == _tip(w)
    assert git(w.app, "log", "-1", "--format=%an <%ae>") == \
        f"Bob <{w.fake.email_of('bob')}>"
    body = git(w.app, "log", "-1", "--format=%B")
    assert body.count("Co-authored-by: Claude <noreply@anthropic.com>") == 1
    # The old tip is pinned (no Undo on the line: §16.12).
    assert old in git(w.app, "for-each-ref", "--format=%(objectname)",
                      "refs/fused/backup/").split()
    assert [k for k, _t in _activity(w)][:2] == ["proposal-sent", "proposal-credit"]
    # On GitHub: the PR from Bob's own copy, exactly that commit, footer + note.
    pr = w.gh.get_pr("alice/sine", 1)
    assert pr["head_sha"] == head and pr["user"] == "bob"
    assert pr["body"].endswith(w.gh.PROPOSAL_MARKER)
    assert "**Note from bob:** Hope you like it" in pr["body"]
    assert w.fake.tip("bob/sine", p["head_branch"]) == head


def test_send_commits_tracked_edits_and_only_the_new_files_ticked(w):
    claude_commit(w.app, {"app.py": "def amplitude():\n    return 5\n"}, "louder")
    edit(w.app, "index.html", PAGE.replace("blue", "green"))
    edit(w.app, "helper.py", "HELP = 1\n")                     # made by hand, ticked
    edit(w.app, "secret.txt", "token\n")                       # made by hand, NOT ticked
    edit(w.app, ".gitignore", "out.csv\n")
    edit(w.app, "out.csv", "ignored\n")
    os.makedirs(os.path.join(w.app, "__pycache__"), exist_ok=True)
    edit(w.app, "__pycache__/app.cpython-312.pyc", "bytes")
    os.makedirs(os.path.join(w.app, ".fused"), exist_ok=True)
    edit(w.app, ".fused/meta.json", "{}")
    offered = _p().untracked(w.app)
    assert "helper.py" in offered and "secret.txt" in offered
    for never in ("out.csv", "__pycache__/app.cpython-312.pyc", ".fused/meta.json"):
        assert never not in offered
    _send(w, include=["helper.py"])
    p = _proposal(w)
    ref = f"refs/heads/{p['head_branch']}"
    assert "green" in w.fake.read("bob/sine", "index.html", ref)
    assert w.fake.read("bob/sine", "helper.py", ref) == "HELP = 1\n"
    for never in ("secret.txt", "out.csv", "__pycache__/app.cpython-312.pyc", ".fused/meta.json"):
        assert w.fake.read("bob/sine", never, ref) is None, never
    assert git(w.app, "log", "-1", "--format=%s") == "Your edits"
    assert _p().untracked(w.app) == [".gitignore", "secret.txt"]   # still here, still his


@pytest.mark.parametrize("bad", [["index.html"], ["../elsewhere.py"], ["nope.py"], "helper.py"])
def test_include_must_name_a_new_file_here(w, bad):
    from fused_render.contrib.jobs import PlainError
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    edit(w.app, "helper.py", "HELP = 1\n")
    with pytest.raises(PlainError):
        _send(w, include=bad)
    assert w.fake.state()["pulls"] == {}


def test_start_send_reports_steps_and_result(w, monkeypatch):
    from fused_render.contrib import jobs
    seen = []
    real = jobs.set_step
    monkeypatch.setattr(jobs, "set_step", lambda jid, key: (seen.append(key), real(jid, key)))
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    out = _p().start_send(w.app, title="Red", description="d", include=[])
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
    edit(w.app, "scratch.txt", "only an unticked new file\n")
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
    first = _head(w)
    w.store.update(w.app, lambda r: r.update(proposal=None))
    claude_commit(w.app, {"app.py": "x = 2\n"}, "more")
    view = _send(w)
    assert view["number"] == 1
    assert len(w.fake.state()["pulls"]["alice/sine"]) == 1
    pr_head = w.gh.get_pr("alice/sine", 1)["head_sha"]
    assert pr_head == _head(w) and _anc(w, first, pr_head)        # added to, not replaced


def test_a_second_copy_never_adopts_the_first_copys_proposal(w, tmp_path):
    from fused_render.contrib import follow
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
    claude_commit(two, {"app.py": APP_PY_LOUD}, "louder")
    view = _p().send(two, title="Louder", description="d")
    assert view["number"] == 2
    assert w.gh.get_pr("alice/sine", 1)["head_sha"] == _proposal(w)["sent"]


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
    sha = _head(w)
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
    sha = _head(w)
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


# --------------------------------------------------------- Claude busy (R8)


def test_r8_nothing_moves_or_leaves_while_claude_works(w, monkeypatch):
    """R8 fixed: Send (and Update, Withdraw, Remix, a direct publish) refuse
    while a Claude turn is open — the half-written file is never sent."""
    from fused_render.contrib import remix
    from fused_render.contrib.jobs import PlainError
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    edit(w.app, "app.py", "def amplitude(:\n")      # half-written by the running turn
    head, pr_head = _head(w), w.gh.get_pr("alice/sine", 1)["head_sha"]
    monkeypatch.setattr(_p(), "claude_busy", lambda root: True)
    assert _p().preflight(w.app)["reason"] == "claude"
    for call in (lambda: _p().update(w.app), lambda: _p().withdraw(w.app),
                 lambda: remix.publish(w.app, name="sine-remix", credit="Remixed.")):
        with pytest.raises(PlainError) as e:
            call()
        assert e.value.reason == "claude" and "Claude is still working" in e.value.message
    assert _p().credit_once(w.app)["reason"] == "claude"
    from fused_render.contrib import engine
    w.fake.add_collaborator("alice/sine", "bob")
    assert engine.push_to_main(w.app)["reason"] == "claude"       # end to end
    w.store.update(w.app, lambda r: r.update(proposal=None))
    with pytest.raises(PlainError) as e:
        _send(w)
    assert e.value.reason == "claude"
    assert _head(w) == head and read(w.app, "app.py") == "def amplitude(:\n"
    assert w.gh.get_pr("alice/sine", 1)["head_sha"] == pr_head
    assert "bob/sine-remix" not in w.fake.state()["repos"]


# ---------------------------------------------------------------- update


def test_update_is_a_plain_push_that_github_sees_as_added(w):
    from fused_render.contrib.jobs import PlainError
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    first = _proposal(w)["sent"]
    assert _p().view(w.app)["unproposed"] == 0
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    assert _p().view(w.app)["unproposed"] == 1
    _p().update(w.app)
    p = _proposal(w)
    assert p["sent"] == _head(w) and p["number"] == 1
    remote = w.gh.get_pr("alice/sine", 1)["head_sha"]
    assert remote == p["sent"] and _anc(w, first, remote)     # added to, never replaced
    # Credit never runs after the first send: the Fused-authored "louder"
    # went exactly as it was.
    assert git(w.app, "log", "-1", "--format=%ae") == "apps@fused.io"
    assert git(w.app, "rev-parse", "HEAD~1") == first
    assert _p().view(w.app)["unproposed"] == 0
    with pytest.raises(PlainError, match="already has all your changes") as e:
        _p().update(w.app)
    assert e.value.reason == "nothing-new"
    assert "proposal-updated" in [k for k, _t in _activity(w)]


def test_update_sends_uncommitted_tracked_edits(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    edit(w.app, "app.py", APP_PY_LOUD)
    assert _p().view(w.app)["unproposed"] == 1
    _p().update(w.app)
    assert not _p().dirty(w.app)
    assert "return 9" in w.fake.read("bob/sine", "app.py",
                                     f"refs/heads/{_proposal(w)['head_branch']}")


def test_r1_an_author_push_never_rewrites_the_proposal(w):
    """R1 fixed: Alice's unrelated push is MERGED into Bob's copy; the page
    has nothing new to offer and Update refuses — no force push, no new
    commit ids for what GitHub already has."""
    from fused_render.contrib.jobs import PlainError
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    first = _proposal(w)["sent"]
    w.fake.commit("alice/sine", "main", {"NOTES.md": "n\n"}, "notes", author="alice")
    _bring_in(w)
    assert _anc(w, first, _head(w))
    view = _p().view(w.app)
    assert view["unproposed"] == 0 and "stale" not in view
    with pytest.raises(PlainError, match="already has all your changes"):
        _p().update(w.app)
    assert w.gh.get_pr("alice/sine", 1)["head_sha"] == first


def test_r4_update_after_discard_refuses_instead_of_emptying_the_proposal(w):
    from fused_render.contrib.jobs import PlainError
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    pr_head = w.gh.get_pr("alice/sine", 1)["head_sha"]
    _fetch(w)
    git(w.app, "reset", "-q", "--hard", "refs/remotes/origin/main")   # what Discard leaves
    with pytest.raises(PlainError, match="matches alice's latest"):
        _p().update(w.app)
    pr = w.gh.get_pr("alice/sine", 1)
    assert pr["state"] == "open" and pr["head_sha"] == pr_head


def test_r9_github_update_branch_is_merged_in_not_replayed(w):
    """R9 fixed: GitHub's "Update branch" merged main (with Alice's M2) into
    Bob's proposal. His Update merges that head into his copy, so M2 keeps
    its id — it is never re-created as one of "his changes"."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    m2 = w.fake.commit("alice/sine", "main", {"NOTES.md": "n\n"}, "M2 notes", author="alice")
    gh_head = w.fake.update_branch("alice/sine", 1)
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    _p().update(w.app)
    head = _head(w)
    assert _anc(w, m2, head) and _anc(w, gh_head, head)
    assert git(w.app, "log", "--format=%s", "HEAD").splitlines().count("M2 notes") == 1
    pr = w.gh.get_pr("alice/sine", 1)
    assert pr["head_sha"] == head and _anc(w, gh_head, pr["head_sha"])
    assert read(w.app, "NOTES.md") == "n\n" and "return 9" in read(w.app, "app.py")
    _fetch(w)
    from fused_render.contrib import proposals
    assert proposals.count_unproposed(w.app, _proposal(w)["sent"], _tip(w)) == 0


def test_update_keeps_alices_edit_to_the_proposal(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    p = _proposal(w)
    # Alice fixes something on github.com, straight onto Bob's branch.
    hers = w.fake.commit("bob/sine", p["head_branch"], {"NOTES.md": "thanks!\n"},
                         "Add notes", author="alice")
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    _p().update(w.app)
    head = _head(w)
    assert w.gh.get_pr("alice/sine", 1)["head_sha"] == _proposal(w)["sent"] == head
    assert _anc(w, hers, head)                             # her commit, same id
    assert git(w.app, "log", "-1", "--format=%an", hers) == "alice"
    assert read(w.app, "NOTES.md") == "thanks!\n"          # Bob now runs her edit
    assert "return 9" in read(w.app, "app.py")             # and his own
    assert len(git(w.app, "rev-list", "--parents", "-n1", "HEAD").split()) == 3   # a merge


def test_update_overlap_goes_to_merge_with_claude(w, monkeypatch):
    started = []
    monkeypatch.setattr(_p(), "_start_merge_job",
                        lambda root, **kw: started.append(kw) or "job-1")
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    p = _proposal(w)
    remote = w.fake.commit("bob/sine", p["head_branch"],
                           {"app.py": APP_PY.replace("return 1", "return 7")},
                           "Tweak", author="alice")
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    head = _head(w)
    with pytest.raises(_p().MergeStarted) as e:
        _p().update(w.app)
    assert e.value.reason == "needs-merge" and e.value.job == "job-1"
    assert started == [{"purpose": "proposal", "ours": head, "theirs": remote}]
    assert w.gh.get_pr("alice/sine", 1)["head_sha"] == remote   # hers untouched
    assert _head(w) == head and "return 9" in read(w.app, "app.py")


def test_merge_accepted_sends_the_combined_version(w, monkeypatch):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    seen = []
    monkeypatch.setattr(_p(), "start_update", lambda app_dir: seen.append(app_dir) or {"ok": True})
    assert _p().merge_accepted(w.app) == {"ok": True} and seen == [w.app]


def test_a_rejected_push_says_try_again(w, monkeypatch):
    from fused_render.contrib.jobs import PlainError
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")

    def rejected(*a, **k):
        raise w.gh.GitHubError("conflict")
    monkeypatch.setattr(w.gh, "push", rejected)
    with pytest.raises(PlainError, match="Something changed on GitHub in the meantime"):
        _p().update(w.app)


# -------------------------------------------------------------- withdraw


def test_withdraw_closes_with_a_note_and_clears(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    head = _head(w)
    assert _p().withdraw(w.app) == {"ok": True}
    pr = w.gh.get_pr("alice/sine", 1)
    assert pr["state"] == "closed" and not pr["merged"]
    assert w.gh.pr_comments("alice/sine", 1)[-1]["body"] == "bob withdrew this proposal."
    assert _proposal(w) is None
    assert _head(w) == head
    assert "proposal-withdrawn" in [k for k, _t in _activity(w)]


def test_r13_withdraw_deletes_the_copy_on_github(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    _p().withdraw(w.app)
    _send(w)
    fork = w.fake.bare_path("bob/sine")
    heads = git(fork, "for-each-ref", "--format=%(refname)", "refs/heads/fused-*").split()
    assert heads == [f"refs/heads/{_proposal(w)['head_branch']}"]


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


def test_refresh_records_the_tip_it_checked(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    new = w.fake.commit("alice/sine", "main", {"NOTES.md": "n\n"}, "notes", author="alice")
    _p().refresh(w.app, force=True, tip=new)
    assert _proposal(w)["checked_tip"] == new


def test_asked_to_update_is_shown_and_update_answers_it(w):
    from fused_render.contrib import inbox
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.commit("alice/sine", "main", {"NOTES.md": "n\n"}, "notes", author="alice")
    w.fake.comment_as("alice", "alice/sine", 1,
                      f"{inbox.ASK_DEFAULT}\n\n{w.gh.ASK_UPDATE_MARKER}")
    view = _p().refresh(w.app, force=True)
    assert view["asked_update"] and view["asked"]["author"] == "alice"
    assert w.gh.ASK_UPDATE_MARKER not in view["comment"]["body"]
    _bring_in(w)                        # nothing of Bob's own is new, but she asked
    _p().update(w.app)
    assert w.gh.get_pr("alice/sine", 1)["head_sha"] == _head(w)
    assert not _p().view(w.app)["asked_update"]


def test_refresh_is_throttled(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    calls = len(w.fake.state()["calls"])
    _p().refresh(w.app)
    assert len(w.fake.state()["calls"]) == calls


def test_declined_keeps_changes_and_says_so_once(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    head = _head(w)
    w.fake.close("alice/sine", 1)
    assert _p().refresh(w.app, force=True)["state"] == "declined"
    _p().refresh(w.app, force=True)
    events = [e["kind"] for e in w.store.get(w.app)["events"]]
    assert events.count("declined") == 1
    assert _head(w) == head
    # Declined is settled: a fresh Send is allowed — and never re-credits
    # what was already sent.
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    assert _send(w)["number"] == 2
    assert _anc(w, head, _head(w))


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


def test_merged_keeps_polling_until_returned_then_stops(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.merge("alice/sine", 1, "squash")
    assert _p().refresh(w.app, force=True)["state"] == "merged"
    before = _api_calls(w)
    _p()._last_refresh.clear()
    _p().refresh(w.app)
    assert _api_calls(w) > before                    # the engine has not returned it yet
    w.store.update(w.app, lambda r: r["proposal"].update(returned_at=1.0))   # engine row 7
    before = _api_calls(w)
    _p().refresh(w.app, force=True)
    assert _api_calls(w) == before


def test_r6_a_merged_proposal_on_a_remix_stops_being_polled(w):
    from fused_render.contrib import remix
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    remix.publish(w.app, name="sine-remix", credit="Remixed from alice/sine.")
    w.fake.merge("alice/sine", 1, "squash")
    assert _p().refresh(w.app, force=True)["state"] == "merged"
    n = []
    for _ in range(3):
        _p()._last_refresh.clear()                   # past the 60 s throttle
        before = _api_calls(w)
        _p().refresh(w.app)
        n.append(_api_calls(w) - before)
    assert n == [0, 0, 0]


def test_edits_made_on_github_are_read_back(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.edit_pr("alice/sine", 1, title="Red wave (edited by Alice)")
    assert _p().refresh(w.app, force=True)["title"] == "Red wave (edited by Alice)"


def test_github_trouble_keeps_the_cached_state(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.fail_next("get_pull", "offline")
    assert _p().refresh(w.app, force=True)["state"] == "waiting"
    assert _proposal(w)["cache"]["error"] == "offline"


# ------------------------------------------------- direct publish (R7, R7b)


def test_r7_direct_publish_keeps_the_proposal_commits_and_settles_it(sw):
    """R7/R7b fixed: a collaborator's direct publish (engine.push_to_main)
    sends HEAD as it is — never force, the sent commit unrewritten, so it IS
    in the author's branch — and the open proposal is closed as published
    directly, then never polled again, never shown as declined."""
    from fused_render.contrib import engine
    w = sw
    w.fake.add_collaborator("alice/sine", "bob")
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    sent = _proposal(w)["sent"]
    w.fake.commit("alice/sine", "main", {"NOTES.md": "n\n"}, "notes", author="alice")
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    out = engine.push_to_main(w.app)
    assert out["ok"], out
    bare = w.fake.bare_path("alice/sine")
    assert w.fake.tip("alice/sine", "main") == _head(w)
    assert subprocess.run(["git", "-C", bare, "merge-base", "--is-ancestor", sent, "main"]
                          ).returncode == 0
    assert w.gh.get_pr("alice/sine", 1)["state"] == "closed"
    assert w.gh.pr_comments("alice/sine", 1)[-1]["body"] == "Published directly"
    view = _p().view(w.app)
    assert view["state"] == "merged" and view["direct"] and not view["can_update"]
    before = _api_calls(w)
    _p().refresh(w.app, force=True)
    assert _api_calls(w) == before
    assert "declined" not in [e["kind"] for e in w.store.get(w.app)["events"]]
    assert w.fake.tip("bob/sine", _proposal(w)["head_branch"]) is None    # tidied


def test_direct_publish_before_any_send_credits_bob(sw):
    from fused_render.contrib import engine
    w = sw
    w.fake.add_collaborator("alice/sine", "bob")
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    assert engine.push_to_main(w.app)["ok"]
    bare = w.fake.bare_path("alice/sine")
    assert git(bare, "log", "-1", "--format=%ae", "main") == w.fake.email_of("bob")
    assert "proposal-credit" in [k for k, _t in _activity(w)]


def test_credit_once_is_refused_while_claude_works(w, monkeypatch):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    head = _head(w)
    monkeypatch.setattr(_p(), "claude_busy", lambda root: True)
    assert _p().credit_once(w.app)["reason"] == "claude"
    assert _head(w) == head


# ------------------------------------------------------- credit, pure parts


def test_with_trailer_is_idempotent():
    p = _p()
    once = p.with_trailer("Claude: red\n\nbody text\n")
    assert once == p.with_trailer(once)
    assert once.count("Co-authored-by: Claude") == 1
    joined = p.with_trailer("Subject\n\nSigned-off-by: Bob <b@x>\n")
    assert joined.endswith("Signed-off-by: Bob <b@x>\nCo-authored-by: Claude <noreply@anthropic.com>\n")


def test_credit_keeps_other_peoples_commits(w):
    base = _head(w)
    claude_commit(w.app, {"a.txt": "1\n"}, "one")
    edit(w.app, "b.txt", "2\n")
    git(w.app, "add", "b.txt")
    git(w.app, "-c", "user.name=Carol", "-c", "user.email=carol@example.com",
        "commit", "-q", "-m", "by hand")
    me = w.gh.identity()
    tip = _p().credit(w.app, base, _head(w), me)
    assert git(w.app, "log", "-1", "--format=%an", tip) == "Carol"
    assert git(w.app, "log", "-1", "--format=%an", f"{tip}~1") == "Bob"
    assert _p().credit(w.app, base, tip, me) == tip


def test_credit_keeps_a_merge_in_bobs_range_as_a_merge(w):
    base = _head(w)
    claude_commit(w.app, {"a.txt": "1\n"}, "one")
    git(w.app, "checkout", "-q", "-b", "side", base)
    claude_commit(w.app, {"b.txt": "2\n"}, "two")
    git(w.app, "checkout", "-q", "main")
    git(w.app, "-c", "user.name=Fused", "-c", "user.email=apps@fused.io",
        "merge", "-q", "--no-ff", "-m", "Claude: combine", "side")
    head = _head(w)
    me = w.gh.identity()
    tip = _p().credit(w.app, base, head, me)
    assert git(w.app, "rev-parse", f"{tip}^{{tree}}") == git(w.app, "rev-parse", f"{head}^{{tree}}")
    assert len(git(w.app, "rev-list", "--parents", "-n1", tip).split()) == 3   # still a merge
    authors = set(git(w.app, "log", "--format=%an", f"{base}..{tip}").splitlines())
    assert authors == {"Bob"}
    assert _p().credit(w.app, base, tip, me) == tip                          # idempotent


def test_credit_skips_what_was_sent_and_the_authors_side(w):
    base = _head(w)
    sent = claude_commit(w.app, {"a.txt": "1\n"}, "sent already")
    claude_commit(w.app, {"b.txt": "2\n"}, "not yet")
    me = w.gh.identity()
    tip = _p().credit(w.app, base, _head(w), me, exclude=[sent])
    assert git(w.app, "rev-parse", f"{tip}~1") == sent
    assert git(w.app, "log", "-1", "--format=%an", tip) == "Bob"


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


def test_plain_push_is_never_force(w):
    """`gh.push` without lease/force is a fast-forward-only push: a branch
    holding something else refuses with reason "conflict"."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    a = _head(w)
    w.gh.push(w.app, a, "alice/sine", "side")
    git(w.app, "reset", "-q", "--hard", "HEAD~1")
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "green")}, "green")
    with pytest.raises(w.gh.GitHubError) as e:
        w.gh.push(w.app, _head(w), "alice/sine", "side")
    assert e.value.reason == "conflict" and w.fake.tip("alice/sine", "side") == a


def test_fake_mergeable_and_delete_branch(w):
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    _send(w)
    assert w.gh.get_pr("alice/sine", 1)["mergeable"] is True
    w.fake.commit("alice/sine", "main", {"app.py": APP_PY.replace("return 1", "return 7")},
                  "quieter", author="alice")
    assert w.gh.get_pr("alice/sine", 1)["mergeable"] is False
    branch = _proposal(w)["head_branch"]
    assert w.gh.delete_branch("bob/sine", branch) and w.fake.tip("bob/sine", branch) is None


# --------------------------------------------------------------- contributors


def test_contributors_after_a_merge(w):
    from fused_render.contrib import contributors
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.merge("alice/sine", 1, "squash")
    _fetch(w)
    people = contributors.list(w.app)
    logins = [x["login"] for x in people]
    assert sorted(logins) == ["alice", "bob"]
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
    msgs = re.findall(r'PlainError\(\s*((?:f?"[^"]*"\s*)+)', src)
    msgs += re.findall(r'"message":\s*((?:f?"[^"]*"\s*)+)', src)
    msgs += re.findall(r'^(?:CLAUDE_BUSY|_MOVED|_BUSY) = ("[^"]*")', src, re.M)
    msgs += re.findall(r'_activity\(\s*root,\s*"[^"]*",\s*((?:f?"[^"]*"\s*)+)', src)
    assert len(msgs) > 20
    for msg in msgs:
        low = msg.lower().replace("merge with claude", "")   # the product's own name
        for word in ("branch", "rebase", "fork", "commit", "push", "pushed", "conflict",
                     "origin", "head", "sha", "merge"):
            assert not re.search(rf"\b{word}\b", low), (word, msg)


# ------------------------------------------- with the engine's sync (v2 table)


def _pr_head(w):
    return w.gh.get_pr("alice/sine", 1)["head_sha"]


@pytest.mark.parametrize("method", ["merge", "squash", "rebase"])
def test_lifecycle_propose_update_merge_return(sw, method):
    """The whole loop on the v2 model: propose → the author pushes (the
    engine MERGES her latest in; the proposal on GitHub is not rewritten)
    → Update is a fast-forward push → she merges (any method) → the copy
    returns to her latest keeping exactly the edits made after the last
    send."""
    w = sw
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    sent1 = _proposal(w)["sent"]
    w.fake.commit("alice/sine", "main", {"NOTES.md": "n\n"}, "notes", author="alice")
    w.sync()
    assert _anc(w, sent1, _head(w)) and read(w.app, "NOTES.md") == "n\n"
    assert _pr_head(w) == sent1                                     # untouched
    assert _p().view(w.app)["unproposed"] == 0
    claude_commit(w.app, {"app.py": APP_PY_LOUD}, "louder")
    _p().update(w.app)
    sent2 = _proposal(w)["sent"]
    assert _pr_head(w) == sent2 == _head(w) and _anc(w, sent1, sent2)   # fast-forward
    w.fake.merge("alice/sine", 1, method)
    claude_commit(w.app, {"EXTRA.md": "after sending\n"}, "extra")  # never proposed
    w.sync()
    p = _proposal(w)
    assert p["cache"]["state"] == "merged" and p["returned_at"], p
    tip = _tip(w)
    assert tip == w.fake.tip("alice/sine", "main")
    assert git(w.app, "diff", "--name-only", tip, "HEAD").split() == ["EXTRA.md"]
    # In history too: exactly the one edit made after the last send.
    assert git(w.app, "log", "--first-parent", "--no-merges", "--format=%s", "HEAD",
               f"^{tip}").splitlines() == ["Claude: extra"]
    assert "red" in read(w.app, "index.html") and "return 9" in read(w.app, "app.py")
    assert _p().view(w.app)["state"] == "merged"


def test_r11_an_author_push_racing_send_keeps_the_sent_commit(sw, monkeypatch):
    """R11 fixed: Alice pushes (and Bob's copy syncs) while Send is between
    pushing and recording. The engine merges, never rewrites, so what was
    sent stays in Bob's history and the next sync keeps it there too."""
    w = sw
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    real = w.gh.open_pr

    def racing(*a, **k):
        w.fake.commit("alice/sine", "main", {"NOTES.md": "n\n"}, "notes", author="alice")
        w.sync()
        return real(*a, **k)

    monkeypatch.setattr(w.gh, "open_pr", racing)
    _send(w)
    sent = _proposal(w)["sent"]
    assert _anc(w, sent, _head(w))
    w.fake.commit("alice/sine", "main", {"MORE.md": "m\n"}, "more", author="alice")
    w.sync()
    assert _anc(w, sent, _head(w)) and _pr_head(w) == sent
    assert _p().view(w.app)["unproposed"] == 0


def test_r10_everything_sent_is_bobs(sw):
    """R10 fixed: the engine's "Your edits" commit and its merge carry the
    copy's own identity (Bob's GitHub one, set at install) and the credit
    covers Fused-authored commits — no machine or Fused identity reaches the
    proposal."""
    w = sw
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    edit(w.app, "app.py", APP_PY_LOUD)              # uncommitted; Alice changes app.py too
    w.fake.commit("alice/sine", "main", {"app.py": APP_PY + "\n# alice\n"}, "alice app.py",
                  author="alice")
    w.sync()
    _send(w)
    branch = _proposal(w)["head_branch"]
    fork = w.fake.bare_path("bob/sine")
    git(fork, "fetch", "-q", w.fake.bare_path("alice/sine"), "+main:refs/tmp/alice")
    emails = set(git(fork, "log", "--format=%ae", f"refs/heads/{branch}", "^refs/tmp/alice"
                     ).split())
    assert emails == {w.fake.email_of("bob")}, emails


def test_fetch_detail_says_why(w):
    ok = w.gh.fetch_detail(w.app, "alice/sine", "+refs/heads/main:refs/fused/x")
    assert ok == (True, None)
    assert w.gh.fetch_detail(w.app, "alice/nope", "+refs/heads/main:refs/fused/x") == \
        (False, "not-found")
    git(w.app, "config", "url.https://evil.example/.insteadOf", "https://github.com/")
    assert w.gh.fetch_detail(w.app, "alice/sine", "+refs/heads/main:refs/fused/x") == \
        (False, "redirected")
    assert w.gh.fetch(w.app, "alice/sine", "+refs/heads/main:refs/fused/x") is False


def test_cached_identity_never_asks_github(w):
    w.gh.forget_identity()
    assert w.gh.cached_identity() is None
    calls = w.fake.state()["calls"].count("user")
    assert w.gh.identity()["login"] == "bob"
    assert w.gh.cached_identity()["login"] == "bob"
    assert w.fake.state()["calls"].count("user") == calls + 1
    w.gh.forget_identity()
    w.fake.fail_next("user", "offline")
    assert w.gh.identity() is None and w.gh.cached_identity() is None


# ------------------------------------------------- review round 1 (§16.12)


@pytest.mark.parametrize("key", [
    "http.https://*.com/.sslCAInfo",
    "http.https://*.com/.curloptResolve",
    "http.https://*.github.com/.extraHeader",
    "http.https://github.com:443/.proxy",
    "http.https://bob@github.com/.sslVerify",
])
def test_s2_url_scoped_http_keys_are_matched_the_way_git_matches_them(w, key):
    url = "https://github.com/alice/sine.git"
    assert w.gh.url_rewrites(w.app, url) == []
    git(w.app, "config", "--local", key, "x")
    assert w.gh.url_rewrites(w.app, url)
    sha = _head(w)
    with pytest.raises(w.gh.GitHubError):
        w.gh.push(w.app, sha, "bob/sine", "x", lease="")
    assert w.gh.fetch_detail(w.app, "alice/sine", "+refs/heads/main:refs/fused/x") == \
        (False, "redirected")


@pytest.mark.parametrize("key", ["http.https://*.example.com/.sslCAInfo",
                                 "http.https://gitlab.com/.curloptResolve"])
def test_s2_keys_scoped_elsewhere_do_not_refuse(w, key):
    git(w.app, "config", "--local", key, "x")
    assert w.gh.url_rewrites(w.app, "https://github.com/alice/sine.git") == []


def test_s2_wildcard_key_in_an_included_file_or_worktree_config(w, tmp_path):
    url = "https://github.com/alice/sine.git"
    extra = tmp_path / "inc.cfg"
    extra.write_text('[http "https://*.com/"]\n\tcurloptResolve = github.com:443:203.0.113.9\n')
    git(w.app, "config", "include.path", str(extra))
    assert w.gh.url_rewrites(w.app, url)
    git(w.app, "config", "--unset", "include.path")
    assert w.gh.url_rewrites(w.app, url) == []
    git(w.app, "config", "extensions.worktreeConfig", "true")
    git(w.app, "config", "--worktree", "http.https://*.com/.sslCAInfo", "/tmp/evil.pem")
    assert w.gh.url_rewrites(w.app, url)


def test_v1_update_is_offered_and_sent_after_merge_with_claude(sw, monkeypatch):
    """V1 fixed: Alice's latest overlaps Bob's open proposal; Bob's Merge
    with Claude resolves it. The resolution is a merge (never counted in
    `unproposed`), yet the page learns the proposal no longer fits and that
    HEAD holds more than GitHub has — and Update sends it."""
    from fused_render.contrib import engine, merge_job
    w = sw
    monkeypatch.setattr(_p(), "merge_accepted", lambda app_dir: {"ok": True})
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.commit("alice/sine", "main", {"index.html": PAGE.replace("blue", "green")},
                  "green", author="alice")
    w.sync()
    plan = engine.merge_plan(w.app)
    assert plan, w.store.get(w.app).get("needs_merge")

    def resolver(wt, files, ctx):
        with open(os.path.join(wt, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(PAGE.replace("blue", "red-green"))
        return {"ok": True, "summary": "Kept both."}

    job = merge_job.start(w.app, purpose=plan["purpose"], ours=plan["ours"],
                          theirs=plan["theirs"], resolver=resolver)
    import time
    deadline = time.time() + 30
    while merge_job.get(job)["status"] == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert merge_job.accept(job)["ok"]
    view = _p().refresh(w.app, force=True)
    assert view["unproposed"] == 0
    assert view["fits"] is False and view["head_differs"] is True
    _p().update(w.app)
    assert w.gh.get_pr("alice/sine", 1)["mergeable"] is True
    assert _p().refresh(w.app, force=True)["head_differs"] is False


def test_head_differs_is_false_after_a_clean_bring_in(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    assert _p().view(w.app)["head_differs"] is False
    w.fake.commit("alice/sine", "main", {"NOTES.md": "n\n"}, "notes", author="alice")
    _bring_in(w)
    view = _p().refresh(w.app, force=True)
    assert view["head_differs"] is False and view["fits"] is True


def test_v2_the_credit_line_has_no_undo_but_the_backup_stays(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    old = _head(w)
    _send(w)
    line = next(a for a in w.store.activity(w.app) if a["kind"] == "proposal-credit")
    assert line["undo"] is False
    refs = git(w.app, "for-each-ref", "--format=%(objectname)", "refs/fused/backup/").split()
    assert old in refs


def test_v5_update_takes_ticked_new_files(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    edit(w.app, "extra.py", "X = 1\n")
    edit(w.app, "scratch.txt", "mine\n")
    assert _p().untracked(w.app) == ["extra.py", "scratch.txt"]
    _p().update(w.app, include=["extra.py"])
    ref = f"refs/heads/{_proposal(w)['head_branch']}"
    assert w.fake.read("bob/sine", "extra.py", ref) == "X = 1\n"
    assert w.fake.read("bob/sine", "scratch.txt", ref) is None
    assert _p().untracked(w.app) == ["scratch.txt"]


def test_fused_transfers_never_run_the_repos_hooks(w):
    """§16.12: a `github` copy refuses every push through its own pre-push
    hook; fused's explicit sends run with no hooks at all."""
    hook = os.path.join(w.app, ".git", "hooks", "pre-push")
    os.makedirs(os.path.dirname(hook), exist_ok=True)
    with open(hook, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho refused >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    assert w.gh.get_pr("alice/sine", 1)["head_sha"] == _head(w)
    args = w.gh.git_net_args()
    assert "core.hooksPath=" + os.devnull in args and "core.fsmonitor=" in args


def test_r2f_a_newer_tip_only_fetched_is_nothing_to_send(w):
    """Round 2 (F): Bob brought in tip1; tip2 is fetched but NOT brought in
    (auto-sync off, Keep mine). head_differs judges HEAD against the
    author's latest HEAD actually holds, so nothing of Bob's reads as new."""
    from fused_render.contrib.jobs import PlainError
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.commit("alice/sine", "main", {"N1.md": "1\n"}, "n1", author="alice")
    _bring_in(w)
    tip2 = w.fake.commit("alice/sine", "main", {"N2.md": "2\n"}, "n2", author="alice")
    assert _fetch(w) == tip2 and not _anc(w, tip2, _head(w))
    view = _p().refresh(w.app, force=True)
    assert view["head_differs"] is False and view["unproposed"] == 0
    with pytest.raises(PlainError, match="already has all your changes"):
        _p().update(w.app)


def test_r2f_a_resolution_still_differs_when_a_newer_tip_is_only_fetched(sw, monkeypatch):
    """The other side of (F): after a Merge with Claude resolution against
    tip1, a fetched-only tip2 must not hide it — head_differs stays True."""
    from fused_render.contrib import engine, merge_job
    w = sw
    monkeypatch.setattr(_p(), "merge_accepted", lambda app_dir: {"ok": True})
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _send(w)
    w.fake.commit("alice/sine", "main", {"index.html": PAGE.replace("blue", "green")},
                  "green", author="alice")
    w.sync()
    plan = engine.merge_plan(w.app)

    def resolver(wt, files, ctx):
        with open(os.path.join(wt, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(PAGE.replace("blue", "red-green"))
        return {"ok": True, "summary": "Kept both."}

    job = merge_job.start(w.app, purpose=plan["purpose"], ours=plan["ours"],
                          theirs=plan["theirs"], resolver=resolver)
    import time
    deadline = time.time() + 30
    while merge_job.get(job)["status"] == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert merge_job.accept(job)["ok"]
    w.fake.commit("alice/sine", "main", {"N2.md": "2\n"}, "n2", author="alice")
    _fetch(w)                                       # fetched, not brought in
    assert _p().view(w.app)["head_differs"] is True


def arm_repo_config(root, tmp_path):
    """Plant a repo-configured fsmonitor command and hooks that write a
    marker when git runs them (rev-sec r2). Answers the marker path."""
    marker = tmp_path / "FIRED"
    script = tmp_path / "fsmon.sh"
    script.write_text(f"#!/bin/sh\necho fsmonitor >> {marker}\nexit 1\n")
    script.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    for h in ("pre-commit", "post-checkout", "commit-msg", "post-commit", "post-merge",
              "pre-push", "reference-transaction"):
        p = hooks / h
        p.write_text(f"#!/bin/sh\necho {h} >> {marker}\n")
        p.chmod(0o755)
    git(root, "config", "--local", "core.fsmonitor", str(script))
    git(root, "config", "--local", "core.hooksPath", str(hooks))
    return marker


def test_r2_proposal_git_calls_never_run_repo_configured_commands(w, tmp_path):
    from fused_render.contrib import contributors
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    edit(w.app, "app.py", APP_PY_LOUD)
    edit(w.app, "new.py", "N = 1\n")
    marker = arm_repo_config(w.app, tmp_path)
    _p().untracked(w.app)
    _send(w, include=["new.py"])
    _p().view(w.app)
    claude_commit(w.app, {"NOTES.md": "n\n"}, "notes")
    marker.unlink(missing_ok=True)        # claude_commit (the test's own git) ran them
    _p().update(w.app)
    _p().refresh(w.app, force=True)
    contributors.list(w.app)
    _p().withdraw(w.app)
    assert not marker.exists(), marker.read_text()
