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
    # Bound by its copy id (§17.6: origin here is a local path, not the
    # repo's GitHub URL, so a row without it would be dropped as a
    # stranger's) — what the first settle's `_stamp_copy` writes.
    rec = store.new_record("own", repo=REPO, branch="main", classified_by="install")
    store.put(e.lap, rec)
    git(e.lap, "config", "--local", "fused.copyid", rec["copy_id"])
    # Fused has seen GitHub's version once (the app was opened after the
    # clone): what the rewind check vouches from. V3b tests the unseen case.
    ownsync.note_seen(e.lap)
    e.other = str(tmp_path / "other")
    git(str(tmp_path), "clone", "-q", e.remote, e.other)
    return e


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


def _verb(args):
    """A git argv with fused's `-c key=value` prefix (§16.12) stripped."""
    args = list(args)
    while args[:1] == ["-c"]:
        args = args[2:]
    return tuple(args) or ("",)


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


def test_B3b_repo_recreated_with_readme(env):
    _commit(env.lap, "x.txt", "x\n", "published")
    git(env.lap, "push", "-q", "origin", "main")
    git(env.lap, "fetch", "-q", "origin")
    shutil.rmtree(env.remote)
    _new_repo_with_readme(env, env.remote)
    head = _head(env.lap)
    res = _sync(env.lap)
    assert _head(env.lap) == head and os.path.exists(os.path.join(env.lap, "x.txt"))
    assert res["status"] == "skipped" and res["why"] == "github-changed"


def _no_reflog(e):
    git(e.lap, "config", "core.logAllRefUpdates", "false")
    log = os.path.join(e.lap, ".git", "logs", "refs", "remotes", "origin", "main")
    if os.path.exists(log):
        os.remove(log)


def _forget_seen(e):
    store.update(e.lap, lambda r: r.__setitem__("seen_tips", []))


def test_no_reflog_is_not_blind_with_tips_fused_saw(env):
    """§16.12: the GitHub tips fused saw are published points too."""
    _no_reflog(env)
    _diverge(env)
    res = _sync(env.lap)
    assert res["status"] == "synced" and res["pushed"] is True
    assert _remote(env) == _head(env.lap) and _read(env.lap, "b.txt") == "mine\n"


def test_V3_unknown_history_never_puts_a_removed_edit_back(env):
    """Audit V3: no reflog and nothing seen; GitHub removed a published K
    and this copy has K + a new edit. Put my edits on top used to pick K back
    onto GitHub. Now: not offered, and refused if called."""
    _no_reflog(env)
    _forget_seen(env)
    _commit(env.lap, "secret.txt", "API_KEY=sk\n", "add key")
    git(env.lap, "push", "-q", "origin", "main")
    git(env.other, "fetch", "-q", "origin")
    git(env.other, "reset", "-q", "--hard", "origin/main~1")
    _commit(env.other, "index.html", "<p>v2</p>\n", "fix")
    git(env.other, "push", "-q", "--force", "origin", "main")
    head = _commit(env.lap, "mine.txt", "m\n", "my edit")
    gh_tip = _remote(env)
    res = _sync(env.lap)
    assert res["why"] == "github-changed" and _head(env.lap) == head
    st = ownsync.status(env.lap)
    assert "put-on-top" not in st["fixes"] and _plain(st["message"])
    out = ownsync.put_on_top(env.lap)
    assert out["ok"] is False and _plain(out["message"])
    assert _remote(env) == gh_tip
    assert git(env.remote, "show", "main:secret.txt", check=False).strip() == ""


def test_V3b_unknown_rewind_holds_an_ahead_only_publish(env):
    """Audit V3b: no reflog and nothing seen; GitHub rewound under a
    published K; this copy is only ahead. The turn-end push used to
    republish K. Now it is held, with a sentence and buttons, until the
    person says Publish now."""
    _no_reflog(env)
    _forget_seen(env)
    kept = _remote(env)
    _commit(env.lap, "secret.txt", "API_KEY=sk\n", "add key")
    git(env.lap, "push", "-q", "origin", "main")
    git(env.other, "fetch", "-q", "origin")
    git(env.other, "push", "-q", "--force", "origin", f"{kept}:refs/heads/main")
    res = _sync(env.lap)
    assert res["status"] == "skipped" and res["why"] == "held" and _remote(env) == kept
    assert _sync(env.lap)["pushed"] is not True and _remote(env) == kept   # still held
    assert git_upstream.sync_failures() == []
    st = ownsync.status(env.lap)
    assert st["state"] == "unknown" and st["hold"] == "unknown" and st["fixes"] == ["publish-now", "use-github"]
    assert _plain(st["message"]) and st["unpublished"] == 1
    assert ownsync.publish_now(env.lap)["ok"]               # the person's go-ahead
    assert _remote(env) == _head(env.lap)
    assert ownsync.status(env.lap)["state"] == "published"


def test_fresh_clone_first_edit_publishes(env):
    """A plain clone of her own repo on a second Mac: nothing seen, no
    tracking reflog — but every new commit was made right here (HEAD's
    reflog), so the first Claude edit publishes, it is not held."""
    _forget_seen(env)
    log = os.path.join(env.lap, ".git", "logs", "refs", "remotes", "origin", "main")
    assert not os.path.exists(log)                 # a clone writes none
    mine = _commit(env.lap, "c.txt", "c\n", "Claude: c")
    res = _sync(env.lap)
    assert res["status"] == "synced" and res["pushed"] is True and _remote(env) == mine
    assert ownsync.hold_of(env.lap) is None


def test_fresh_clone_with_a_commit_from_elsewhere_stays_held(env):
    """U holds a commit this folder did not make (fetched in by hand, never
    seen published here): still unknown, held."""
    _forget_seen(env)
    git(env.other, "pull", "-q", "--ff-only", "origin", "main")
    foreign = _commit(env.other, "f.txt", "f\n", "made elsewhere")
    git(env.lap, "fetch", "-q", env.other, "main")
    git(env.lap, "merge", "-q", "--ff-only", foreign)
    res = _sync(env.lap)
    assert res["status"] == "skipped" and res["why"] == "held"
    assert ownsync.status(env.lap)["state"] == "unknown"


def test_held_after_upgrade_never_publishes_until_the_button(env):
    """§16.12: github→own upgrade with unpublished edits sets the hold
    (ws-guard, `ownsync.set_hold`); every automatic sync keeps it, Publish
    now ends it."""
    ownsync.set_hold(env.lap, "upgrade", count=1)
    before = _remote(env)
    _commit(env.lap, "c.txt", "c\n", "c")
    assert _sync(env.lap)["pushed"] is not True and _remote(env) == before
    _diverge(env)                                    # combining still happens
    res = _sync(env.lap)
    assert res["status"] == "synced" and res["pushed"] is False
    st = ownsync.status(env.lap)
    assert st["state"] == "held" and st["fixes"] == ["publish-now"]
    assert st["message"] == "This is your app. 2 edits are ready to publish."
    assert ownsync.publish_now(env.lap)["ok"] and _remote(env) == _head(env.lap)
    assert ownsync.hold_of(env.lap) is None


def test_R2_no_stuck_state_or_merge_ends_the_hold(env, monkeypatch):
    """Audit R2: a needs-merge used to overwrite the hold, and a later clean
    combine (or a landed Merge with Claude) then published the held edit
    without Publish now."""
    ownsync.set_hold(env.lap, "upgrade", count=1)
    _commit(env.lap, "a.txt", "one\nMINE\nthree\n", "held edit")
    _elsewhere(env, "a.txt", "one\nTHEIRS\nthree\n", "other mac")
    assert _sync(env.lap)["why"] == "needs-merge"
    assert ownsync.status(env.lap)["state"] == "needs-merge"   # its buttons first
    assert ownsync.hold_of(env.lap)["why"] == "upgrade"
    # A landed Merge with Claude schedules a publish: the hold still decides.
    monkeypatch.setattr(git_upstream, "schedule_sync",
                        lambda path, action, *, push=True, _runner=None:
                        git_upstream.sync_repo(path, action=action, push=push))
    ownsync.merge_landed(env.lap, old_tip=None, new_tip=None)
    _elsewhere(env, "a.txt", "one\ntwo\nthree\n", "other mac reverts")
    res = _sync(env.lap)
    assert res["status"] == "synced" and res["pushed"] is False
    assert "MINE" not in git(env.remote, "show", "main:a.txt")
    assert ownsync.status(env.lap)["state"] == "held"
    assert ownsync.publish_now(env.lap)["ok"]
    assert "MINE" in git(env.remote, "show", "main:a.txt")


def test_R2_save_my_edits_never_publishes_held_edits(env):
    """rev-sec R2: an overlapping unsaved edit's `edits` state used to replace
    the hold, and Save my edits and continue then published."""
    store.update(env.lap, lambda r: r.__setitem__(           # an earlier build's hold
        "own_state", {"state": "held", "why": "upgrade", "at": time.time()}))
    _commit(env.lap, "c.txt", "c\n", "held edit")
    theirs = _elsewhere(env, "a.txt", "one\nTHEIRS\nthree\n", "other mac")
    write(env.lap, "a.txt", "one\nDIRTY\nthree\n")
    assert _sync(env.lap)["why"] == "edits"
    assert ownsync.hold_of(env.lap)["why"] == "upgrade"     # moved, not lost
    out = ownsync.save_edits(env.lap)
    assert out["ok"] is False or out["state"] in ("held", "needs-merge")
    assert _remote(env) != _head(env.lap)
    assert git(env.remote, "show", "main:c.txt", check=False) == ""   # held edit stays here
    assert ownsync.hold_of(env.lap) is not None and _remote(env) == theirs


def test_use_github_drops_the_hold_with_the_edits(env):
    ownsync.set_hold(env.lap, "upgrade", count=1)
    _commit(env.lap, "c.txt", "c\n", "c")
    assert ownsync.use_github(env.lap)["ok"] and ownsync.hold_of(env.lap) is None


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


def test_V2_undo_of_an_automatic_combine_sticks_until_github_moves(env):
    """Audit V2: the next sync used to redo the combine the person undid."""
    head = _diverge(env)
    assert _sync(env.lap, push=False)["status"] == "synced"
    line = _activity(env.lap)[0]
    assert ownsync.restore(env.lap, line["id"])["ok"]
    assert _head(env.lap) == head
    for push in (False, True):                       # app open, turn end
        res = _sync(env.lap, push=push)
        assert res["status"] == "skipped" and res["why"] == "kept"
        assert _head(env.lap) == head
    assert ownsync.status(env.lap)["state"] == "kept"
    _elsewhere(env, "z.txt", "z\n", "GitHub moved again")
    assert _sync(env.lap)["status"] == "synced" and _head(env.lap) == _remote(env)
    assert _read(env.lap, "b.txt") == "mine\n" and _read(env.lap, "z.txt") == "z\n"


def test_R4_after_undo_the_panel_says_why_edits_wait(env):
    """Audit R4: after Undo new edits piled up unpublished under a vague
    "GitHub has a newer version". Now the sentence says why, and Bring in
    and publish ends it."""
    _diverge(env)
    assert _sync(env.lap, push=False)["status"] == "synced"
    assert ownsync.restore(env.lap, _activity(env.lap)[0]["id"])["ok"]
    later = _commit(env.lap, "later.txt", "l\n", "Claude: later")
    assert _sync(env.lap)["why"] == "kept" and _remote(env) != later
    st = ownsync.status(env.lap)
    assert st["state"] == "kept" and st["fixes"] == ["bring-in"]
    assert st["message"] == ("You undid bringing in GitHub's version. New edits wait "
                             "here until you bring it in again.")
    out = ownsync.bring_in(env.lap)
    assert out["ok"], out
    assert _remote(env) == _head(env.lap) and _read(env.lap, "later.txt") == "l\n"


def test_V2_check_now_brings_in_what_undo_set_aside(env):
    head = _diverge(env)
    assert _sync(env.lap, push=False)["status"] == "synced"
    assert ownsync.restore(env.lap, _activity(env.lap)[0]["id"])["ok"]
    assert _sync(env.lap, push=False)["why"] == "kept" and _head(env.lap) == head
    assert ownsync.check_now(env.lap)["ok"] and _head(env.lap) != head
    assert _read(env.lap, "b.txt") == "mine\n"


def _fake_job(monkeypatch, purpose):
    from fused_render.contrib import merge_job
    job = {"id": "j1", "status": "ready", "purpose": purpose, "files": ["index.html"]}
    monkeypatch.setattr(merge_job, "current", lambda app_dir, purpose=None: (
        dict(job) if purpose in (None, job["purpose"]) else None))


def test_V1_a_proposal_merge_never_stalls_the_authors_copy(env, monkeypatch):
    """Audit V1: an inbox Merge with Claude left `ready` used to stop every
    publish of the author's copy and paint her panel "merging"."""
    _fake_job(monkeypatch, "proposal")
    mine = _commit(env.lap, "c.txt", "c\n", "Claude: c")
    assert _sync(env.lap)["pushed"] is True and _remote(env) == mine
    assert ownsync.status(env.lap)["state"] == "published"
    _diverge(env)
    assert _sync(env.lap)["status"] == "synced" and _remote(env) == _head(env.lap)
    assert ownsync.status(env.lap)["state"] == "published"


def test_V1_own_merge_job_holds_moves_not_publishes(env, monkeypatch):
    _fake_job(monkeypatch, "own-sync")
    assert ownsync.status(env.lap)["state"] == "merging"
    mine = _commit(env.lap, "c.txt", "c\n", "Claude: c")
    assert _sync(env.lap)["pushed"] is True and _remote(env) == mine   # never waits
    head = _diverge(env)
    res = _sync(env.lap)
    assert res["why"] == "merge-job" and _head(env.lap) == head


def test_publish_over_confirm_says_what_comes_back():
    low = ownsync.PUBLISH_OVER_CONFIRM.lower()
    assert "removed from github" in low and "comes back" in low
    assert _plain(ownsync.PUBLISH_OVER_CONFIRM)


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
        if _verb(a)[:2] == ("merge-base", "--is-ancestor"):
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
    calls, plain = [], []
    real = git_upstream._run

    def counting(root, *a, **k):
        calls.append(_verb(a)[0])
        if a[:1] != ("-c",):
            plain.append(_verb(a)[0])
        return real(root, *a, **k)

    with replay.slot():
        real(env.lap, "fetch", "--", "origin", "main")
        monkeypatch.setattr(git_upstream, "_run", counting)
        res = ownsync.resolve(env.lap, action="test", push=True)
    assert res["status"] == "synced" and res["pushed"] is True and _remote(env) == mine
    assert calls.count("push") == 1 and len(calls) - 1 <= 6, calls
    # The publish runs as the author's own push would — her hooks, her
    # fsmonitor (P5); fused's private reads run with them off (§16.12).
    # (`symbolic-ref`/`rev-list` here are git_upstream's own helpers.)
    assert "push" in plain
    assert not [c for c in plain if c not in ("push", "symbolic-ref", "rev-list")], plain


def _refusing_hook(repo):
    """The author's own pre-push check (a secret scanner) that refuses."""
    path = os.path.join(repo, ".git", "hooks", "pre-push")
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\necho 'secret scanner: blocked' >&2\nexit 1\n")
    os.chmod(path, 0o755)


def _hook_stopped(st):
    assert st["state"] == "publish-hook" and st["fixes"] == ["publish-now"], st
    assert "own check" in st["message"] and _plain(st["message"])


@pytest.mark.parametrize("how", ["auto", "publish-now"])
def test_R3_authors_pre_push_check_runs_on_every_publish(env, how):
    """Audit R3: fused's own pushes ran with hooks off, so the author's secret
    scanner never saw them. Now it runs, and its refusal is a plain state."""
    _refusing_hook(env.lap)
    before = _remote(env)
    _commit(env.lap, "secret.txt", "AKIA...\n", "Claude: oops")
    if how == "auto":
        res = _sync(env.lap)
        assert res["status"] == "skipped" and res["why"] == "publish-hook"
    else:
        assert ownsync.publish_now(env.lap)["ok"] is False
    assert _remote(env) == before
    assert git_upstream.sync_failures() == []
    _hook_stopped(ownsync.status(env.lap))


def test_R3_pre_push_check_runs_for_put_on_top_and_publish_over(env):
    _commit(env.lap, "x.txt", "x\n", "published")
    assert _sync(env.lap)["pushed"] is True
    gh_tip = _rewrite_elsewhere(env)
    _commit(env.lap, "n.txt", "n\n", "edit")
    assert _sync(env.lap)["why"] == "github-changed"
    _refusing_hook(env.lap)
    out = ownsync.put_on_top(env.lap)
    assert out["ok"] is False and "own check" in out["message"] and _remote(env) == gh_tip
    _hook_stopped(ownsync.status(env.lap))
    out = ownsync.publish_over(env.lap)
    assert out["ok"] is False and "own check" in out["message"] and _remote(env) == gh_tip


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
