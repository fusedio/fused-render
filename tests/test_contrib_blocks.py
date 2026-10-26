"""The block list (docs/CONTRIB_LOOP.md §18.4 B4) against REAL git: a
marketplace copy of Alice's app (`test_contrib_market`'s world) running a
release Fused later blocked.

Covered: a hard block on a clean copy (reset to the newest safe release, the
app stopped, no Undo, the gate open again), on a copy with changes (the
revert as a merge keeps Bob's change), on overlapping changes
(`blocked-release`, Merge with Claude's plan, the gate shut), a release
built on a blocked commit never reached, Undo never landing on a blocked
commit, a soft block (updates pause, opening asks), a GitHub-link copy's
warning (no move), and offline keeping the last list.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _contrib_gh_world import (  # noqa: E402
    APP_PY, PAGE, Catalog, assert_home_invariants, assert_market_invariants, claude_commit,
    git, make_world, read)
from test_contrib_market import _Proposals, _alice  # noqa: E402

from fused_render import git_upstream  # noqa: E402
from fused_render.contrib import actions, blocks, classify, engine, store  # noqa: E402

REPO = "alice/sine"
RED = PAGE.replace("blue", "red")


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
    from _contrib_gh_world import make_market_copy
    w.c1 = make_market_copy(w, w.cat)
    w.stopped = []
    from fused_render import background_apps
    monkeypatch.setattr(background_apps, "stop_and_disable",
                        lambda folder: w.stopped.append(folder) or False)
    w.g = lambda *a, check=True: git(w.app, *a, check=check)
    w.sync = lambda: git_upstream.sync_repo(w.app, action="test", push=False)
    w.head = lambda: w.g("rev-parse", "HEAD")
    return w


def _release(m, files, msg, *, at="2026-10-20T10:00:00Z"):
    sha = _alice(m, files, msg)
    m.cat.release("sine", sha, whats_new=msg, at=at)
    assert m.sync()["status"] == "synced"
    assert m.head() == sha
    return sha


def _block(m, sha, level="hard"):
    m.cat.block("sine", sha, level=level)
    assert blocks.refresh()


def inv(m):
    assert_market_invariants(m.app)


def _rollback_lines(m):
    return [x for x in store.activity(m.app) if x["kind"] == "rollback"]


def test_hard_block_on_a_clean_copy_rolls_back_without_undo(m):
    c2 = _release(m, {"index.html": RED}, "Red")
    _block(m, c2)
    assert blocks.gate(m.app) is not None             # before enforcement caught up
    out = blocks.enforce(m.app)
    assert out["action"] == "rolled-back"
    assert m.head() == m.c1 and read(m.app, "index.html") == PAGE
    inv(m)
    assert m.stopped == [m.app]
    line = _rollback_lines(m)[0]
    assert not line["undo"]
    assert line["text"].startswith("Fused blocked the version you were running: "
                                   "It read saved passwords. Rolled back to the release of Oct 1.")
    assert line["text"].endswith("Change your passwords.")
    assert blocks.gate(m.app) is None                  # rolled back: it opens again
    st = engine.status(m.app)
    assert st["block"]["level"] == "hard" and st["block"]["rolled_back_to"] == m.c1
    # The next sync never moves back to the blocked release.
    m.sync()
    assert m.head() == m.c1
    inv(m)


def test_hard_block_on_a_copy_with_changes_reverts_as_a_merge(m):
    c2 = _release(m, {"NOTES.md": "secret reader\n"}, "Notes")
    mine = claude_commit(m.app, {"app.py": APP_PY.replace("return 1", "return 7")}, "loud")
    _block(m, c2)
    out = blocks.enforce(m.app)
    assert out["action"] == "rolled-back", out
    assert not os.path.exists(os.path.join(m.app, "NOTES.md"))     # the blocked change is gone
    assert "return 7" in read(m.app, "app.py")                      # Bob's is kept
    parents = m.g("rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert parents == [mine, m.c1]
    inv(m)
    assert blocks.gate(m.app) is None
    # What reads as Bob's change is his file alone: the blocked release is
    # taken back by the rollback merge (whose own commit never counts).
    st = engine.status(m.app)
    assert st["changes_files"] == ["app.py"] and st["state"] == "changes"
    # Enforcing again changes nothing: the blocked commit is rolled back in HEAD.
    assert blocks.enforce(m.app) is None or m.head() == git(m.app, "rev-parse", "HEAD")
    assert len(_rollback_lines(m)) == 1


def test_hard_block_overlapping_changes_waits_for_merge_with_claude(m):
    c2 = _release(m, {"index.html": RED}, "Red")
    claude_commit(m.app, {"index.html": RED.replace("red", "crimson")}, "crimson")
    head = m.head()
    _block(m, c2)
    out = blocks.enforce(m.app)
    assert out["action"] == "conflict"
    assert m.head() == head                          # nothing moved in the folder
    assert m.g("rev-parse", "refs/heads/main") == m.c1    # the mirror did (M1)
    gate = blocks.gate(m.app)
    assert gate and gate["blocked"]["conflict"]
    st = engine.status(m.app)
    assert st["state"] == "blocked-release"
    plan = engine.merge_plan(m.app)
    assert plan["purpose"] == "update" and plan["theirs"] == m.c1 and plan["base"] == c2
    decided = actions.decide({**actions.facts(m.app), "flags": {"sources": True}})
    assert decided["panel"]["state"] == "blocked-release"
    assert decided["actions"][0]["id"] == "merge-claude"
    # Claude's merge landing is the rollback: the gate opens.
    engine.merge_landed(m.app, purpose="update", old_tip=head, new_tip=head)
    assert blocks.gate(m.app) is None


def test_a_release_built_on_a_blocked_commit_is_never_reached(m):
    c2 = _alice(m, {"index.html": RED}, "Red")
    c3 = _alice(m, {"NOTES.md": "n\n"}, "Notes on top")
    m.cat.release("sine", c2, at="2026-10-10T10:00:00Z")
    m.cat.release("sine", c3, at="2026-10-12T10:00:00Z")
    _block(m, c2)
    m.sync()
    assert m.head() == m.c1                          # c3 contains c2: not safe
    inv(m)
    assert store.get(m.app)["approved"]["commit"] == m.c1
    m.sync()                                         # and the catalog read keeps it so
    assert m.head() == m.c1 and store.get(m.app)["approved"]["commit"] == m.c1


def test_undo_never_lands_on_a_blocked_commit(m):
    c2 = _release(m, {"index.html": RED}, "Red", at="2026-10-10T10:00:00Z")
    c3 = _release(m, {"NOTES.md": "n\n"}, "Notes", at="2026-10-12T10:00:00Z")
    line = store.activity(m.app)[0]                  # c3's release: backup = c2
    assert line["undo"]
    _block(m, c2)
    assert actions.activity(m.app)[0]["undo"] is False or not [
        x for x in actions.activity(m.app) if x["id"] == line["id"] and x["undo"]]
    refused = engine.restore(m.app, line["id"])
    assert not refused["ok"] and refused["reason"] == "blocked"
    assert m.head() == c3


def test_soft_block_pauses_updates_and_asks_before_opening(m):
    c2 = _release(m, {"index.html": RED}, "Red")
    _block(m, c2, level="soft")
    out = blocks.enforce(m.app)
    assert out == {"level": "soft", "action": "marked"}
    assert m.head() == c2 and blocks.gate(m.app) is None
    assert engine.status(m.app)["state"] == "soft-blocked"
    decided = actions.decide({**actions.facts(m.app), "flags": {"sources": True}})
    assert decided["actions"][0]["id"] == "confirm-block"
    c3 = _alice(m, {"NOTES.md": "n\n"}, "Notes")
    m.cat.release("sine", c3)
    assert m.sync()["why"] == "soft-blocked" and m.head() == c2
    blocks.confirm_soft(m.app)
    assert engine.status(m.app)["state"] != "soft-blocked"
    from fused_render.contrib import refresh
    card = refresh._card(store.key(m.app), store.get(m.app), False)
    assert card["kind"] == "marketplace" and card["block"]["confirmed"]


def test_a_github_link_copy_only_gets_a_warning(m):
    from fused_render.contrib import market
    assert market.switch_to_link(m.app)["ok"]
    head = m.head()
    _block(m, m.c1)
    blocks.enforce_all()
    assert m.head() == head
    assert_home_invariants(m.app)
    rec = store.get(m.app)
    assert rec["block_warn"]["commit"] == m.c1 and not rec.get("block")
    assert blocks.gate(m.app) is None and m.stopped == []
    decided = actions.decide({**actions.facts(m.app), "flags": {"sources": True}})
    assert "You follow alice directly, so nothing was changed." in decided["panel"]["sentence"]


def test_offline_keeps_the_last_list(m):
    c2 = _release(m, {"index.html": RED}, "Red")
    _block(m, c2)
    m.cat.down = True
    assert blocks.refresh() is False
    assert blocks.is_blocked_commit("sine", c2)
    assert blocks.enforce(m.app)["action"] == "rolled-back"
    inv(m)


def test_the_open_sync_enforces_and_a_safe_release_ends_the_notice(m):
    """The product trigger: an app open's sync (`engine.sync` → `enforce`
    before the fetch), not a direct call."""
    c2 = _release(m, {"index.html": RED}, "Red")
    _block(m, c2)
    m.sync()
    assert m.head() == m.c1 and m.stopped == [m.app]
    inv(m)
    assert store.get(m.app)["block"]["rolled_back_to"] == m.c1
    m.sync()
    assert store.get(m.app)["block"]                 # the notice stands meanwhile
    # A fixed release NOT built on the blocked one (Alice reverted it).
    git(m.fake.bare_path(REPO), "update-ref", "refs/heads/main", m.c1)
    c3 = _alice(m, {"NOTES.md": "fixed\n"}, "Fixed")
    m.cat.release("sine", c3, at="2026-10-26T10:00:00Z")
    m.sync()
    assert m.head() == c3
    inv(m)
    m.sync()
    assert store.get(m.app)["block"] is None         # moved on: the notice has done its job


def test_no_safe_release_left_never_moves_even_briefly(m):
    """M1: the installed release gets blocked and the only newer one is built
    on it — the sync moves nothing (enforcement stops the app instead)."""
    c2 = _alice(m, {"NOTES.md": "n\n"}, "Built on the blocked one")
    m.cat.release("sine", c2)
    m.cat.block("sine", m.c1)
    assert blocks.refresh()
    m.sync()
    assert m.head() == m.c1 and m.g("rev-parse", "refs/heads/main") == m.c1
    assert engine.status(m.app)["state"] == "blocked-release"
    assert store.get(m.app)["block"]["no_target"]
    assert blocks.gate(m.app) is not None


def test_blocked_app_tools_leave_the_bots_registry(m):
    c2 = _release(m, {"index.html": RED}, "Red")
    claude_commit(m.app, {"index.html": RED.replace("red", "crimson")}, "crimson")
    _block(m, c2)
    blocks.enforce(m.app)
    assert m.app in blocks.blocked_dirs()
    from fused_render.bots import apptools
    assert m.app in apptools._blocked_dirs()
