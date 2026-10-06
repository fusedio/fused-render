"""git_upstream auto-sync: fast-forward on app open, push of app-made commits,
persistent failures, silent cases (SPEC-git-auto-sync.md). Real repos, no
mocked git."""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _git_repo import git, git_available, with_remote, write  # noqa: E402

from fused_render import git_upstream

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _sync(fn):
    fn()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(git_upstream, "_checked", {})
    monkeypatch.setattr(git_upstream, "_state", {})
    monkeypatch.setattr(git_upstream, "_sync_failures", {})
    monkeypatch.setattr(git_upstream, "_pulled_events", [])
    monkeypatch.setattr(git_upstream, "_pending", {})
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: True)
    if git_upstream._check_slot.acquire(timeout=git_upstream.TIMEOUT_S + 5):
        git_upstream._check_slot.release()


def _ident(repo):
    git(repo, "config", "user.name", "Fixture Author")
    git(repo, "config", "user.email", "fixture@example.com")


def _commit(repo, name, text, msg):
    write(repo, name, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


def _make(tmp_path, name="repo"):
    """(local, remote, other): local is a clone-equivalent in sync with the
    bare remote; `other` is a second checkout used to advance the remote."""
    remote = str(tmp_path / f"{name}.git")
    local = str(tmp_path / name)
    os.makedirs(local)
    git(local, "init", "-q")
    _ident(local)
    _commit(local, "a.txt", "1\n", "c1")
    with_remote(local, remote)
    other = str(tmp_path / f"{name}-other")
    git(str(tmp_path), "clone", "-q", remote, other)
    _ident(other)
    return local, remote, other


def _advance_remote(other, name="b.txt", text="2\n", msg="c2"):
    _commit(other, name, text, msg)
    git(other, "push", "-q", "origin", "HEAD:main")


def _head(repo):
    return git(repo, "rev-parse", "HEAD").strip()


def _remote_head(remote):
    return git(remote, "rev-parse", "refs/heads/main").strip()


def test_open_fast_forwards_a_clean_behind_repo_and_reports_the_pull(tmp_path):
    local, _remote, other = _make(tmp_path)
    _advance_remote(other)
    _advance_remote(other, "c.txt", "3\n", "c3")

    git_upstream.note_app_opened(local, _runner=_sync)

    assert os.path.exists(os.path.join(local, "c.txt"))
    assert git_upstream.known_repos() == []
    pulls = git_upstream.recent_pulls()
    assert len(pulls) == 1 and pulls[0]["count"] == 2
    assert pulls[0]["name"] == "repo"
    assert git_upstream.sync_failures() == []


def test_open_does_not_push(tmp_path):
    local, remote, _other = _make(tmp_path)
    before = _remote_head(remote)
    _commit(local, "mine.txt", "x\n", "mine")
    git_upstream.note_app_opened(local, _runner=_sync)
    assert _remote_head(remote) == before


def test_dirty_tree_blocks_the_pull_and_records_one_failure(tmp_path):
    local, _remote, other = _make(tmp_path)
    _advance_remote(other)
    write(local, "a.txt", "edited\n")
    head = _head(local)

    git_upstream.note_app_opened(local, _runner=_sync)

    assert _head(local) == head
    fails = git_upstream.sync_failures()
    assert [f["reason"] for f in fails] == ["dirty"]
    assert fails[0]["action"] == "Auto-update on app open"
    assert "git pull --ff-only" in fails[0]["command"]
    assert git_upstream.recent_pulls() == []
    # The Update card is replaced by the failure notification.
    assert git_upstream.known_repos() == []


def test_a_repeat_failure_updates_the_same_row(tmp_path):
    local, _remote, other = _make(tmp_path)
    _advance_remote(other)
    write(local, "a.txt", "edited\n")
    root = os.path.realpath(local)
    git_upstream.sync_repo(root, action="x", push=False)
    git_upstream.sync_repo(root, action="x", push=False)
    assert len(git_upstream.sync_failures()) == 1


def test_diverged_changes_nothing_and_quotes_gits_full_output(tmp_path):
    local, _remote, other = _make(tmp_path)
    _advance_remote(other)
    _commit(local, "mine.txt", "x\n", "mine")
    head = _head(local)

    res = git_upstream.sync_repo(os.path.realpath(local),
                                 action="Auto-push after Claude commit", push=True)

    assert res["status"] == "failed"
    assert _head(local) == head
    (fail,) = git_upstream.sync_failures()
    assert fail["reason"] == "diverged"
    assert fail["action"] == "Auto-push after Claude commit"
    assert "fast-forward" in fail["output"].lower()


def test_push_after_commit_makes_remote_equal_local(tmp_path):
    local, remote, _other = _make(tmp_path)
    _commit(local, "mine.txt", "x\n", "mine")
    res = git_upstream.sync_repo(os.path.realpath(local), action="a", push=True)
    assert res["status"] == "synced" and res["pushed"] is True
    assert _remote_head(remote) == _head(local)
    assert git_upstream.recent_pulls() == []
    assert git_upstream.sync_failures() == []


def test_push_fast_forwards_from_remote_first(tmp_path):
    local, remote, other = _make(tmp_path)
    _advance_remote(other)
    # A local commit on top of the OLD tip diverges; instead make local clean
    # and behind with nothing ahead: push=True must still just pull.
    res = git_upstream.sync_repo(os.path.realpath(local), action="a", push=True)
    assert res["status"] == "synced" and res["pulled"] == 1
    assert _remote_head(remote) == _head(local)


def test_rejected_push_is_a_failure_with_full_output(tmp_path, monkeypatch):
    local, remote, other = _make(tmp_path)
    _commit(local, "mine.txt", "x\n", "mine")
    root = os.path.realpath(local)
    # The remote moves between our fetch and our push.
    real_run = git_upstream._run

    def racing(r, *args, **kw):
        if args and args[0] == "push":
            _advance_remote(other)
        return real_run(r, *args, **kw)

    monkeypatch.setattr(git_upstream, "_run", racing)
    res = git_upstream.sync_repo(root, action="Auto-push after Claude commit", push=True)
    assert res["status"] == "failed"
    (fail,) = git_upstream.sync_failures()
    assert fail["reason"] == "rejected"
    assert fail["command"] == "git push -- origin main"
    assert "fetch first" in fail["output"] or "rejected" in fail["output"]
    assert "hint" in fail["output"]  # not only the last line


def test_success_clears_a_standing_failure(tmp_path):
    local, _remote, other = _make(tmp_path)
    _advance_remote(other)
    write(local, "a.txt", "edited\n")
    root = os.path.realpath(local)
    git_upstream.sync_repo(root, action="a", push=False)
    assert git_upstream.sync_failures()
    git(local, "checkout", "--", "a.txt")
    res = git_upstream.retry_sync(root, "dirty")
    assert res["status"] == "synced"
    assert git_upstream.sync_failures() == []


def test_no_remote_is_a_silent_skip(tmp_path):
    local = str(tmp_path / "solo")
    os.makedirs(local)
    git(local, "init", "-q")
    _ident(local)
    _commit(local, "a.txt", "1\n", "c1")
    res = git_upstream.sync_repo(os.path.realpath(local), action="a", push=True)
    assert res["status"] == "skipped"
    assert git_upstream.sync_failures() == []


def test_other_branch_is_a_silent_skip(tmp_path):
    local, remote, other = _make(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    _commit(local, "f.txt", "x\n", "f")
    before = _remote_head(remote)
    res = git_upstream.sync_repo(os.path.realpath(local), action="a", push=True)
    assert res["status"] == "skipped" and res["why"] == "not-default"
    assert _remote_head(remote) == before
    assert git_upstream.sync_failures() == []


def test_unresolvable_remote_never_raises_and_changes_nothing(tmp_path):
    local, _remote, _other = _make(tmp_path)
    git(local, "remote", "set-head", "origin", "main")
    git(local, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    _commit(local, "mine.txt", "x\n", "mine")
    head = _head(local)
    res = git_upstream.sync_repo(os.path.realpath(local), action="a", push=True)
    # A missing local path is a git error rather than a network one, so it may
    # classify as a failure; the offline classification itself is pinned by
    # test_classifier. Here: no raise, nothing touched.
    assert res["status"] in {"failed", "offline"}
    assert _head(local) == head


@pytest.mark.parametrize("text,expected", [
    ("fatal: unable to access 'https://x/': Could not resolve host: x", "offline"),
    ("ssh: Could not resolve hostname github.com", "offline"),
    ("fatal: Authentication failed for 'https://x/'", "auth"),
    ("git@github.com: Permission denied (publickey).", "auth"),
    ("fatal: could not read Username for 'https://x': terminal prompts disabled", "auth"),
    (" ! [rejected]        main -> main (fetch first)", "rejected"),
    ("fatal: something odd", "git-failed"),
])
def test_classifier(text, expected):
    result = (1, b"", text.encode())
    assert git_upstream._classify(result, push=True) == expected


def test_a_timeout_is_offline():
    assert git_upstream._classify(None) == "offline"


def test_setting_off_keeps_todays_behaviour(tmp_path, monkeypatch):
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: False)
    local, _remote, other = _make(tmp_path)
    _advance_remote(other)
    head = _head(local)
    git_upstream.note_app_opened(local, _runner=_sync)
    assert _head(local) == head
    assert git_upstream.recent_pulls() == []
    (row,) = git_upstream.known_repos()
    assert row["behind"] == 1
    assert git_upstream.schedule_sync(local, "a", _runner=_sync) is False


def test_schedule_sync_pushes_in_the_background_runner(tmp_path):
    local, remote, _other = _make(tmp_path)
    _commit(local, "mine.txt", "x\n", "mine")
    assert git_upstream.schedule_sync(local, "Auto-push", _runner=_sync) is True
    assert _remote_head(remote) == _head(local)


def test_known_repo_includes_a_failed_repo(tmp_path):
    local, _remote, other = _make(tmp_path)
    _advance_remote(other)
    write(local, "a.txt", "edited\n")
    root = os.path.realpath(local)
    git_upstream.sync_repo(root, action="a", push=False)
    assert git_upstream.is_known_repo(root)


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from fused_render.server import create_app

    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))
    (tmp_path / "Fused").mkdir()
    return TestClient(create_app(start_dir=str(tmp_path)))


_FUSED = {"X-Fused": "1"}


def test_get_reports_failures_and_pulls_and_post_retries_and_dismisses(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    local, _remote, other = _make(tmp_path)
    _advance_remote(other)
    write(local, "a.txt", "edited\n")
    root = os.path.realpath(local)
    git_upstream.sync_repo(root, action="Auto-update on app open", push=False)

    body = client.get("/api/git-upstream").json()
    assert body["auto_sync"] is True
    assert [f["reason"] for f in body["sync_failures"]] == ["dirty"]
    assert body["pulls"] == []

    # Still dirty: retry fails in place.
    r = client.post("/api/git-upstream", json={"action": "sync-retry", "root": root,
                                               "reason": "dirty"}, headers=_FUSED).json()
    assert r["ok"] is False and r["reason"] == "dirty"

    git(local, "checkout", "--", "a.txt")
    r = client.post("/api/git-upstream", json={"action": "sync-retry", "root": root,
                                               "reason": "dirty"}, headers=_FUSED).json()
    assert r["ok"] is True
    body = client.get("/api/git-upstream").json()
    assert body["sync_failures"] == [] and len(body["pulls"]) == 1

    write(local, "a.txt", "edited again\n")
    _advance_remote(other, "z.txt", "z\n", "z")
    git_upstream.sync_repo(root, action="x", push=False)
    r = client.post("/api/git-upstream", json={"action": "sync-dismiss", "root": root,
                                               "reason": "dirty"}, headers=_FUSED).json()
    assert r["ok"] is True
    assert client.get("/api/git-upstream").json()["sync_failures"] == []


def test_retry_that_did_not_sync_is_not_ok(tmp_path, monkeypatch):
    """Retry returns ok only when the sync really cleared the failure; offline,
    busy and skipped leave the row standing, so the UI must keep it."""
    client = _client(tmp_path, monkeypatch)
    local, _remote, other = _make(tmp_path)
    _advance_remote(other)
    write(local, "a.txt", "edited\n")
    root = os.path.realpath(local)
    git_upstream.sync_repo(root, action="Auto-update on app open", push=False)
    body = {"action": "sync-retry", "root": root, "reason": "dirty"}

    for result in ({"ok": True, "status": "offline"},
                   {"ok": True, "status": "offline", "busy": True},
                   {"ok": True, "status": "skipped", "why": "not-default"}):
        monkeypatch.setattr(git_upstream, "retry_sync", lambda r, why, _x=result: _x)
        r = client.post("/api/git-upstream", json=body, headers=_FUSED).json()
        assert r["ok"] is False, result
        assert r["message"]
    failures = client.get("/api/git-upstream").json()["sync_failures"]
    assert [f["reason"] for f in failures] == ["dirty"]


def test_auth_failure_on_open_fetch_records_a_row_offline_stays_silent(tmp_path, monkeypatch):
    local, _remote, _other = _make(tmp_path)
    root = os.path.realpath(local)
    git(local, "remote", "set-head", "origin", "--auto")
    os.rename(_remote, _remote + ".moved")
    monkeypatch.setattr(git_upstream, "_classify", lambda *a, **k: "auth")
    git_upstream.note_app_opened(local, _runner=_sync)
    assert [f["reason"] for f in git_upstream.sync_failures()] == ["auth"]
    monkeypatch.setattr(git_upstream, "_sync_failures", {})
    monkeypatch.setattr(git_upstream, "_checked", {})
    monkeypatch.setattr(git_upstream, "_classify", lambda *a, **k: "offline")
    git_upstream.note_app_opened(local, _runner=_sync)
    assert git_upstream.sync_failures() == []
    assert root


# ------------------------------------------- folder-open trigger + busy slot

_APP_HTML = '<html><head><meta name="fused-app" /></head><body>x</body></html>'


def _inline_note(monkeypatch):
    """Route the endpoint's `note_app_opened` through the inline runner."""
    real = git_upstream.note_app_opened
    monkeypatch.setattr(git_upstream, "note_app_opened",
                        lambda path, **kw: real(path, _runner=_sync))


def test_folder_open_pulls_and_does_not_record_recency(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _inline_note(monkeypatch)
    local, _remote, other = _make(tmp_path)
    write(local, "app.html", _APP_HTML)
    git(local, "add", "-A")
    git(local, "commit", "-q", "-m", "app")
    git(local, "push", "-q", "origin", "HEAD:main")
    git(other, "pull", "-q")
    _advance_remote(other, "c.txt", "3\n", "c3")

    from fused_render.server.routers import apps as apps_router
    recorded = []
    monkeypatch.setattr(apps_router, "record_app_open", lambda d: recorded.append(d))

    r = client.get("/api/apps/entry", params={"path": local, "opened": 1}).json()
    assert r["entry"].endswith("app.html")
    assert os.path.exists(os.path.join(local, "c.txt"))
    assert len(git_upstream.recent_pulls()) == 1
    assert recorded == []


def test_folder_open_on_divergence_records_a_failure_row(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _inline_note(monkeypatch)
    local, _remote, other = _make(tmp_path)
    write(local, "app.html", _APP_HTML)
    git(local, "add", "-A")
    git(local, "commit", "-q", "-m", "app")
    git(local, "push", "-q", "origin", "HEAD:main")
    git(other, "pull", "-q")
    _advance_remote(other, "c.txt", "3\n", "c3")
    _commit(local, "mine.txt", "m\n", "mine")

    client.get("/api/apps/entry", params={"path": local, "opened": 1})
    assert [f["reason"] for f in git_upstream.sync_failures()] == ["diverged"]


def test_entry_probe_without_opened_does_not_trigger(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(git_upstream, "note_app_opened",
                        lambda path, **kw: calls.append(path))
    local, _remote, _other = _make(tmp_path)
    write(local, "app.html", _APP_HTML)
    client.get("/api/apps/entry", params={"path": local})
    assert calls == []
    # An entry-less folder is not an app being opened either.
    client.get("/api/apps/entry", params={"path": str(tmp_path), "opened": 1})
    assert calls == []


def test_busy_slot_open_is_parked_and_runs_when_the_slot_frees(tmp_path):
    local, _remote, other = _make(tmp_path)
    _advance_remote(other)
    assert git_upstream._check_slot.acquire(blocking=False)
    try:
        started = git_upstream.note_app_opened(local, _runner=_sync)
        assert started is False
        assert not os.path.exists(os.path.join(local, "b.txt"))
    finally:
        git_upstream._check_slot.release()
        git_upstream._drain_pending(_sync)
    assert os.path.exists(os.path.join(local, "b.txt"))
    assert git_upstream._pending == {}


def test_parked_opens_are_deduped_and_chain_one_at_a_time(tmp_path):
    a, _ra, other_a = _make(tmp_path, "a")
    b, _rb, other_b = _make(tmp_path, "b")
    _advance_remote(other_a)
    _advance_remote(other_b)
    assert git_upstream._check_slot.acquire(blocking=False)
    try:
        for path in (a, a, b):
            git_upstream.note_app_opened(path, _runner=_sync)
        assert list(git_upstream._pending) == [a, b]
    finally:
        git_upstream._check_slot.release()
        git_upstream._drain_pending(_sync)
    assert os.path.exists(os.path.join(a, "b.txt"))
    assert os.path.exists(os.path.join(b, "b.txt"))
    assert git_upstream._check_slot.acquire(blocking=False)  # slot ends free
    git_upstream._check_slot.release()


def test_parked_open_respects_the_throttle(tmp_path):
    local, _remote, other = _make(tmp_path)
    git_upstream.note_app_opened(local, _runner=_sync)  # stamps the throttle
    _advance_remote(other)
    assert git_upstream._check_slot.acquire(blocking=False)
    try:
        git_upstream.note_app_opened(local, _runner=_sync)
    finally:
        git_upstream._check_slot.release()
        git_upstream._drain_pending(_sync)
    assert not os.path.exists(os.path.join(local, "b.txt"))
