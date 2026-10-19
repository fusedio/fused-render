"""contrib/ownsync.py — an own copy's divergence fix (docs/CONTRIB_LOOP.md
§16.7) against REAL git repos: a local bare repo stands in for the author's
GitHub repo, `lap` is her app folder on this Mac (an `own` record), and
`other` is her other machine (or GitHub's web editor) pushing to the same
repo.

Every audit-author repro that described a v1 bug is here asserting the
fixed behaviour: A (a headless turn's end never stalls the publish), B1 (a
rewrite on GitHub plus a local edit is a stuck state WITH buttons), B2 (the
edits sentence has no "commit" and its button carries on), B3/B3b (a
different repo at origin never replaces the folder), C (a bounded number of
git processes with a long history of fetches), G (the author's own git
config never stops her sync), and Accept (merge/squash/rebase) followed by
the sync leaves her copy synced and published with her unpublished edit.
"""
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _git_repo import git, git_available, write  # noqa: E402

from fused_render import git_upstream  # noqa: E402
from fused_render.contrib import classify, ownsync, replay, store  # noqa: E402

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")

REPO = "alice/sine"
_GIT_WORDS = ("commit", "branch", "rebase", "push", "conflict", "origin", "HEAD",
              "merge ", "fork", "sha")


# --------------------------------------------------------------- fixtures


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    home.mkdir()
    (ws / "app").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(git_upstream, "_checked", {})
    monkeypatch.setattr(git_upstream, "_state", {})
    monkeypatch.setattr(git_upstream, "_sync_failures", {})
    monkeypatch.setattr(git_upstream, "_pulled_events", [])
    monkeypatch.setattr(git_upstream, "_pending", {})
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: True)
    monkeypatch.setattr(classify, "ensure_in_background", lambda app_dir: False)
    from fused_render import claude_session_move as csm
    monkeypatch.setattr(csm, "PROJECTS_DIR", str(tmp_path / "claude-projects"))
    # The author's GitHub account (never a real `gh` call in a test).
    from fused_render.contrib import gh
    alice = {"login": "alice", "id": 7, "name": "Alice",
             "email": "7+alice@users.noreply.github.com"}
    monkeypatch.setattr(gh, "identity", lambda **kw: dict(alice))
    monkeypatch.setattr(gh, "cached_identity", lambda: dict(alice))

    class Env:
        pass

    e = Env()
    e.tmp = tmp_path
    e.home = home
    e.remote = str(tmp_path / "remote.git")
    git(str(tmp_path), "init", "-q", "--bare", "-b", "main", e.remote)
    seed = str(tmp_path / "seed")
    git(str(tmp_path), "init", "-q", "-b", "main", seed)
    write(seed, "index.html", "<p>hello</p>\n")
    write(seed, "a.txt", "one\ntwo\nthree\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "c1")
    git(seed, "push", "-q", e.remote, "main")
    # Her app folder on this Mac, and her other machine.
    e.lap = os.path.join(str(ws), "app", "sine")
    git(str(tmp_path), "clone", "-q", e.remote, e.lap)
    e.lap = os.path.realpath(e.lap)
    store.put(e.lap, store.new_record("own", repo=REPO, branch="main",
                                      classified_by="install"))
    e.other = str(tmp_path / "other")
    git(str(tmp_path), "clone", "-q", e.remote, e.other)
    _flags(e, sources=True)
    return e


def _flags(e, **on):
    keys = {"sources": "contrib_sources_enabled", "changes": "contrib_changes_enabled",
            "inbox": "contrib_inbox_enabled", "loop": "contrib_loop_enabled"}
    with open(e.home / "prefs.json", "w") as f:
        json.dump({keys[k]: v for k, v in on.items()}, f)


def _commit(repo, name, text, msg):
    write(repo, name, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return _head(repo)


def _elsewhere(e, name, text, msg):
    """Her other machine publishes an edit."""
    git(e.other, "pull", "-q", "--ff-only", "origin", "main")
    tip = _commit(e.other, name, text, msg)
    git(e.other, "push", "-q", "origin", "main")
    return tip


def _head(repo):
    return git(repo, "rev-parse", "HEAD").strip()


def _remote(e):
    return git(e.remote, "rev-parse", "main").strip()


def _read(repo, name):
    with open(os.path.join(repo, name)) as f:
        return f.read()


def _sync(root, push=True):
    return git_upstream.sync_repo(root, action="test", push=push)


def _activity(root):
    return store.activity(root)


def _plain(text):
    low = text.lower()
    return not any(w.lower() in low for w in _GIT_WORDS)


def _diverge(e):
    """The everyday divergence: she published from her other machine, and
    has an unpublished edit here to a different file."""
    _elsewhere(e, "index.html", "<p>hello from the other Mac</p>\n", "other mac")
    return _commit(e.lap, "b.txt", "mine\n", "my edit")


def _rewrite_elsewhere(e):
    """Her other machine rewrites what this copy already published."""
    git(e.other, "fetch", "-q", "origin")
    git(e.other, "reset", "-q", "--hard", "origin/main")
    git(e.other, "commit", "-q", "--amend", "-m", "published edit (reworded)")
    git(e.other, "push", "-q", "--force", "origin", "main")
    return _remote(e)


# ---------------------------------------------------------------- the hook


def test_flags_off_diverged_is_todays_failure(env):
    """Loop off: resolve answers "not handled" and today's row is recorded,
    tree untouched."""
    _flags(env)
    head = _diverge(env)
    with replay.slot():
        git_upstream._run(env.lap, "fetch", "--", "origin", "main")
        assert ownsync.resolve(env.lap, action="test", push=True) is None
    assert _head(env.lap) == head


def test_diverged_puts_edits_on_top_and_publishes(env):
    _diverge(env)
    res = _sync(env.lap)
    assert res["status"] == "synced" and res["pushed"] is True and res["pulled"] == 1
    assert _head(env.lap) == _remote(env)
    assert _read(env.lap, "b.txt") == "mine\n"
    assert "other Mac" in _read(env.lap, "index.html")
    assert git_upstream.sync_failures() == []
    lines = _activity(env.lap)
    assert len(lines) == 1 and lines[0]["undo"] is True and _plain(lines[0]["text"])
    assert ownsync.status(env.lap)["state"] == "published"
    assert len(git(env.lap, "worktree", "list").splitlines()) == 1
    # The re-made edit keeps its author; the one re-making it is her account.
    assert git(env.lap, "log", "-1", "--format=%an|%cn <%ce>").strip() == \
        "Fixture Author|Alice <7+alice@users.noreply.github.com>"


def test_push_false_brings_in_without_publishing(env):
    _diverge(env)
    before = _remote(env)
    res = _sync(env.lap, push=False)
    assert res["status"] == "synced" and res["pushed"] is False
    assert _remote(env) == before
    st = ownsync.status(env.lap)
    assert st["state"] == "unpublished" and st["unpublished"] == 1
    assert st["fixes"] == ["publish-now"]
    assert ownsync.publish_now(env.lap)["ok"] and _remote(env) == _head(env.lap)


def test_uncommitted_edit_elsewhere_rides_along(env):
    _diverge(env)
    write(env.lap, "a.txt", "one\ntwo\nthree\nunsaved\n")
    res = _sync(env.lap)
    assert res["status"] == "synced" and res["pushed"] is True
    assert _read(env.lap, "a.txt").endswith("unsaved\n")
    assert "a.txt" in git(env.lap, "status", "--porcelain")


def test_overlapping_edit_is_named_and_saved_by_its_button(env):
    _elsewhere(env, "a.txt", "one\nTWO\nthree\n", "other mac")
    _commit(env.lap, "b.txt", "mine\n", "my edit")
    write(env.lap, "a.txt", "one\ntwo\nthree\nunsaved\n")
    head = _head(env.lap)
    res = _sync(env.lap)
    assert res["status"] == "skipped" and res["why"] == "edits"
    assert _head(env.lap) == head
    st = ownsync.status(env.lap)
    assert st["state"] == "edits" and st["files"] == ["a.txt"]
    assert st["fixes"] == ["save-edits"] and _plain(st["message"])
    write(env.lap, "a.txt", "one\ntwo\nthree\n")
    assert ownsync.status(env.lap)["state"] != "edits"     # undone by hand: over
    write(env.lap, "a.txt", "one\ntwo\nthree\nunsaved\n")
    out = ownsync.save_edits(env.lap)
    assert out["ok"], out
    assert _head(env.lap) == _remote(env)
    assert _read(env.lap, "a.txt") == "one\nTWO\nthree\nunsaved\n"
    assert git(env.lap, "status", "--porcelain").strip() == ""
    # Her own edits, saved in her GitHub name (TL ruling), not Fused's.
    saved = git(env.lap, "log", "-1", "--format=%an <%ae>|%cn", "--grep=Saved edits").strip()
    assert saved == "Alice <7+alice@users.noreply.github.com>|Alice"


def test_overlap_in_history_is_needs_merge_and_stands(env, monkeypatch):
    _elsewhere(env, "a.txt", "one\nTHEIRS\nthree\n", "other mac")
    head = _commit(env.lap, "a.txt", "one\nMINE\nthree\n", "my edit")
    res = _sync(env.lap)
    assert res["status"] == "skipped" and res["why"] == "needs-merge"
    assert _head(env.lap) == head and _read(env.lap, "a.txt") == "one\nMINE\nthree\n"
    st = ownsync.status(env.lap)
    assert st["state"] == "needs-merge" and st["files"] == ["a.txt"]
    assert "merge-claude" in st["fixes"] and _plain(st["message"])
    assert _sync(env.lap)["why"] == "needs-merge"   # standing: nothing recomputed
    from fused_render.contrib import merge_job
    seen = {}
    monkeypatch.setattr(merge_job, "start", lambda app_dir, **kw: seen.update(kw) or "j1")
    assert ownsync.start_merge(env.lap) == {"ok": True, "job": "j1"}
    assert seen == {"purpose": "own-sync", "ours": head, "theirs": _remote(env),
                    "identity": {"name": "Alice", "email": "7+alice@users.noreply.github.com"}}
    # The job landed: the state is over and the combined version publishes.
    ran = []
    monkeypatch.setattr(git_upstream, "schedule_sync",
                        lambda path, action, *, push=True, _runner=None: ran.append(push))
    ownsync.merge_landed(env.lap, old_tip=head, new_tip=head)
    assert ran == [True] and store.get(env.lap)["own_state"] is None
    assert ownsync.merge_plan(env.lap) is None


def test_merge_in_unpublished_edits_combines_instead(env):
    """U holds a merge (a pick would flatten it): the tip is merged in."""
    _elsewhere(env, "index.html", "<p>other</p>\n", "other mac")
    base = _head(env.lap)
    git(env.lap, "checkout", "-q", "-b", "side")
    _commit(env.lap, "s.txt", "side\n", "side")
    git(env.lap, "checkout", "-q", "main")
    _commit(env.lap, "b.txt", "mine\n", "mine")
    git(env.lap, "merge", "-q", "--no-ff", "-m", "join", "side")
    head = _head(env.lap)
    res = _sync(env.lap)
    assert res["status"] == "synced" and res["pushed"] is True
    new = _head(env.lap)
    assert git(env.lap, "rev-parse", f"{new}^1").strip() == head  # nothing rewritten
    assert replay.is_ancestor(env.lap, base, new)
    assert _remote(env) == new


# --------------------------------------------------------------- audit A


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _transcript(env, root, *, last="assistant", ended_ago=3.0):
    """A headless run's transcript for `root`: no `turn_duration` row; the
    last message is a finished reply (`assistant`) or a prompt (`user`)."""
    from fused_render import claude_session_move as csm
    bucket = os.path.join(csm.PROJECTS_DIR, csm.munge(root))
    os.makedirs(bucket, exist_ok=True)
    now = time.time()
    rows = [{"type": "user", "cwd": root, "sessionId": "s1", "timestamp": _iso(now - 30),
             "message": {"role": "user", "content": "make the wave red"}}]
    if last == "assistant":
        rows.append({"type": "assistant", "cwd": root, "sessionId": "s1",
                     "timestamp": _iso(now - ended_ago),
                     "message": {"role": "assistant",
                                 "content": [{"type": "text", "text": "Done."}]}})
    with open(os.path.join(bucket, "s1.jsonl"), "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")


def test_A_headless_turn_just_ended_still_publishes(env):
    """Audit A: the turn-end sync of a headless run (no turn_duration row,
    ended 3 s ago) used to defer as "claude" and nothing retried."""
    _transcript(env, env.lap, last="assistant", ended_ago=3.0)
    _diverge(env)
    res = _sync(env.lap)
    assert res["status"] == "synced" and res["pushed"] is True
    assert _remote(env) == _head(env.lap)


def test_A_open_turn_defers_the_move_never_a_plain_publish(env):
    _transcript(env, env.lap, last="user")
    assert ownsync._claude_busy(env.lap)
    head = _diverge(env)
    res = _sync(env.lap)
    assert res["status"] == "skipped" and res["why"] == "claude"
    assert _head(env.lap) == head
    assert ownsync.use_github(env.lap)["ok"] is False   # tree buttons wait too
    # Not diverged: the publish itself never waits for Claude.
    git(env.lap, "reset", "-q", "--keep", "origin/main")
    mine = _commit(env.lap, "c.txt", "c\n", "c")
    assert _sync(env.lap)["pushed"] is True and _remote(env) == mine


# --------------------------------------------------------------- audit B


def test_B1_rewrite_then_local_edits_has_a_way_out(env):
    """Audit B1: GitHub rewrote a published edit; three local edits after it
    were stuck forever with no button. Now: nothing moves on its own, the
    state names the fixes, and Put my edits on top publishes exactly the
    three onto GitHub's version (the reworded one is not brought back)."""
    _commit(env.lap, "x.txt", "x\n", "published edit")
    assert _sync(env.lap)["pushed"] is True
    gh_tip = _rewrite_elsewhere(env)
    mine = [_commit(env.lap, f"n{i}.txt", f"{i}\n", f"edit {i}") for i in range(3)]
    for _ in range(2):
        res = _sync(env.lap)
        assert res["status"] == "skipped" and res["why"] == "github-changed"
        assert _remote(env) == gh_tip and _head(env.lap) == mine[-1]
    assert git_upstream.sync_failures() == []
    st = ownsync.status(env.lap)
    assert st["state"] == "github-changed" and st["unpublished"] == 3
    assert st["fixes"] == ["use-github", "put-on-top", "publish-over"]
    assert _plain(st["message"])
    out = ownsync.put_on_top(env.lap)
    assert out["ok"], out
    tip = _remote(env)
    assert tip == _head(env.lap)
    assert git(env.lap, "rev-list", "--count", f"{gh_tip}..{tip}").strip() == "3"
    assert git(env.lap, "log", "-1", "--format=%s", gh_tip).strip() == \
        "published edit (reworded)"
    assert ownsync.status(env.lap)["state"] == "published"
    assert _activity(env.lap)[0]["undo"] is True


def test_B2_overlapping_edit_after_rewrite_carries_on(env):
    """Audit B2: the edits sentence said "Commit or undo them", which led
    into B1's dead end. Now github-changed names the fixes; Put my edits on
    top stops at the overlapping edit, names it, and Save my edits and
    continue finishes the job."""
    _commit(env.lap, "a.txt", "one\nTWO\nthree\n", "published")
    assert _sync(env.lap)["pushed"] is True
    git(env.other, "fetch", "-q", "origin")
    git(env.other, "reset", "-q", "--hard", "origin/main~1")
    _commit(env.other, "a.txt", "one\nTWO-v2\nthree\n", "rewritten")
    git(env.other, "push", "-q", "--force", "origin", "main")
    write(env.lap, "a.txt", "one\nTWO\nthree\nmine\n")
    res = _sync(env.lap)
    assert res["why"] == "github-changed"
    st = ownsync.status(env.lap)
    assert "commit" not in st["message"].lower() and _plain(st["message"])
    out = ownsync.put_on_top(env.lap)
    assert out["ok"] is False and out["state"] == "edits" and out["files"] == ["a.txt"]
    assert out["fixes"] == ["save-edits"]
    assert _sync(env.lap)["why"] == "edits"              # a background sync keeps it
    assert ownsync.status(env.lap)["state"] == "edits"
    out = ownsync.save_edits(env.lap)
    assert out["ok"], out
    assert _head(env.lap) == _remote(env)
    assert _read(env.lap, "a.txt") == "one\nTWO-v2\nthree\nmine\n"


def _new_repo_with_readme(e, path):
    git(str(e.tmp), "init", "-q", "--bare", "-b", "main", path)
    seed = str(e.tmp / "seed2")
    git(str(e.tmp), "init", "-q", "-b", "main", seed)
    write(seed, "README.md", "# new\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "Initial commit")
    git(seed, "push", "-q", path, "main")
    return _head(seed)


def test_B3_origin_pointed_at_a_new_repo_never_replaces_the_folder(env):
    _commit(env.lap, "x.txt", "x\n", "published")
    assert _sync(env.lap)["pushed"] is True
    newrepo = str(env.tmp / "new.git")
    readme = _new_repo_with_readme(env, newrepo)
    git(env.lap, "remote", "set-url", "origin", newrepo)
    head = _head(env.lap)
    res = _sync(env.lap)
    assert res["why"] == "github-changed" and _head(env.lap) == head
    assert os.path.exists(os.path.join(env.lap, "x.txt"))
    assert ownsync.put_on_top(env.lap)["ok"] is False      # nothing new to put on top
    assert _head(env.lap) == head
    out = ownsync.publish_over(env.lap)
    assert out["ok"], out
    assert git(newrepo, "rev-parse", "main").strip() == head
    pins = git(env.lap, "for-each-ref", "--format=%(objectname)",
               replay.BACKUP_PREFIX).split()
    assert readme in pins                                   # GitHub's old version kept


@pytest.mark.parametrize("flags_on", [True, False])
def test_B3b_repo_recreated_with_readme(env, flags_on):
    if not flags_on:
        _flags(env)
    _commit(env.lap, "x.txt", "x\n", "published")
    git(env.lap, "push", "-q", "origin", "main")
    git(env.lap, "fetch", "-q", "origin")
    shutil.rmtree(env.remote)
    _new_repo_with_readme(env, env.remote)
    head = _head(env.lap)
    res = _sync(env.lap)
    assert _head(env.lap) == head and os.path.exists(os.path.join(env.lap, "x.txt"))
    if flags_on:
        assert res["status"] == "skipped" and res["why"] == "github-changed"
    else:
        assert res["status"] == "failed" and res["failure"]["reason"] == "diverged"


def test_no_history_is_github_changed_and_put_on_top_works(env):
    """No reflog (expired, or never written) and HEAD lacks the tip: no
    evidence of what was published, so nothing moves on its own."""
    _diverge(env)
    log = os.path.join(env.lap, ".git", "logs", "refs", "remotes", "origin", "main")
    git(env.lap, "fetch", "-q", "origin")
    os.remove(log)
    res = _sync(env.lap)
    assert res["why"] == "github-changed"
    out = ownsync.put_on_top(env.lap)
    assert out["ok"], out
    assert _remote(env) == _head(env.lap) and _read(env.lap, "b.txt") == "mine\n"


def test_put_on_top_refuses_when_edits_hold_a_combined_version(env):
    _commit(env.lap, "x.txt", "x\n", "published")
    assert _sync(env.lap)["pushed"] is True
    _rewrite_elsewhere(env)
    git(env.lap, "checkout", "-q", "-b", "side")
    _commit(env.lap, "s.txt", "s\n", "side")
    git(env.lap, "checkout", "-q", "main")
    _commit(env.lap, "m.txt", "m\n", "m")
    git(env.lap, "merge", "-q", "--no-ff", "-m", "join", "side")
    head = _head(env.lap)
    assert _sync(env.lap)["why"] == "github-changed"
    out = ownsync.put_on_top(env.lap)
    assert out["ok"] is False and _plain(out["message"]) and _head(env.lap) == head


def test_use_github_backs_up_everything(env):
    _commit(env.lap, "x.txt", "x\n", "published")
    assert _sync(env.lap)["pushed"] is True
    gh_tip = _rewrite_elsewhere(env)
    mine = _commit(env.lap, "n.txt", "n\n", "edit")
    write(env.lap, "x.txt", "unsaved\n")          # overlaps: forces the hard move
    assert _sync(env.lap)["why"] == "github-changed"
    out = ownsync.use_github(env.lap)
    assert out["ok"], out
    assert _head(env.lap) == gh_tip and out["state"] == "published"
    line = _activity(env.lap)[0]
    assert line["undo"] is True and _plain(line["text"])
    entry = store.activity_entry(env.lap, line["id"])
    refs = entry["backup"]
    assert any(git(env.lap, "rev-parse", r).strip() == mine for r in refs)
    wip = [r for r in refs if r.endswith("-wip")]
    assert wip and git(env.lap, "show", f"{wip[0]}:x.txt") == "unsaved\n"


def test_undo_use_github_brings_back_edits_saved_and_not(env):
    _commit(env.lap, "x.txt", "x\n", "published")
    assert _sync(env.lap)["pushed"] is True
    gh_tip = _rewrite_elsewhere(env)
    mine = _commit(env.lap, "n.txt", "n\n", "edit")
    write(env.lap, "x.txt", "unsaved\n")
    write(env.lap, "new.txt", "brand new\n")       # untracked: stays where it is
    assert _sync(env.lap)["why"] == "github-changed"
    assert ownsync.use_github(env.lap)["ok"] and _head(env.lap) == gh_tip
    line = _activity(env.lap)[0]
    _transcript(env, env.lap, last="user")
    assert ownsync.restore(env.lap, line["id"])["ok"] is False   # Claude is working
    _transcript(env, env.lap, last="assistant")                 # the turn ended
    out =ownsync.restore(env.lap, line["id"])
    assert out["ok"], out
    assert _head(env.lap) == mine
    assert _read(env.lap, "x.txt") == "unsaved\n" and _read(env.lap, "new.txt") == "brand new\n"
    lines = _activity(env.lap)
    assert lines[0]["undo"] is True and _plain(lines[0]["text"])      # undo of the undo
    assert [ln["undo"] for ln in lines if ln["id"] == line["id"]] == [False]
    assert ownsync.restore(env.lap, line["id"])["ok"] is False        # only once
    assert _sync(env.lap)["why"] == "github-changed" and _head(env.lap) == mine


def test_undo_an_automatic_combine(env):
    head = _diverge(env)
    assert _sync(env.lap, push=False)["status"] == "synced"
    line = _activity(env.lap)[0]
    assert ownsync.restore(env.lap, line["id"])["ok"]
    assert _head(env.lap) == head


def test_publish_over_refuses_when_github_moved_again(env):
    _commit(env.lap, "x.txt", "x\n", "published")
    assert _sync(env.lap)["pushed"] is True
    _rewrite_elsewhere(env)
    _commit(env.lap, "n.txt", "n\n", "edit")
    assert _sync(env.lap)["why"] == "github-changed"
    later = _elsewhere(env, "z.txt", "z\n", "even later")
    out = ownsync.publish_over(env.lap)
    assert out["ok"] is False and _plain(out["message"])
    assert _remote(env) == later


# --------------------------------------------------------------- audit C


def test_C_one_diverged_sync_costs_few_processes(env, monkeypatch):
    for i in range(150):                 # 150 publishes elsewhere, each fetched here
        _elsewhere(env, "a.txt", f"{i}\n", f"o{i}")
        git(env.lap, "fetch", "-q", "origin")
    git(env.lap, "merge", "-q", "--ff-only", "origin/main")
    _diverge(env)
    calls = {"n": 0, "anc": 0}
    real = git_upstream._run

    def counting(root, *a, **k):
        calls["n"] += 1
        if a[:2] == ("merge-base", "--is-ancestor"):
            calls["anc"] += 1
        return real(root, *a, **k)

    monkeypatch.setattr(git_upstream, "_run", counting)
    with replay.slot():
        real(env.lap, "fetch", "--", "origin", "main")
        res = ownsync.resolve(env.lap, action="test", push=True)
    assert res["status"] == "synced" and res["pushed"] is True
    print("one diverged own sync:", calls)
    assert calls["anc"] <= 1 and calls["n"] <= 40, calls


def test_rewind_never_republishes_what_github_removed(env):
    _commit(env.lap, "x.txt", "x\n", "kept")
    assert _sync(env.lap)["pushed"] is True
    kept = _remote(env)
    removed = _commit(env.lap, "y.txt", "y\n", "removed later")
    assert _sync(env.lap)["pushed"] is True
    git(env.other, "fetch", "-q", "origin")
    git(env.other, "push", "-q", "--force", "origin", f"{kept}:refs/heads/main")
    res = _sync(env.lap)        # only ahead now: the hook's push call site
    assert res["status"] == "skipped" and res["why"] == "github-changed"
    assert git_upstream.sync_failures() == []
    assert _remote(env) == kept and _head(env.lap) == removed
    st = ownsync.status(env.lap)
    assert st["state"] == "github-changed" and st["fixes"] == ["use-github", "publish-over"]
    assert ownsync.use_github(env.lap)["ok"] and _head(env.lap) == kept


def test_plain_ahead_publishes_in_few_processes(env, monkeypatch):
    mine = _commit(env.lap, "c.txt", "c\n", "c")
    calls = []
    real = git_upstream._run

    def counting(root, *a, **k):
        calls.append(a[0])
        return real(root, *a, **k)

    with replay.slot():
        real(env.lap, "fetch", "--", "origin", "main")
        monkeypatch.setattr(git_upstream, "_run", counting)
        res = ownsync.resolve(env.lap, action="test", push=True)
    assert res["status"] == "synced" and res["pushed"] is True and _remote(env) == mine
    assert calls.count("push") == 1 and len(calls) - 1 <= 6, calls


# --------------------------------------------------------------- audit G


@pytest.mark.parametrize("key,value", [
    ("credential.helper", "osxkeychain"),
    ("credential.https://github.com.username", "alice"),
    ("url.git@github.com:.insteadOf", "https://github.com/"),
])
def test_G_own_git_config_never_stops_the_sync(env, key, value):
    git(env.lap, "config", key, value)
    _diverge(env)
    res = _sync(env.lap)
    assert res["status"] == "synced" and res["pushed"] is True


# --------------------------------------------------------------- Accept


@pytest.fixture
def world(tmp_path, monkeypatch):
    from _contrib_gh_world import make_world, git as wgit

    from fused_render.contrib import ai, inbox, jobs
    w = make_world(tmp_path, monkeypatch)
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(git_upstream, "_sync_failures", {})
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: True)
    monkeypatch.setattr(inbox, "_lists", {})
    monkeypatch.setattr(inbox, "_asked", {})
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(ai, "_run_cli", lambda system, prompt, *, model, timeout: json.dumps(
        {"verdict": "fine", "summary": "Bob's change.", "risks": []}))
    w.fake.sign_in("alice")
    w.gh.forget_identity()
    ws = tmp_path / "ws" / "app"
    ws.mkdir(parents=True)
    alice = str(ws / "sine-alice")
    wgit(tmp_path, "clone", "-q", "https://github.com/alice/sine", alice)
    store.put(alice, store.new_record("own", repo="alice/sine", branch="main"))
    w.alice = store.key(alice)
    return w


@pytest.mark.parametrize("method", ["merge", "squash", "rebase"])
def test_accept_keeps_and_publishes_the_authors_unpublished_edit(world, method):
    from _contrib_gh_world import PAGE, claude_commit, git as wgit, read

    from fused_render.contrib import inbox, jobs
    w = world
    w.fake.set_merge_methods("alice/sine", merge=method == "merge",
                             squash=method == "squash", rebase=method == "rebase")
    claude_commit(w.alice, {"app.py": "def amplitude():\n    return 1\n\n\n"
                                      "def frequency():\n    return 2\n\n# mine\n"},
                  "alice local, unpublished")
    w.fake.sign_in("bob")
    w.gh.forget_identity()
    fork = w.gh.ensure_fork("alice/sine")
    w.fake.commit(fork, "main", {"index.html": PAGE.replace("blue", "green")},
                  "Bob's change", author="bob")
    pr = w.gh.open_pr("alice/sine", title="Bob", body="b", head="bob:main", base="main")
    w.fake.sign_in("alice")
    w.gh.forget_identity()
    jobs.wait(inbox.review(w.alice, pr["number"]), 30)
    res = inbox.accept(w.alice, pr["number"])
    assert res["ok"] and res["merged"] and res["synced"], res
    tip = w.fake.tip("alice/sine")
    assert tip == wgit(w.alice, "rev-parse", "HEAD")
    assert "green" in read(w.alice, "index.html")
    assert "# mine" in wgit(w.alice, "show", f"{tip}:app.py")
    assert ownsync.status(w.alice)["state"] == "published"
