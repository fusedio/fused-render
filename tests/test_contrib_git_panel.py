"""The Git panel on a contributor's copy (CONTRIB_LOOP.md §17.8).

Bob proposes, Alice publishes more while the proposal is open, Bob brings
that in (a merge commit on `fused/mine`), Alice accepts. The panel's plain
pull can no longer fast-forward (Bob's bring-in merge and Alice's accept
merge are siblings), so on a guarded copy it answers the loop's sentence,
never "do it in a terminal"; the button posts `/api/git-upstream`
get-latest, the loop's own Bring in, which settles Bob onto Alice's tip.
I3 and I4 (§17.1) hold at every step.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

from _contrib_gh_world import PAGE, assert_home_invariants, git
from test_contrib_acceptance import (  # noqa: F401 — `world` is the fixture
    ALL_OFF, H, W, app_open, head, ids, louder, page, read, use, world, alice_publish,
)
from test_contrib_permutations import (
    accept_in_fused, accept_on_github, first_parent_subjects, propose, setup_world,
)

REPO = "alice/sine"


def _ops():
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fused_render", "templates", "git", "ops.py")
    spec = importlib.util.spec_from_file_location("git_ops_under_test", here)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get_latest(m):
    use(m)
    r = W.client.post("/api/git-upstream", json={"action": "get-latest",
                                                 "path": os.path.join(m.app, "index.html")},
                      headers=H)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("via", ("fused", "github"))
@pytest.mark.parametrize("method", ("merge", "squash"))
def test_git_panel_get_latest_on_a_contributor_copy(world, method, via):
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    alice_publish({"index.html": PAGE.replace("blue", "green")}, "green")
    app_open(W.bob)  # bring-in: a merge commit on fused/mine
    assert_home_invariants(W.bob.app)
    assert read(W.bob.app, "index.html") == PAGE.replace("blue", "green")
    assert git(W.bob.app, "rev-list", "--parents", "-1", "HEAD").count(" ") == 2
    tip = (accept_in_fused if via == "fused" else accept_on_github)(n, method=method)
    use(W.bob)
    git(W.bob.app, "fetch", "-q", "origin")
    ops = _ops()
    # The plain pull: Bob's merge and Alice's accept merge are siblings, so
    # it cannot fast-forward; a guarded copy gets the loop's sentence.
    got = ops.main(W.bob.app, op="pull")
    assert got["ok"] is False and got["reason"] == "follows-author", got
    assert "terminal" not in got["message"] and "git" not in got["message"], got
    # Reset-to-commit is Bob's own on fused/mine (§17.8): not refused.
    assert ops._reset_blocked(W.bob.app) is None
    assert head(W.bob.app) != tip
    # The button's path: the loop's Bring in → settle onto Alice's tip.
    out = _get_latest(W.bob)
    assert out.get("ok") is True, out
    assert "merged your change" in out["message"], out
    assert head(W.bob.app) == tip
    assert_home_invariants(W.bob.app)
    assert first_parent_subjects(W.bob.app, tip) == []
    assert page(W.bob)["panel"]["state"] == "merged"
    from fused_render.contrib import store
    assert (store.get(W.bob.app) or {}).get("proposal", {}).get("landed_at")
    # Again: nothing to do, still plain words.
    out = _get_latest(W.bob)
    assert out.get("ok") is True and "already have" in out["message"], out
    assert_home_invariants(W.bob.app)


def test_get_latest_refuses_an_own_app(world):
    setup_world()
    out = _get_latest(W.alice)
    assert out == {"ok": False, "reason": "not-a-copy",
                   "message": "This only works for a copy of someone else's app."}


def test_get_latest_with_community_apps_off(world):
    """The switch is off on Bob's machine (the default): Get latest still
    moves — kind decides (P1). The bring-in lands Alice's accept (an
    empty-diff merge), the tree equals hers, and both invariants hold;
    whether the settle runs depends only on whether the proposal's state
    was read, never on the switch."""
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    W.bob.flags = dict(ALL_OFF)
    alice_publish({"index.html": PAGE.replace("blue", "green")}, "green")
    app_open(W.bob)
    assert_home_invariants(W.bob.app)
    tip = accept_on_github(n, method="merge")
    out = _get_latest(W.bob)
    assert out.get("ok") is True, out
    assert_home_invariants(W.bob.app)
    assert git(W.bob.app, "diff", "--name-only", tip, "HEAD").split() == []
    assert first_parent_subjects(W.bob.app, tip) == []
    out = _get_latest(W.bob)
    assert out.get("ok") is True and "already have" in out["message"], out
    assert_home_invariants(W.bob.app)
    assert _ops()._reset_blocked(W.bob.app) is None
