"""The `marketplace` kind (docs/CONTRIB_LOOP.md §18.4) against REAL git:
Alice's repo on the FakeGitHub, Bob's copy pinned to the approved commit of
its index row, the catalog a stand-in (`_contrib_gh_world.Catalog`) for
`fused_render.marketplace`'s snapshot. Every step that moves something
asserts the marketplace reading of I3 + I4 (`inv`): HEAD on fused/mine,
upstream = the mirror, the mirror == the approved commit — never the
author's tip (M1).

Covered: the pinned follow (Alice's pushes never reach Bob, not even on a
button), a release move with What's new + Undo + the hold until the next
release, a commit not on the author's branch, delisted (fresh vs stale
catalog), both switches + their Undo, adopting a legacy package install,
B5 (merged → "ships in the next release" → released + live), install at a
commit, the guard's kind and the Git panel's get-latest.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _contrib_gh_world import (  # noqa: E402
    PAGE, Catalog, assert_home_invariants, assert_market_invariants, claude_commit, edit, git,
    make_market_copy, make_world)

from fused_render import git_upstream  # noqa: E402
from fused_render.contrib import actions, classify, engine, follow, market, replay, store  # noqa: E402

REPO = "alice/sine"
HOME = classify.HOME_BRANCH


class _Proposals(types.ModuleType):
    """proposals.py's surface the engine calls, GitHub-true over the fake."""

    def __init__(self, w):
        super().__init__("fused_render.contrib.proposals")
        self.w = w

    def refresh(self, app_dir, *, force=False, tip=None):
        p = (store.get(app_dir) or {}).get("proposal")
        if not isinstance(p, dict):
            return None
        pr = self.w.gh.get_pr(p["repo"], p["number"])

        def fn(r):
            r["proposal"]["cache"] = {
                "state": "merged" if pr["merged"] else pr["state"],
                "merge_commit_sha": pr["merge_commit_sha"] if pr["merged"] else None,
                "head_sha": pr.get("head_sha")}
        store.update(app_dir, fn)
        return {}

    def settle_direct(self, app_dir, sha):
        pass

    def withdraw(self, app_dir):
        return {"ok": True}

    def merge_accepted(self, app_dir):
        pass

    def view(self, app_dir):
        return None

    def untracked(self, app_dir):
        return []


@pytest.fixture
def m(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    for name, value in (("_checked", {}), ("_state", {}), ("_sync_failures", {}),
                        ("_pulled_events", []), ("_pending", {})):
        monkeypatch.setattr(git_upstream, name, value)
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: True)
    monkeypatch.setattr(classify, "ensure_in_background", lambda app_dir: False)
    monkeypatch.setattr(engine, "_claude_busy", lambda root: False)
    engine._status_cache.clear()
    w = make_world(tmp_path, monkeypatch)
    fake = _Proposals(w)
    import fused_render.contrib as pkg
    monkeypatch.setitem(sys.modules, "fused_render.contrib.proposals", fake)
    monkeypatch.setattr(pkg, "proposals", fake, raising=False)
    w.cat = Catalog(tmp_path).install(monkeypatch)
    w.c1 = make_market_copy(w, w.cat)
    w.g = lambda *a, check=True: git(w.app, *a, check=check)
    w.sync = lambda: git_upstream.sync_repo(w.app, action="test", push=False)
    w.head = lambda: w.g("rev-parse", "HEAD")
    w.alice = lambda: w.fake.tip(REPO, "main")
    return w


def inv(w):
    assert_market_invariants(w.app)


def _alice(w, files, msg):
    return w.fake.commit(REPO, "main", files, msg, author="alice")


def _lines(w):
    return store.activity(w.app)


# ------------------------------------------------------------- pinned follow


def test_alice_pushing_never_moves_a_marketplace_copy(m):
    inv(m)
    c2 = _alice(m, {"NOTES.md": "n\n"}, "Unreviewed work")
    assert m.sync()["ok"]
    inv(m)
    assert m.head() == m.c1
    # The author's tip was fetched (objects, the ancestor check) — and
    # nothing else: the mirror and HEAD stay on the approved commit.
    assert m.g("rev-parse", "refs/remotes/origin/main") == c2
    assert engine.bring_in(m.app)["message"] == "You're on the reviewed version."
    assert m.head() == m.c1
    st = engine.status(m.app)
    assert st["kind"] == "marketplace" and st["state"] == "following" and st["behind"] == 0
    inv(m)


def test_a_release_moves_the_copy_with_whats_new_and_undo_holds_until_the_next(m):
    c2 = _alice(m, {"NOTES.md": "n\n"}, "Notes")
    m.cat.release("sine", c2, whats_new="Adds notes", at="2026-10-20T10:00:00Z")
    assert m.sync()["status"] == "synced"
    inv(m)
    assert m.head() == c2
    line = _lines(m)[0]
    assert line["text"] == "Updated to alice's release of Oct 20: Adds notes" and line["undo"]
    assert engine.restore(m.app, line["id"])["ok"]
    assert m.head() == m.c1 and store.get(m.app)["skip_tip"] == c2
    assert m.sync()["why"] == "kept-mine" and m.head() == m.c1
    inv(m)
    c3 = _alice(m, {"MORE.md": "m\n"}, "More")
    assert m.sync()["why"] == "kept-mine"          # Alice's push is not a release
    m.cat.release("sine", c3, whats_new="More things", at="2026-10-22T10:00:00Z")
    assert m.sync()["status"] == "synced" and m.head() == c3
    inv(m)
    assert store.get(m.app)["skip_tip"] is None


def test_a_commit_not_on_the_authors_branch_is_never_run(m):
    bare = m.fake.bare_path(REPO)
    tree = git(bare, "rev-parse", f"{m.c1}^{{tree}}")
    fork_only = git(bare, "-c", "user.name=x", "-c", "user.email=x@x", "commit-tree", tree,
                    "-p", m.c1, "-m", "fork only")
    git(bare, "update-ref", "refs/pull/9/head", fork_only)
    m.cat.release("sine", fork_only, whats_new="Sneaky")
    m.sync()
    assert m.head() == m.c1
    assert m.g("rev-parse", "refs/heads/main") == m.c1
    rec = store.get(m.app)
    assert rec["sync_error"]["reason"] == follow.NOT_ON_BRANCH
    assert "isn't in alice's app any more" in engine.status(m.app)["sync_error"]


def test_delisted_needs_a_fresh_catalog_and_keeps_the_last_version(m):
    _alice(m, {"NOTES.md": "n\n"}, "Notes")
    m.cat.delist("sine")
    m.cat.stale = True                        # a stale fallback never says "delisted"
    m.sync()
    assert (store.get(m.app)["listing"] or {}).get("status") == "live"
    m.cat.stale = False
    origin_before = m.g("rev-parse", "refs/remotes/origin/main")
    assert m.sync()["status"] in ("skipped", "synced")
    rec = store.get(m.app)
    assert rec["listing"]["status"] == "delisted"
    # No fetch, no move: it keeps working on its last reviewed version.
    assert m.g("rev-parse", "refs/remotes/origin/main") == origin_before
    assert m.head() == m.c1
    st = engine.status(m.app)
    assert st["state"] == "delisted"
    panel = actions.decide({**actions.facts(m.app), "flags": {"sources": True}})["panel"]
    assert panel["sentence"].startswith("No longer in the Community.")
    inv(m)


def test_offline_catalog_is_no_move(m):
    c2 = _alice(m, {"NOTES.md": "n\n"}, "Notes")
    m.cat.release("sine", c2)
    m.cat.down = True
    m.sync()
    assert m.head() == m.c1
    inv(m)


# ---------------------------------------------------------------- switching


def test_switch_to_link_follows_the_author_and_undo_switches_back(m):
    c2 = _alice(m, {"NOTES.md": "n\n"}, "Unreviewed")
    m.sync()
    out = market.switch_to_link(m.app)
    assert out["ok"], out
    rec = store.get(m.app)
    assert rec["kind"] == "github" and classify.kind_of(m.app) == "github"
    assert m.head() == c2
    assert_home_invariants(m.app)
    assert m.g("config", "--local", "fused.kind") == "github"
    assert m.g("config", f"branch.{HOME}.remote") == "origin"
    line = _lines(m)[0]
    assert line["kind"] == "switch" and line["undo"] and "unreviewed" in line["text"]
    back = engine.restore(m.app, line["id"])
    assert back["ok"], back
    assert classify.kind_of(m.app) == "marketplace" and m.head() == m.c1
    inv(m)
    assert not [x for x in _lines(m) if x["id"] == line["id"]][0]["undo"]


def test_switch_to_reviewed_refuses_changes_then_pins(m):
    out = market.switch_to_link(m.app)
    assert out["ok"]
    c2 = _alice(m, {"NOTES.md": "n\n"}, "Unreviewed")
    m.sync()
    assert m.head() == c2
    claude_commit(m.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    row = market.row_for("sine")
    refused = market.switch_to_reviewed(m.app, row)
    assert not refused["ok"] and refused["message"] == market.CHANGES_FIRST
    m.g("reset", "-q", "--hard", c2)
    out = market.switch_to_reviewed(m.app, row)
    assert out["ok"], out
    assert m.head() == m.c1 and classify.kind_of(m.app) == "marketplace"
    inv(m)
    line = _lines(m)[0]
    assert line["kind"] == "switch" and line["text"] == market.REVIEWED_TEXT
    assert engine.restore(m.app, line["id"])["ok"]
    assert classify.kind_of(m.app) == "github" and m.head() == c2
    assert_home_invariants(m.app)


# ------------------------------------------------------------------- legacy


def test_adopt_legacy_keeps_edits_and_data(m, tmp_path):
    # The release carries files the old package never shipped (dotted names
    # are dropped by the export): they must not read as the user's deletions.
    c2 = _alice(m, {".gitignore": "*.log\n", "docs/extra.md": "x\n"}, "Extras")
    m.cat.release("sine", c2)
    legacy = tmp_path / "ws" / "legacy-sine"
    legacy.mkdir(parents=True)
    (legacy / "index.html").write_text(PAGE.replace("blue", "green"), encoding="utf-8")
    (legacy / "app.py").write_text(m.fake.read(REPO, "app.py", m.c1), encoding="utf-8")
    (legacy / "data.csv").write_text("a,b\n", encoding="utf-8")
    os.makedirs(legacy / ".fused")
    (legacy / ".fused" / "marketplace.json").write_text("{}", encoding="utf-8")
    out = market.adopt_legacy(str(legacy), market.row_for("sine"))
    assert out["ok"], out
    root = str(legacy)
    assert not os.path.exists(legacy / ".fused" / "marketplace.json")
    assert git(root, "symbolic-ref", "HEAD") == f"refs/heads/{HOME}"
    assert git(root, "rev-parse", "HEAD") == c2
    status = sorted(s.strip() for s in git(root, "status", "--porcelain").splitlines())
    assert status == ["?? data.csv", "M index.html"], status
    assert (legacy / "index.html").read_text(encoding="utf-8") == PAGE.replace("blue", "green")
    assert (legacy / ".gitignore").read_text(encoding="utf-8") == "*.log\n"
    assert store.get(root)["kind"] == "marketplace"
    assert_market_invariants(root)
    again = market.adopt_legacy(str(legacy), market.row_for("sine"))
    assert not again["ok"]


# ---------------------------------------------------------------------- B5


def _propose(w):
    gh = w.gh
    head = w.head()
    fork = gh.ensure_fork(REPO)
    branch = "fused-sine-20261026-1200-ab12"
    gh.push(w.app, head, fork, branch)
    pr = gh.open_pr(REPO, title="Red", body="b", head=f"{fork.split('/')[0]}:{branch}",
                    base="main")

    def fn(r):
        r["proposal"] = {"repo": REPO, "number": pr["number"], "url": pr.get("url"),
                         "head_repo": fork, "head_branch": branch, "title": "Red",
                         "sent_at": 1.0, "settled_seen": False, "landed_at": None,
                         "cache": {"state": "open", "head_sha": head}}
    store.update(w.app, fn)
    return pr["number"]


@pytest.mark.parametrize("method", ["squash", "merge"])
def test_b5_merged_ships_in_the_next_release(m, method):
    claude_commit(m.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    mine = m.head()
    n = _propose(m)
    m.fake.merge(REPO, n, method)
    sys.modules["fused_render.contrib.proposals"].refresh(m.app)
    m.sync()
    assert m.head() == mine                          # nothing moves before a release
    inv(m)
    st = engine.status(m.app)
    assert st["state"] == "merged" and not st["released"]
    f = actions.facts(m.app)
    panel = actions.decide({**f, "flags": {"sources": True, "changes": True}})
    assert "It ships in alice's next release." in panel["panel"]["sentence"]
    assert not [a for a in panel["actions"] if a["id"] == "bring-in"]
    assert any(a["label"] == "Check for a new release" for a in panel["actions"])
    released = m.alice()
    m.cat.release("sine", released, whats_new="Red wave", credits=["bob"])
    assert m.sync()["status"] == "synced"
    inv(m)
    assert m.head() == released
    rec = store.get(m.app)
    assert rec["proposal"]["landed_at"] and rec["proposal"]["live"]
    assert any(x["text"] == "alice released your change. You're on the reviewed version."
               and x["undo"] for x in _lines(m))
    panel = actions.decide({**actions.facts(m.app), "flags": {"sources": True,
                                                               "changes": True}})
    assert "Your change is live for everyone." in panel["panel"]["sentence"]


# ------------------------------------------------------------ guard + panel


def test_guard_reports_the_kind_and_pull_is_always_the_engines(m):
    guard = replay._guard_module()

    def run(root, *args):
        r = replay.git(root, *args)
        return (r[0], r[1]) if r else (1, b"")
    who, unknown, kind = guard.guarded_who(m.app, run)
    assert who == REPO and kind == "marketplace" and not unknown
    assert guard.pull_blocked(m.app, run) == guard.MARKET_PULL_SENTENCE
    assert guard.ahead(m.app, run) == 0       # counted against the mirror


def _ops():
    import importlib.util
    here = os.path.join(os.path.dirname(_HERE), "fused_render", "templates", "git", "ops.py")
    spec = importlib.util.spec_from_file_location("git_ops_market_test", here)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _log():
    import importlib.util
    here = os.path.join(os.path.dirname(_HERE), "fused_render", "templates", "git", "log.py")
    spec = importlib.util.spec_from_file_location("git_log_market_test", here)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_git_panel_pull_is_refused_and_get_latest_brings_in_the_release(m, monkeypatch):
    from fused_render import app_git
    from fused_render.server.routers import git_upstream as route
    c2 = _alice(m, {"NOTES.md": "n\n"}, "Notes")
    m.g("fetch", "-q", "origin")             # a terminal / the panel's ⟳: origin moves only
    inv(m)
    got = _ops().main(m.app, op="pull")      # never fetches Alice's unreviewed tip
    assert got["ok"] is False and got["message"] == replay._guard_module().MARKET_PULL_SENTENCE
    assert m.head() == m.c1
    assert _log()._guard(m.app)["guard"] == {"kind": "marketplace"}
    monkeypatch.setattr(app_git, "app_dir_for", lambda p: m.app if p.startswith(m.app) else None)
    out = route.api_git_upstream_action({"action": "get-latest", "path": m.app}, x_fused="1")
    assert out["ok"] and m.head() == m.c1    # no release yet: nothing to bring in
    m.cat.release("sine", c2)
    out = route.api_git_upstream_action({"action": "get-latest", "path": m.app}, x_fused="1")
    assert out["ok"], out
    assert m.head() == c2
    inv(m)


# --------------------------------------------- the Marketplace switch alone


def test_marketplace_switch_alone_shows_a_marketplace_copys_ui(m, monkeypatch):
    """§18.8 (owner 2026-10-26): with the Marketplace switch on and the
    loop off, a marketplace copy's panel, Undo and card show; a GitHub-link
    copy's stay hidden; Propose/Remix still need the loop."""
    from _contrib_gh_world import write_prefs

    from fused_render import app_git
    from fused_render.contrib import flags, refresh
    from fused_render.server.routers import contrib as route
    write_prefs(contrib_enabled=False, contrib_remix_enabled=False, marketplace_enabled=True)
    assert flags.marketplace_ui() and not flags.sources()
    monkeypatch.setattr(app_git, "app_dir_for", lambda p: m.app if p.startswith(m.app) else None)
    c2 = _alice(m, {"NOTES.md": "n\n"}, "Notes")
    m.cat.release("sine", c2, whats_new="Adds notes", at="2026-10-20T10:00:00Z")
    assert m.sync()["status"] == "synced" and m.head() == c2
    page = route.api_contrib_app(path=m.app)
    assert page["ok"] and page["panel"]["kind"] == "marketplace", page
    ids = {a["id"] for a in page["actions"]}
    assert "check" in ids and "switch-link" in ids
    assert not ids & {"propose", "update-proposal", "withdraw", "remix"}
    line = page["activity"][0]
    assert line["undo"] and line["text"].startswith("Updated to alice's release")
    out = route.api_contrib_restore({"path": m.app, "id": line["id"]}, x_fused="1")
    assert out["ok"], out
    assert m.head() == m.c1
    inv(m)
    claude_commit(m.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    page = route.api_contrib_app(path=m.app)
    assert not {a["id"] for a in page["actions"]} & {"propose", "remix"}
    assert refresh.cards()[m.app]["label"] == "Community · reviewed"
    # A GitHub-link copy: the loop's own gate, unchanged.
    m.g("reset", "-q", "--hard", m.c1)
    assert market.switch_to_link(m.app)["ok"]
    assert route.api_contrib_app(path=m.app)["reason"] == "disabled"
    out = route.api_contrib_action({"path": m.app, "action": "check"}, x_fused="1")
    assert out["reason"] == "disabled"
    assert m.app not in refresh.cards()
    write_prefs(marketplace_enabled=False)
    assert not flags.marketplace_ui()


# ------------------------------------------------------------ install path


def test_install_at_the_approved_commit_never_checks_out_the_tip(m, tmp_path):
    from fused_render import appfile, appfile_git
    c2 = _alice(m, {"UNREVIEWED.md": "x\n"}, "Unreviewed")
    url = "https://github.com/alice/sine"
    staged, commit, _at = appfile_git.clone(url, "sine", commit=m.c1)
    assert commit == m.c1
    assert git(staged, "rev-parse", "HEAD") == m.c1
    assert not os.path.exists(os.path.join(staged, "UNREVIEWED.md"))
    dest = str(tmp_path / "ws" / "app" / "sine-market")
    appfile_git._place(staged, dest, park_existing=False, kind="marketplace")
    appfile_git._record_install(dest, url, kind="marketplace",
                                market={"slug": "sine",
                                        "approved": m.cat.rows["sine"]["releases"][0]})
    root = store.key(dest)
    assert_market_invariants(root)
    assert git(root, "config", "--local", "fused.kind") == "marketplace"
    assert git(root, "config", "--get", "remote.origin.pushurl") == classify.NO_PUSH_URL
    assert git_upstream.sync_repo(root, action="test", push=False)["ok"]
    assert git(root, "rev-parse", "HEAD") == m.c1 and not os.path.exists(
        os.path.join(root, "UNREVIEWED.md"))
    assert_market_invariants(root)
    # A GitHub link opened for it: the switch prompt, never a move.
    src = {"url": url, "commit": c2}
    assert appfile_git.probe(root, src) == appfile.STATE_SWITCH_LINK
    assert appfile_git.copy_view(root, src)["switch"] == (
        "Follow alice directly? You'll get every change, unreviewed.")
    target = {"git": src, "state": appfile.STATE_SWITCH_LINK, "path": root, "found_by": "git"}
    manifest = {"entry": "index.html"}
    with pytest.raises(appfile.InstallNeedsConfirm):
        appfile_git.install("x.fused", target, manifest, force=False)
    assert git(root, "rev-parse", "HEAD") == m.c1
    done = appfile_git.install("x.fused", target, manifest, force=True)
    assert done["action"] == "switched"
    assert classify.kind_of(root) == "github" and git(root, "rev-parse", "HEAD") == c2
    assert_home_invariants(root)


def test_install_refuses_a_fork_only_commit(m):
    from fused_render import appfile_git
    bare = m.fake.bare_path(REPO)
    tree = git(bare, "rev-parse", f"{m.c1}^{{tree}}")
    fork_only = git(bare, "-c", "user.name=x", "-c", "user.email=x@x", "commit-tree", tree,
                    "-p", m.c1, "-m", "fork only")
    git(bare, "update-ref", "refs/pull/7/head", fork_only)
    with pytest.raises(appfile_git.GitInstallError, match="reviewed version isn't in"):
        appfile_git.clone("https://github.com/alice/sine", "sine", commit=fork_only)
