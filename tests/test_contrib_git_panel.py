"""The Git panel's Get latest on a contributor's copy (CONTRIB_LOOP.md §16.8).

Bob proposes, Alice publishes more while the proposal is open, Bob brings
that in (a merge commit — his copy can never fast-forward again), Alice
accepts. The panel's Get latest must not end in "do it in a terminal": on a
guarded copy it runs the loop's own Bring in through the server, which
returns Bob to Alice's latest exactly as auto-sync would.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

from _contrib_gh_world import PAGE, git
from test_contrib_acceptance import (  # noqa: F401 — `world` is the fixture
    H, W, app_open, head, ids, louder, page, read, use, world, alice_publish,
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
    app_open(W.bob)  # row 11: a merge commit; Bob's main is now never fast-forwardable
    assert read(W.bob.app, "index.html") == PAGE.replace("blue", "green")
    assert git(W.bob.app, "rev-list", "--parents", "-1", "HEAD").count(" ") == 2
    tip = (accept_in_fused if via == "fused" else accept_on_github)(n, method=method)
    use(W.bob)
    git(W.bob.app, "fetch", "-q", "origin")
    # The plain pull the panel used to run: diverged, so it can only refuse.
    ops = _ops()
    got = ops.main(W.bob.app, op="pull")
    assert got["ok"] is False and got["reason"] == "follows-author", got
    assert "terminal" not in got["message"] and "git" not in got["message"], got
    got = ops.main(W.bob.app, op="reset", sha=tip)
    assert got["ok"] is False and got["reason"] == "follows-author", got
    assert head(W.bob.app) != tip
    # The button's new path: the loop's Bring in, through the server.
    out = _get_latest(W.bob)
    assert out.get("ok") is True, out
    assert "merged your change" in out["message"], out
    assert head(W.bob.app) == tip
    assert first_parent_subjects(W.bob.app, tip) == []
    assert page(W.bob)["panel"]["state"] == "merged"
    # Again: nothing to do, still plain words.
    out = _get_latest(W.bob)
    assert out.get("ok") is True and "already have" in out["message"], out


def test_get_latest_refuses_an_own_app(world):
    setup_world()
    out = _get_latest(W.alice)
    assert out == {"ok": False, "reason": "not-a-copy",
                   "message": "This only works for a copy of someone else's app."}
