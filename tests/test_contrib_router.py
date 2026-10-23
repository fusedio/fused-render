"""Tests for the contribution loop's HTTP surface (server/routers/contrib.py),
its listing/background side (contrib/refresh.py) and the contributor share
link (share_app.github_link_state).

A bare FastAPI app carrying just the routers under test (the
test_server_github.py pattern), the REAL record store, flags and job
registry under a per-test home and workspace, and stand-ins for the modules
whose behaviour belongs to other test files (engine, classify, proposals,
merge_job, remix, contributors, inbox): installed into `sys.modules`, which
is what the router's lazy `_mod()` imports resolve to. A stand-in that must
never be reached (GitHub on a read path, git on the listing) raises. The
double-click test runs the REAL `proposals.start_send` (only `send` and the
GitHub identity stubbed), since the dedupe it pins spans router and module.

What every route must get right, and what is pinned here: the X-Fused
guard, the workspace-app containment rule, the phase flag checked BEFORE
any classification, the record-kind check, plain refusals, one job for a
double-clicked Send, reads that never wait on GitHub, and a listing that
gains `contrib` only while the sources phase is on — and pays no git for it.
"""
import os
import subprocess
import sys
import threading
import types

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import fused_render.contrib as contrib_pkg
from fused_render.contrib import flags, jobs, refresh, store

H = {"X-Fused": "1"}


def _boom(*_a, **_k):
    raise AssertionError("must not be called on this path")


def _started(kind):
    """A `start_*` stand-in: a real job of `kind` around `fn`."""
    def start(app_dir, fn):
        return {"ok": True, "job": jobs.start(kind, store.key(app_dir), fn)}
    return start


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, ws = tmp_path / "home", tmp_path / "ws"
    home.mkdir()
    ws.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    on = {"sources": True, "changes": True, "inbox": True, "loop": True, "remix": True}
    monkeypatch.setattr(flags, "_raw", lambda: dict(on))
    monkeypatch.setattr(flags, "effective", lambda raw: dict(raw))  # five phases, as faked
    monkeypatch.setattr(refresh, "_INLINE", True)
    refresh._reset()

    from fused_render import github_setup
    monkeypatch.setattr(github_setup, "cached_summary",
                        lambda: {"signed_in": True, "account": "bob", "stale": False})
    monkeypatch.setattr(github_setup, "summary", _boom)

    calls = []

    def stub(name, **fns):
        mod = types.ModuleType(f"fused_render.contrib.{name}")
        for k, v in fns.items():
            setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
        # `from fused_render.contrib import <name>` reads the PACKAGE
        # attribute once the real module was ever imported (an earlier test
        # file), ignoring sys.modules — replace both.
        monkeypatch.setattr(contrib_pkg, name, mod, raising=False)
        return mod

    def ensure(app_dir, **_):
        calls.append(("ensure", app_dir))
        return store.get(app_dir)

    ns = types.SimpleNamespace(home=home, ws=ws, flags=on, calls=calls, stub=stub)
    ns.classify = stub("classify", ensure=ensure,
                       kind_of=lambda d, **_: (store.get(d) or {}).get("kind"))
    ns.engine = stub("engine", status=lambda d: {"kind": "github", "state": "changes",
                                                   "changes": 2, "unproposed": 1,
                                                   "changes_files": ["a.py", "b.py"],
                                                   "needs_merge": False, "merge_job": None,
                                                   "can_push": False, "pending": False},
                     discard=lambda d, withdraw=False: {"ok": True, "withdrew": withdraw},
                     push_to_main=lambda d: {"ok": False, "reason": "forbidden",
                                             "message": "Propose them instead."},
                     bring_in=_boom, merge_plan=lambda d: None)
    draft = _started("propose-draft")
    ns.proposals = stub("proposals",
                        refresh=lambda d, force=False: calls.append(("prefresh", force)),
                        view=lambda d: {"number": 4, "state": "waiting"},
                        untracked=lambda d: ["new.csv"],
                        start_draft=lambda d: draft(d, lambda: {"title": "T", "description": "D"}),
                        start_send=_boom, start_update=_boom,
                        withdraw=lambda d: {"ok": True})
    ns.remix = stub("remix", check_original=lambda d: calls.append(("original", d)),
                    start_draft=_boom, start_publish=_boom,
                    start_bring_in=lambda d: {"ok": True, "job": "mj-1"})
    ns.contributors = stub("contributors",
                           list=lambda d: [{"login": "bob", "name": "Bob", "commits": 2}])
    ns.merge_job = stub("merge_job", get=lambda i: None,
                        current=lambda d, purpose=None: None,
                        for_app=lambda d, purpose=None: None,
                        start=_boom, accept=_boom, discard=_boom)
    ns.ownsync = stub("ownsync", status=lambda d: {"state": "published", "unpublished": 0,
                                                   "github_new": 0, "files": [],
                                                   "message": "Saved on GitHub.", "fixes": []},
                      start_merge=lambda d: {"ok": False,
                                             "message": "There's nothing to combine right now."},
                      check_now=_boom, publish_now=_boom,
                      use_github=_boom, put_on_top=_boom, publish_over=_boom,
                      save_edits=_boom)
    ns.inbox = stub("inbox",
                    list=lambda d, review=True, force=False: (
                        calls.append(("ilist", review)) or [{"number": 7}]),
                    cached=lambda d, **_: [{"number": 7, "title": "x"}], invalidate=lambda d: None,
                    refreshing=lambda d: False,
                    review=lambda d, n: "rv-1",
                    try_open=lambda d, n, confirm=False: {"ok": True, "path": "/p", "url": "/u",
                                                          "confirm": confirm},
                    try_remove=lambda d, n: {"ok": True}, accept=_boom,
                    owns_merge=lambda job: False,
                    decline=lambda d, n, note="": {"ok": True, "note": note})

    def app(name, kind="github", **extra):
        d = ws / "tag" / name
        d.mkdir(parents=True)
        (d / "index.html").write_text('<meta name="fused-app">hi')
        if kind is not None:
            repo = extra.pop("repo", "alice/x" if kind == "github" else None)
            rec = store.new_record(kind, repo=repo, branch="main", classified_by="migration")
            rec.update(extra)
            store.put(str(d), rec)
        return str(d)

    ns.app = app
    fapp = FastAPI()
    from fused_render.server.routers.apps import router as apps_router
    from fused_render.server.routers.contrib import router
    fapp.include_router(router)
    fapp.include_router(apps_router)
    ns.client = TestClient(fapp)
    yield ns
    refresh._reset()


def _post(env, route, **body):
    return env.client.post(f"/api/contrib/{route}", json=body, headers=H).json()


def _job(env, job_id):
    jobs.wait(job_id)
    return env.client.get("/api/contrib/job", params={"id": job_id}).json()


# -- the gate -------------------------------------------------------------------


def test_post_without_x_fused_is_refused(env):
    d = env.app("a")
    r = env.client.post("/api/contrib/propose/draft", json={"path": d})
    assert r.status_code == 403
    assert env.calls == []


def test_path_outside_the_workspace_is_refused(env, tmp_path):
    other = tmp_path / "elsewhere" / "tag" / "a"
    other.mkdir(parents=True)
    for path in (str(other), "relative/path", "", str(env.ws)):
        out = _post(env, "propose/draft", path=path)
        assert out["ok"] is False and out["reason"] == "not-an-app"
    assert env.calls == []


def test_phase_off_answers_disabled_before_classifying(env):
    d = env.app("a")
    env.flags["changes"] = False
    out = _post(env, "propose/draft", path=d)
    assert out == {"ok": False, "reason": "disabled", "message": "This isn't turned on."}
    assert env.calls == []  # nothing was classified


def test_wrong_kind_is_refused_in_plain_words(env):
    d = env.app("mine", kind="own", repo="bob/mine")
    out = _post(env, "propose/draft", path=d)
    assert out["ok"] is False and out["reason"] == "not-allowed"
    g = env.app("theirs")
    assert _post(env, "inbox/review", path=g, number=1)["reason"] == "not-allowed"
    own_no_repo = env.app("local", kind="own", repo=None)
    assert _post(env, "inbox/review", path=own_no_repo, number=1)["reason"] == "not-allowed"
    assert _post(env, "remix/bring-in", path=d)["reason"] == "not-allowed"  # not a remix


def test_a_path_inside_the_app_resolves_to_the_app(env):
    d = env.app("a")
    out = _post(env, "propose/draft", path=os.path.join(d, "index.html"))
    assert out["ok"] is True
    assert _job(env, out["job"])["result"] == {"title": "T", "description": "D"}


# -- proposals ------------------------------------------------------------------


def test_double_clicked_send_runs_one_job(env, monkeypatch):
    """Through the REAL proposals.start_send: one proposal job per app."""
    import importlib

    from fused_render.contrib import gh
    monkeypatch.delitem(sys.modules, "fused_render.contrib.proposals")  # the stand-in
    proposals = importlib.import_module("fused_render.contrib.proposals")
    monkeypatch.setattr(gh, "identity", lambda **_: {"login": "bob"})
    d = env.app("a")
    gate, sent = threading.Event(), []

    def send(app_dir, *, title, description, note=None, **_):
        sent.append(title)
        gate.wait(5)
        return {"number": 3}

    monkeypatch.setattr(proposals, "send", send)
    a = _post(env, "propose/send", path=d, title="Brighter", description="", note="")
    b = _post(env, "propose/send", path=d, title="Brighter", description="", note="")
    gate.set()
    assert a["ok"] and a["job"] == b["job"]
    assert _job(env, a["job"])["status"] == "done"
    assert sent == ["Brighter"]


def test_start_refusals_pass_through_untouched(env):
    d = env.app("a")
    assert _post(env, "propose/send", path=d, title="")["reason"] == "missing"
    signed_out = {"ok": False, "reason": "signed-out",
                  "message": "Sign in to GitHub to send your proposal."}
    env.proposals.start_send = lambda app_dir, **_: signed_out
    assert _post(env, "propose/send", path=d, title="x") == signed_out


def test_a_github_failure_fails_the_job_with_its_plain_sentence(env):
    from fused_render.contrib.gh import GitHubError
    d = env.app("a")

    def send():
        raise GitHubError("forbidden", "GitHub didn't let you do that.")

    env.proposals.start_send = lambda app_dir, **_: _started("propose-send")(app_dir, send)
    out = _job(env, _post(env, "propose/send", path=d, title="x")["job"])
    assert out["status"] == "failed"
    assert out["message"] == "GitHub didn't let you do that."
    assert "step" in out and "reason" in out


def test_an_unexpected_exception_never_reaches_the_page(env):
    d = env.app("a", proposal={"number": 1, "cache": {"state": "open"}})

    def withdraw(app_dir):
        raise RuntimeError("fatal: refusing to merge unrelated histories")

    env.proposals.withdraw = withdraw
    out = _post(env, "propose/withdraw", path=d)
    assert out == {"ok": False, "reason": "failed", "message": "Something went wrong. Try again."}
    assert out["message"] == jobs.GENERIC_MESSAGE


def test_a_remix_may_withdraw_but_not_update(env):
    remix = env.app("r", kind="own", repo="bob/r", remix_of={"repo": "alice/x", "branch": "main"},
                    proposal={"number": 2, "cache": {"state": "open"}})
    assert _post(env, "propose/withdraw", path=remix) == {"ok": True}
    assert _post(env, "propose/update", path=remix)["reason"] == "not-allowed"
    settled = env.app("s", proposal={"number": 3, "cache": {"state": "merged"}})
    assert _post(env, "propose/withdraw", path=settled)["reason"] == "no-proposal"
    assert _post(env, "propose/withdraw", path=env.app("n"))["reason"] == "no-proposal"


def test_update_copy_answers_the_engine(env):
    d = env.app("a")
    env.engine.bring_in = lambda app_dir: {"ok": True, "message": "Up to date."}
    assert _post(env, "update-copy", path=d) == {"ok": True, "message": "Up to date."}
    own = env.app("m", kind="own", repo="bob/m")
    assert _post(env, "update-copy", path=own)["reason"] == "not-allowed"
    env.flags["changes"] = False
    assert _post(env, "update-copy", path=d)["reason"] == "disabled"


def test_discard_and_push_main_answer_the_engine(env):
    d = env.app("a")
    assert _post(env, "discard", path=d) == {"ok": True, "withdrew": False}
    assert _post(env, "discard", path=d, withdraw=True)["withdrew"] is True
    assert _post(env, "push-main", path=d)["reason"] == "forbidden"


# -- v2: the one action route ---------------------------------------------------


def _v2(state="changes", **over):
    """A v2 engine.status for a GitHub-link copy."""
    files = ["a.py"] if state == "changes" else []
    s = {"kind": "github", "state": state, "who": "alice", "author": "alice",
         "repo": "alice/x", "behind": 0, "changes_files": files, "changes": len(files),
         "dirty": False, "unproposed": 0, "needs_merge": state == "needs-merge",
         "needs_merge_purpose": "update" if state == "needs-merge" else None,
         "needs_merge_files": [], "merge_job": None, "blocked_files": [],
         "kept_mine": False, "off_default": False, "claude_busy": False,
         "last_checked": 5.0, "sync_error": None, "sync_error_code": None,
         "can_push": False, "pending": False}
    s.update(over)
    return s


def _act(env, d, action, **body):
    return _post(env, "action", path=d, action=action, **body)


def test_action_gate(env):
    d = env.app("a")
    r = env.client.post("/api/contrib/action", json={"path": d, "action": "check"})
    assert r.status_code == 403
    assert _post(env, "action", path="/nowhere", action="check")["reason"] == "not-an-app"
    assert _act(env, d, "fly")["reason"] == "unknown-action"
    env.flags["sources"] = False
    assert _act(env, d, "check")["reason"] == "disabled"


def test_action_must_be_offered_right_now(env):
    """Flags, kind and state decide in actions.for_app only: an id it does
    not offer is refused, whatever the client posts."""
    d = env.app("a")
    env.engine.status = lambda app_dir: _v2("following")
    for wrong in ("propose", "publish-now", "use-github", "merge-claude", "discard"):
        out = _act(env, d, wrong, args={"title": "T"})
        assert out == {"ok": False, "reason": "not-offered",
                       "message": "That isn't available right now."}, wrong
    env.engine.status = lambda app_dir: _v2("changes")
    env.engine.status = lambda app_dir: _v2("changes", can_push=True)
    env.flags["changes"] = False  # §16.12: changes off hides only the proposal buttons
    assert _act(env, d, "propose", args={"title": "T"})["reason"] == "not-offered"
    assert _act(env, d, "push-main", confirm=True)["reason"] == "not-offered"
    assert _act(env, d, "discard", confirm=True) == {"ok": True, "withdrew": False}


def test_confirm_is_required_for_a_confirm_button(env):
    d = env.app("a")
    env.engine.status = lambda app_dir: _v2("changes")
    seen = []
    env.engine.discard = lambda app_dir, withdraw=False: seen.append(withdraw) or {"ok": True}
    out = _act(env, d, "discard")
    assert out["ok"] is False and out["reason"] == "confirm"
    assert "Activity" in out["message"] and seen == []
    assert _act(env, d, "discard", confirm=True) == {"ok": True}
    assert seen == [False]
    job = _act(env, d, "discard", confirm=True, args={"withdraw": True})["job"]
    assert _job(env, job)["status"] == "done" and seen[-1] is True


def test_update_proposal_takes_include(env):
    d = env.app("a")
    env.engine.status = lambda app_dir: _v2("proposed", unproposed=1, changes_files=["a.py"])
    env.proposals.view = lambda app_dir: {"number": 4, "state": "waiting", "can_update": True,
                                          "can_withdraw": True}
    sent = []
    env.proposals.start_update = lambda app_dir, **kw: sent.append(kw) or {"ok": True,
                                                                          "job": "u1"}
    assert _act(env, d, "update-proposal", args={"include": ["../x"]})["reason"] == "missing"
    assert _act(env, d, "update-proposal", args={"include": ["new.csv"]}) == {"ok": True,
                                                                              "job": "u1"}
    assert _act(env, d, "update-proposal") == {"ok": True, "job": "u1"}
    assert sent == [{"include": ["new.csv"]}, {}]


def test_propose_args(env):
    d = env.app("a")
    env.engine.status = lambda app_dir: _v2("changes")
    sent = []
    env.proposals.start_send = lambda app_dir, **kw: sent.append(kw) or {"ok": True, "job": "p1"}
    assert _act(env, d, "propose", args={})["reason"] == "missing"
    for bad in (["../x"], ["/etc/passwd"], "a.py", [""], [3]):
        assert _act(env, d, "propose", args={"title": "T", "include": bad})["reason"] \
            == "missing", bad
    assert sent == []
    assert _act(env, d, "propose", args={"title": " T ", "description": "D",
                                         "include": ["data/new.csv"]}) == {"ok": True,
                                                                           "job": "p1"}
    assert sent == [{"title": "T", "description": "D", "note": "", "include": ["data/new.csv"]}]
    _act(env, d, "propose", args={"title": "T"})
    assert "include" not in sent[-1]  # nothing ticked: the v1 call shape


def test_long_actions_are_jobs(env):
    d = env.app("a")
    env.engine.status = lambda app_dir: _v2("following", behind=2)
    ran = []
    env.engine.bring_in = lambda app_dir: ran.append("bring_in") or {"ok": True, "message": "In."}
    out = _act(env, d, "bring-in")
    assert out["ok"] is True and _job(env, out["job"])["result"] == {"ok": True, "message": "In."}
    env.engine.bring_in = lambda app_dir: {"ok": False, "reason": "busy",
                                           "message": "Claude is still working on this app."}
    job = _job(env, _act(env, d, "bring-in")["job"])
    assert job["status"] == "failed" and job["reason"] == "busy"
    assert job["message"] == "Claude is still working on this app."


def test_quick_fixes_answer_inline(env):
    d = env.app("a")
    env.engine.status = lambda app_dir: _v2("needs-merge", needs_merge_files=["a"])
    env.engine.keep_mine = lambda app_dir: {"ok": True, "message": "Kept yours."}
    assert _act(env, d, "keep-mine") == {"ok": True, "message": "Kept yours."}
    env.engine.use_theirs = lambda app_dir: {"ok": True}
    assert _act(env, d, "use-theirs")["reason"] == "confirm"
    assert _act(env, d, "use-theirs", confirm=True) == {"ok": True}


def test_merge_claude_starts_the_planned_merge(env):
    d = env.app("a")
    env.engine.status = lambda app_dir: _v2("needs-merge")
    started = []
    env.engine.merge_plan = lambda app_dir: {"purpose": "update", "ours": "h", "theirs": "t",
                                             "onto": "t", "files": ["a"]}

    def start(app_dir, *, purpose, ours, theirs, resolver=None):
        started.append((purpose, ours, theirs))
        return "mj-2"

    env.merge_job.start = start
    assert _act(env, d, "merge-claude") == {"ok": True, "job": "mj-2", "merge": True}
    assert started == [("update", "h", "t")]
    env.engine.merge_plan = lambda app_dir: None
    assert _act(env, d, "merge-claude")["reason"] == "nothing-to-merge"


def test_choose_decides_only_the_choice_files(env):
    """choose-mine / choose-theirs → merge_job.choose on THIS copy's choice
    job (its purposes per kind); the answer is the merge job, for the review."""
    d = env.app("a")
    choice = {"id": "mj-c", "status": "choice", "purpose": "update", "files": ["logo.png"],
              "error": "logo.png isn't text."}
    env.engine.status = lambda app_dir: _v2("needs-merge", merge_job=choice)
    asked, chosen = [], []
    env.merge_job.current = lambda app_dir, purpose=None: asked.append(purpose) or choice
    env.merge_job.choose = lambda job, side: chosen.append((job, side)) or {"ok": True}
    assert _act(env, d, "choose-mine") == {"ok": True, "job": "mj-c", "merge": True}
    assert _act(env, d, "choose-theirs")["ok"] is True
    assert chosen == [("mj-c", "mine"), ("mj-c", "theirs")]
    assert asked[-1] == ("update", "return", "proposal")
    # Once the job moved on, the per-file buttons are gone.
    env.engine.status = lambda app_dir: _v2("needs-merge")
    assert _act(env, d, "choose-mine")["reason"] == "not-offered"


def test_own_bring_in_is_ownsyncs_when_it_names_it(env):
    """`kept` (an undone combine) → ownsync.bring_in; a remix whose original
    moved → remix.start_bring_in."""
    d = env.app("m", kind="own", repo="bob/m")
    ran = []
    env.ownsync.status = lambda app_dir: {"state": "kept", "unpublished": 0, "github_new": 1,
                                          "files": [], "message": "You undid it.",
                                          "fixes": ["bring-in"]}
    env.ownsync.bring_in = lambda app_dir: ran.append("own") or {"ok": True}
    env.remix.start_bring_in = lambda app_dir: ran.append("remix") or {"ok": True, "job": "r"}
    assert _job(env, _act(env, d, "bring-in")["job"])["status"] == "done"
    r = env.app("r", kind="own", repo="bob/r", remix_of={"repo": "alice/x"},
                original_update=True)
    env.ownsync.status = lambda app_dir: {"state": "published", "unpublished": 0,
                                          "github_new": 0, "files": [], "message": "",
                                          "fixes": []}
    # A merge job id: the page reads the review, never polls /job (rev-fe 4).
    assert _act(env, r, "bring-in") == {"ok": True, "job": "r", "merge": True}
    assert ran == ["own", "remix"]


def test_own_app_fixes_dispatch_to_ownsync(env):
    d = env.app("m", kind="own", repo="bob/m")
    stuck = {"state": "github-changed", "unpublished": 1, "github_new": 2, "files": [],
             "message": "Choose which version to keep.",
             "fixes": ["use-github", "put-on-top", "publish-over"]}
    env.ownsync.status = lambda app_dir: dict(stuck)
    ran = []
    env.ownsync.put_on_top = lambda app_dir: ran.append("top") or {"ok": True}
    env.ownsync.publish_over = lambda app_dir: ran.append("over") or {"ok": True}
    env.flags.update(changes=False, inbox=False, loop=False, remix=False)  # P5: still fixable
    assert _job(env, _act(env, d, "put-on-top")["job"])["status"] == "done"
    assert _act(env, d, "publish-over")["reason"] == "confirm"
    assert _job(env, _act(env, d, "publish-over", confirm=True)["job"])["status"] == "done"
    assert ran == ["top", "over"]
    assert _act(env, d, "propose", args={"title": "T"})["reason"] == "not-offered"
    # Merge with Claude for an own app is ownsync's own start.
    stuck.update(state="needs-merge", fixes=["merge-claude"])
    env.ownsync.start_merge = lambda app_dir: {"ok": True, "job": "mj-o"}
    assert _act(env, d, "merge-claude") == {"ok": True, "job": "mj-o", "merge": True}


def test_check_now(env, monkeypatch):
    kicked, synced = [], []
    monkeypatch.setattr(refresh, "refresh_now", lambda app_dir: kicked.append(app_dir) or True)
    from fused_render import git_upstream
    monkeypatch.setattr(git_upstream, "sync_repo",
                        lambda root, *, action, push: synced.append((action, push))
                        or {"ok": True, "status": "noop"})
    own = env.app("m", kind="own", repo="bob/m")
    env.ownsync.check_now = lambda app_dir: {"ok": True, "message": "Up to date."}
    assert _job(env, _act(env, own, "check")["job"])["result"] == {"ok": True,
                                                                    "message": "Up to date."}
    assert kicked == [own] and synced == []
    # A GitHub-link copy: the sync auto-sync runs (the engine's table),
    # never pushing — and its own words stay in the log.
    d = env.app("a")
    env.engine.status = lambda app_dir: _v2("following")
    assert _job(env, _act(env, d, "check")["job"])["result"] == {"ok": True}
    assert synced == [("Check now", False)] and kicked == [own, d]
    monkeypatch.setattr(git_upstream, "sync_repo",
                        lambda root, **_: {"ok": False, "reason": "diverged",
                                           "message": "fatal: Not possible to fast-forward"})
    job = _job(env, _act(env, d, "check")["job"])
    assert job["status"] == "failed" and job["message"] == "Couldn't check right now. Try again."


def test_a_broken_resolver_still_checks(env, monkeypatch):
    """P6 holds when actions.for_app itself fails: GET shows Check now and
    the action route accepts exactly that."""
    d = env.app("a")
    monkeypatch.setattr(sys.modules["fused_render.contrib.actions"], "for_app", _boom)
    out = env.client.get("/api/contrib/app", params={"path": d}).json()
    assert [a["id"] for a in out["actions"]] == ["check"]
    monkeypatch.setattr(refresh, "refresh_now", lambda app_dir: True)
    from fused_render import git_upstream
    monkeypatch.setattr(git_upstream, "sync_repo", lambda root, **_: {"ok": True})
    assert _job(env, _act(env, d, "check")["job"])["status"] == "done"
    assert _act(env, d, "discard", confirm=True)["reason"] == "not-offered"


def test_sign_in_is_the_pages_own_flow(env, monkeypatch):
    d = env.app("a")
    env.engine.status = lambda app_dir: _v2("changes")
    from fused_render import github_setup
    monkeypatch.setattr(github_setup, "cached_summary",
                        lambda: {"signed_in": False, "account": None, "stale": False})
    assert _act(env, d, "sign-in") == {"ok": True, "dialog": "sign-in"}
    assert _act(env, d, "propose", args={"title": "T"})["reason"] == "not-offered"


def test_restore_only_an_offered_undo(env, monkeypatch):
    d = env.app("a")
    lines = [{"id": "a1", "at": 1, "kind": "update", "text": "Got alice's latest",
              "undo": True},
             {"id": "a2", "at": 0, "kind": "saved", "text": "Saved your edit", "undo": False}]
    monkeypatch.setattr(store, "activity", lambda app_dir: list(lines), raising=False)
    restored = []
    env.engine.restore = lambda app_dir, line: restored.append(line) or {"ok": True}
    for bad in ("a2", "zz", ""):
        assert _post(env, "restore", path=d, id=bad)["reason"] == "not-offered"
    assert _post(env, "restore", path=d, id="a1") == {"ok": True}
    assert restored == ["a1"]
    # rev-fe 13: no Undo while a merge that lands here is running or ready.
    env.merge_job.for_app = lambda app_dir, purpose=None: {"id": "mj", "status": "ready",
                                                          "land": True}
    assert _post(env, "restore", path=d, id="a1") == {
        "ok": False, "reason": "merging", "message": "Keep or discard Claude's merge first."}
    env.merge_job.for_app = lambda app_dir, purpose=None: None
    # An own app's lines are ownsync's to undo, never the engine's.
    mine = env.app("m", kind="own", repo="bob/m")
    env.ownsync.restore = lambda app_dir, line: restored.append(("own", line)) or {"ok": True}
    assert _post(env, "restore", path=mine, id="a1") == {"ok": True}
    assert restored == ["a1", ("own", "a1")]


def test_app_read_carries_panel_actions_activity(env, monkeypatch):
    d = env.app("a")
    env.engine.status = lambda app_dir: _v2("changes", behind=1)
    line = {"id": "a1", "at": 1, "kind": "update", "text": "Got alice's latest", "undo": True}
    monkeypatch.setattr(store, "activity", lambda app_dir: [line], raising=False)
    out = env.client.get("/api/contrib/app", params={"path": d}).json()
    assert out["panel"]["state"] == "changes" and out["panel"]["who"] == "alice"
    assert out["panel"]["last_checked"] == 5.0
    assert out["panel"]["untracked"] == ["new.csv"]  # for the Propose dialog
    assert [a["id"] for a in out["actions"]] == ["propose", "remix", "discard", "bring-in",
                                                 "check"]
    assert out["activity"] == [line]
    # A broken resolver still answers a panel with Check now (P6).
    monkeypatch.setattr(sys.modules["fused_render.contrib.actions"], "for_app", _boom)
    out = env.client.get("/api/contrib/app", params={"path": d}).json()
    assert out["panel"]["state"] == "unknown"
    assert [a["id"] for a in out["actions"]] == ["check"]


def test_unrecorded_app_reads_a_pending_panel(env):
    d = env.app("new", kind=None)
    env.engine.status = lambda app_dir: {"pending": True}
    out = env.client.get("/api/contrib/app", params={"path": d}).json()
    assert out["pending"] is True and out["panel"]["state"] == "pending"
    assert out["actions"] == [] and out["activity"] == []


# -- merges and jobs ------------------------------------------------------------


def test_merge_start_follows_the_engine_plan(env):
    d = env.app("a")
    assert _post(env, "merge/start", path=d)["reason"] == "nothing-to-merge"
    started = []
    env.merge_job.start = lambda app_dir, **kw: started.append(kw) or "mj-9"
    env.engine.merge_plan = lambda app_dir: {"purpose": "needs-merge", "onto": "aaa",
                                             "upstream": "bbb", "strategy": "rebase",
                                             "mark": "m1", "files": ["x"], "resolver": "no"}
    assert _post(env, "merge/start", path=d) == {"ok": True, "job": "mj-9"}
    # Every key merge_job.start takes is passed through; bookkeeping keys are not.
    assert started == [{"purpose": "needs-merge", "onto": "aaa", "upstream": "bbb",
                        "strategy": "rebase", "mark": "m1"}]
    # An own app's overlap is ownsync's to start, offered whenever the panel is.
    own = env.app("c", kind="own", repo="bob/c")
    env.ownsync.start_merge = lambda app_dir: {"ok": True, "job": "mj-own"}
    env.flags["changes"] = False
    assert _post(env, "merge/start", path=own) == {"ok": True, "job": "mj-own"}
    env.engine.merge_plan = lambda app_dir: {"purpose": "needs-merge", "onto": "a", "upstream": "b"}
    assert _post(env, "merge/start", path=d)["reason"] == "disabled"


def test_merge_accept_only_for_this_apps_job(env):
    a, b = env.app("a"), env.app("b")
    env.merge_job.get = lambda i: {"id": "mj", "status": "ready",
                                   "app_dir": store.key(a)} if i == "mj" else None
    env.merge_job.accept = lambda i: {"ok": True, "message": "Kept."}
    assert _post(env, "merge/accept", path=b, job="mj")["reason"] == "unknown-job"
    assert _post(env, "merge/accept", path=a, job="nope")["reason"] == "unknown-job"
    assert _post(env, "merge/accept", path=a, job="mj") == {"ok": True, "message": "Kept."}


def test_keeping_an_inbox_merge_sends_it_to_the_proposal(env):
    """An inbox Merge with Claude (inbox.owns_merge) is kept through
    inbox.merge_accept — never merge_job.accept alone."""
    mine = env.app("mine", kind="own", repo="bob/mine")
    env.merge_job.get = lambda i: {"id": i, "status": "ready", "purpose": "proposal",
                                   "app_dir": store.key(mine)}
    env.merge_job.accept = _boom
    env.inbox.owns_merge = lambda job: job == "mj"
    kept, discarded = [], []
    env.inbox.merge_accept = lambda d, job: kept.append(job) or {"ok": True, "message": "Sent."}
    env.merge_job.discard = lambda i: discarded.append(i) or {"ok": True}
    assert _post(env, "merge/accept", path=mine, job="mj") == {"ok": True, "message": "Sent."}
    assert kept == ["mj"]
    assert _post(env, "merge/discard", path=mine, job="mj") == {"ok": True}
    assert discarded == ["mj"]
    env.flags["inbox"] = False
    assert _post(env, "merge/accept", path=mine, job="mj")["reason"] == "disabled"


def test_job_endpoint_reads_both_registries(env):
    env.merge_job.get = lambda i: {"status": "ready", "summary": "Both kept.", "files": ["a"],
                                   "purpose": "needs-merge", "error": None} if i == "mj" else None
    out = env.client.get("/api/contrib/job", params={"id": "mj"}).json()
    assert (out["status"], out["message"]) == ("done", "Both kept.")
    assert out["result"]["purpose"] == "needs-merge"
    gone = env.client.get("/api/contrib/job", params={"id": "zzz"}).json()
    assert gone["ok"] is False and gone["status"] == "failed"


# -- inbox ----------------------------------------------------------------------


def test_inbox_routes(env):
    d = env.app("mine", kind="own", repo="bob/mine")
    assert _post(env, "inbox/review", path=d, number=7) == {"ok": True, "job": "rv-1"}
    assert _post(env, "inbox/review", path=d, number="x")["reason"] == "missing"
    tried = _post(env, "inbox/try", path=d, number=7)
    assert (tried["path"], tried["url"], tried["confirm"]) == ("/p", "/u", False)
    assert _post(env, "inbox/try", path=d, number=7, confirm=True)["confirm"] is True
    assert _post(env, "inbox/decline", path=d, number=7, note=" thanks ")["note"] == "thanks"


def test_accepting_two_proposals_runs_two_jobs(env):
    d = env.app("mine", kind="own", repo="bob/mine")
    gate, accepted = threading.Event(), []

    def accept(app_dir, number, confirm=False):
        accepted.append(number)
        gate.wait(5)
        return {"ok": True, "merged": True}

    env.inbox.accept = accept
    a = _post(env, "inbox/accept", path=d, number=7)["job"]
    b = _post(env, "inbox/accept", path=d, number=8)["job"]
    again = _post(env, "inbox/accept", path=d, number=7)["job"]
    gate.set()
    assert a != b and again == a
    jobs.wait(a)
    jobs.wait(b)
    assert sorted(accepted) == [7, 8]


def test_an_accept_refusal_fails_its_job(env):
    d = env.app("mine", kind="own", repo="bob/mine")
    env.inbox.accept = lambda app_dir, n, confirm=False: {"ok": False, "reason": "closed",
                                                          "message": "This proposal is no longer open."}
    out = _job(env, _post(env, "inbox/accept", path=d, number=7)["job"])
    assert (out["status"], out["message"], out["reason"]) == (
        "failed", "This proposal is no longer open.", "closed")


def test_after_decline_or_accept_the_inbox_reloads_not_unavailable(env, monkeypatch):
    mine = env.app("mine", kind="own", repo="bob/mine")
    lists = [[{"number": 7}, {"number": 8}]]
    env.inbox.cached = lambda d, **_: lists[-1] if lists else None
    env.inbox.list = lambda d, review=True, force=False: lists.append([{"number": 8}]) or lists[-1]

    def decline(d, n, note=""):
        lists.clear()  # inbox.invalidate: the cached list is gone
        return {"ok": True}

    env.inbox.decline = decline
    # A recent failed kick on record: without the reset, the next read would
    # wait out the throttle and answer "unavailable".
    key = store.key(mine)
    refresh._inbox_kicked[key] = refresh.time.monotonic()
    refresh._inbox_reached[key] = False
    refresh._inbox_last[key] = [{"number": 7}, {"number": 8}]
    queued = []
    monkeypatch.setattr(refresh, "_submit", lambda prio, what, k, fn: queued.append((what, fn)) or True)
    assert _post(env, "inbox/decline", path=mine, number=7)["ok"] is True
    assert [w for w, _ in queued] == ["inbox-open"]  # listed again at once
    snap = refresh.inbox_snapshot(mine)
    assert snap["loading"] is True and "unavailable" not in snap
    assert snap["items"] == [{"number": 8}]  # the rest stays shown, the declined one gone
    queued[0][1]()
    assert refresh.inbox_snapshot(mine) == {"items": [{"number": 8}], "count": 1, "loading": False}

    # Accept: the reset runs when the job ends, refusal or not.
    queued.clear()
    env.inbox.accept = lambda d, n, confirm=False: lists.clear() or {"ok": True}
    job = _post(env, "inbox/accept", path=mine, number=8)["job"]
    jobs.wait(job)
    assert jobs.get(job)["app_dir"] == key  # keyed on the store's spelling
    assert [w for w, _ in queued] == ["inbox-open"]


def test_accept_passes_confirm_through(env):
    d = env.app("mine", kind="own", repo="bob/mine")
    seen = []
    env.inbox.accept = lambda app_dir, n, confirm=False: seen.append(confirm) or {"ok": True}
    jobs.wait(_post(env, "inbox/accept", path=d, number=7)["job"])
    jobs.wait(_post(env, "inbox/accept", path=d, number=8, confirm=True)["job"])
    assert seen == [False, True]


def test_a_github_copy_is_never_uploaded_to_fused(env):
    from fused_render.share_app import GITHUB_COPY_REFUSAL
    from fused_render.share_app import router as share_router
    app = FastAPI()
    app.include_router(share_router)
    c = TestClient(app)
    theirs, mine = env.app("theirs"), env.app("mine", kind="own", repo="bob/mine")
    st = c.get("/api/share/status", params={"path": theirs}).json()
    assert (st["can_share"], st["refusal"], st["refusal_code"]) == (
        False, GITHUB_COPY_REFUSAL, "github_copy")
    r = c.post("/api/share/publish", data={"path": theirs}, headers=H)
    assert r.status_code == 409 and r.json()["code"] == "github_copy"
    assert c.get("/api/share/status", params={"path": mine}).json()["refusal_code"] is None
    env.flags["sources"] = False  # loop off: today's behaviour
    assert c.get("/api/share/status", params={"path": theirs}).json()["refusal_code"] is None


# -- reads ----------------------------------------------------------------------


def test_app_read_assembles_cached_state_without_github(env):
    d = env.app("a")
    out = env.client.get("/api/contrib/app", params={"path": d}).json()
    assert out["ok"] is True and out["pending"] is False
    assert out["copy"]["state"] == "changes"
    assert out["proposal"] == {"number": 4, "state": "waiting"}
    assert out["inbox"] is None  # not an own app
    assert out["contributors"][0]["login"] == "bob"
    assert out["github"] == {"signed_in": True, "login": "bob", "checking": False}
    # A contributor's copy shares the AUTHOR's link (share_app._contributor_link).
    assert out["share_url"].endswith("?repo=alice/x")
    mine = env.app("m", kind="own", repo="bob/m")
    out = env.client.get("/api/contrib/app", params={"path": mine}).json()
    assert out["inbox"] == {"items": [{"number": 7, "title": "x"}], "count": 1,
                            "loading": False}
    assert out["share_url"] is None  # own app, not a repo at all


def test_app_read_disabled_and_unrecorded(env):
    d = env.app("a", kind=None)
    env.flags["sources"] = False
    out = env.client.get("/api/contrib/app", params={"path": d}).json()
    assert out["ok"] is False and out["reason"] == "disabled" and out["flags"]["sources"] is False
    env.flags["sources"] = True
    env.proposals.view = _boom
    out = env.client.get("/api/contrib/app", params={"path": d}).json()
    assert out["pending"] is True and out["proposal"] is None


def test_app_read_runs_status_once_and_no_share_link_while_pending(env, monkeypatch):
    from fused_render import share_app
    statuses = []
    env.engine.status = lambda d: statuses.append(d) or {"changes": 5, "pending": False}
    d = env.app("a")
    out = env.client.get("/api/contrib/app", params={"path": d}).json()
    assert len(statuses) == 1  # handed to the contributor link, not re-run
    assert out["share_url"].endswith("?repo=alice/x")
    assert share_app.github_link_state(d, copy_status={"changes": 5})["changes"] == 5
    monkeypatch.setattr(share_app, "github_link_state", _boom)
    pending = env.app("p", kind=None)
    env.engine.status = lambda d: {"pending": True}
    out = env.client.get("/api/contrib/app", params={"path": pending}).json()
    assert out["pending"] is True and out["share_url"] is None


def _never_listed_inbox(env, reaches_github):
    """An inbox stand-in that has no list until `list` reaches GitHub."""
    listed, asked = [], []

    def list_(d, review=True, force=False):
        asked.append(review)
        if reaches_github:
            listed.append([{"number": 9}])
        return listed[-1] if listed else []

    env.inbox.list = list_
    env.inbox.cached = lambda d, **_: listed[-1] if listed else None
    return asked


def test_a_never_listed_inbox_reads_loading_and_is_kicked(env, monkeypatch):
    mine = env.app("m", kind="own", repo="bob/m")
    asked = _never_listed_inbox(env, reaches_github=True)
    queued = []
    monkeypatch.setattr(refresh, "_submit", lambda prio, what, key, fn: queued.append(
        (prio, what, fn)) or True)
    get = lambda: env.client.get("/api/contrib/app", params={"path": mine}).json()  # noqa: E731
    assert get()["inbox"] == {"items": [], "count": 0, "loading": True}
    kicks = [q for q in queued if q[1] == "inbox-open"]
    assert len(kicks) == 1 and kicks[0][0] == refresh._P_OPEN
    get()
    assert len([q for q in queued if q[1] == "inbox-open"]) == 1  # throttled, not re-asked
    kicks[0][2]()  # the worker runs the listing
    assert asked == [True]
    assert get()["inbox"] == {"items": [{"number": 9}], "count": 1, "loading": False}
    assert refresh.inbox_count(mine) == 1


def test_an_inbox_github_never_answered_is_unavailable_not_empty(env):
    mine = env.app("m", kind="own", repo="bob/m")
    _never_listed_inbox(env, reaches_github=False)
    out = env.client.get("/api/contrib/app", params={"path": mine}).json()["inbox"]
    assert out == {"items": [], "count": 0, "loading": False, "unavailable": True}
    assert store.key(mine) not in refresh._counts  # no confident zero on the card either


def test_the_page_starts_reviews_and_marks_a_kept_list_refreshing(env):
    mine = env.app("m", kind="own", repo="bob/m")
    asked, state = [], {"refreshing": True}
    env.inbox.cached = lambda d, start_reviews=False: asked.append(start_reviews) or [{"number": 7}]
    env.inbox.refreshing = lambda d: state["refreshing"]
    assert refresh.inbox_snapshot(mine) == {"items": [{"number": 7}], "count": 1, "loading": True}
    assert asked == [True]  # the page path starts reviews
    state["refreshing"] = False
    assert refresh.inbox_snapshot(mine)["loading"] is False
    asked.clear()
    refresh.sweep_once()
    assert True not in asked  # the sweep never does


def test_an_offline_rekick_keeps_unavailable_and_the_last_list(env, monkeypatch):
    mine = env.app("m", kind="own", repo="bob/m")
    online = {"up": True}
    listed = []

    def list_(d, review=True, force=False):
        if online["up"]:
            listed.append([{"number": 9}])
        return listed[-1] if listed else []

    env.inbox.list = list_
    env.inbox.cached = lambda d, **_: listed[-1] if listed else None
    assert refresh.inbox_snapshot(mine)["items"] == [{"number": 9}]  # one good list
    listed.clear()  # the cache is dropped and GitHub goes away
    online["up"] = False
    # Until the throttle lets the next kick through, a dropped cache reads
    # as loading (a listing is due), not as offline.
    assert refresh.inbox_snapshot(mine)["loading"] is True
    refresh._inbox_kicked.clear()
    first = refresh.inbox_snapshot(mine)  # kicked inline: fails
    assert first == {"items": [{"number": 9}], "count": 1, "loading": False, "unavailable": True}
    # The next kick (throttle expired) is queued: loading AND still unavailable.
    refresh._inbox_kicked.clear()
    monkeypatch.setattr(refresh, "_submit", lambda prio, what, k, fn: refresh._queued.add(
        (what, k)) or True)
    again = refresh.inbox_snapshot(mine)
    assert again == {"items": [{"number": 9}], "count": 1, "loading": True, "unavailable": True}
    refresh._queued.clear()


def test_github_checking_until_first_measured(env, monkeypatch):
    from fused_render import github_setup
    monkeypatch.setattr(github_setup, "cached_summary", lambda: None)
    measured = []
    monkeypatch.setattr(github_setup, "summary", lambda: measured.append(1) or {})
    out = env.client.get("/api/contrib/app", params={"path": env.app("a")}).json()
    assert out["github"] == {"signed_in": False, "login": None, "checking": True}
    assert measured == [1]  # re-measured off the request path (inline in tests)


def test_ack(env):
    d = env.app("a", proposal={"number": 4, "cache": {"state": "merged"}, "settled_seen": False})
    assert _post(env, "ack", path=d, kind="settled") == {"ok": True}
    assert store.get(d)["proposal"]["settled_seen"] is True
    assert _post(env, "ack", path=d, kind="other")["reason"] == "bad-kind"


def test_contributor_share_link(env):
    from fused_render import share_app
    d = env.app("a")
    state = share_app.github_link_state(d)
    assert state["ok"] is True and state["code"] == "contributor"
    assert (state["repo"], state["changes"]) == ("alice/x", 2)
    # Uncommitted edits are one more thing the author's link doesn't carry.
    dirty = share_app.github_link_state(d, copy_status={"changes": 2, "dirty": True})
    assert dirty["changes"] == 3
    # A folder nested inside the copy is still the author's code: no Fused
    # upload, but no contributor link either (it would install the whole repo).
    nested = os.path.join(d, "sub")
    os.mkdir(nested)
    assert share_app._github_copy_repo(nested) == "alice/x"
    assert share_app._contributor_link(nested) is None
    env.flags["sources"] = False
    assert share_app.github_link_state(d)["code"] == "not_repo"  # today's checks
    assert share_app._github_copy_repo(nested) is None


def test_a_retry_sync_is_labelled(monkeypatch):
    from fused_render.server.routers import git_upstream as gu_router
    assert gu_router._SYNC_ACTIONS["retry"] == "Retry after a failed send"


# -- the listing ----------------------------------------------------------------


def test_listing_gains_contrib_only_with_sources_on_and_runs_no_git(env, monkeypatch):
    theirs = env.app("theirs")
    env.app("mine", kind="own", repo="bob/mine")
    env.app("remix", kind="own", repo="bob/remix", remix_of={"repo": "alice/x", "branch": "main"})
    env.app("new", kind=None)
    env.engine.status = _boom
    real_popen = subprocess.Popen

    def no_git(args, *a, **k):
        if "git" in str(args[0]) or "gh" in os.path.basename(str(args[0])):
            raise AssertionError(f"listing spawned {args!r}")
        return real_popen(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", no_git)
    refresh._counts[store.key(os.path.join(env.ws, "tag", "mine"))] = 2
    rows = {r["name"]: r for r in env.client.get("/api/apps").json()["apps"]}
    assert rows["theirs"]["contrib"] == {"label": "GitHub link · follows alice/x", "inbox": 0,
                                         "kind": "github"}
    assert rows["mine"]["contrib"] == {"label": "Your app", "inbox": 2, "kind": "own"}
    assert rows["remix"]["contrib"] == {"label": "Remix of alice/x", "inbox": 0, "kind": "remix"}
    assert "contrib" not in rows["new"]
    # The unrecorded app was handed to the classifier (inline here).
    assert ("ensure", os.path.join(env.ws, "tag", "new")) in env.calls
    paged = env.client.get("/api/apps", params={"limit": 50}).json()["apps"]
    assert {r["name"]: r.get("contrib") for r in paged}["theirs"]["kind"] == "github"
    cards = env.client.get("/api/contrib/cards").json()
    assert cards[store.key(theirs)]["kind"] == "github"

    env.flags["sources"] = False
    rows = env.client.get("/api/apps").json()["apps"]
    assert all("contrib" not in r for r in rows)
    assert env.client.get("/api/contrib/cards").json() == {}


# -- the background refresh -----------------------------------------------------


def test_open_refreshes_by_kind_and_flag_throttled(env):
    theirs = env.app("theirs", proposal={"number": 1, "cache": {"state": "open"}})
    assert refresh.note_app_opened(theirs) is True
    assert ("prefresh", False) in env.calls
    assert refresh.note_app_opened(theirs) is False  # throttled
    mine = env.app("mine", kind="own", repo="bob/mine",
                   remix_of={"repo": "alice/x", "branch": "main"},
                   proposal={"number": 2, "cache": {"state": "open"}})
    env.calls.clear()
    refresh.note_app_opened(mine)
    assert ("ilist", True) in env.calls and ("original", mine) in env.calls
    assert ("prefresh", False) in env.calls  # a remix's proposal is still followed
    assert refresh.inbox_count(mine) == 1
    # Phase off: the open queues nothing at all.
    env.flags["sources"] = False
    other = env.app("other")
    assert refresh.note_app_opened(other) is False
    # Not a workspace app: never classified.
    env.flags["sources"] = True
    assert refresh.note_app_opened(str(env.ws / "tag")) is False


def test_forced_refresh_is_rate_limited(env):
    d = env.app("a", proposal={"number": 1, "cache": {"state": "open"}})
    assert _post(env, "refresh", path=d) == {"ok": True, "refreshed": True, "kind": "github"}
    assert ("prefresh", True) in env.calls
    assert _post(env, "refresh", path=d) == {"ok": True, "refreshed": False, "kind": "github"}


def test_refresh_rechecks_ownership_with_a_fresh_identity(env, monkeypatch):
    """Classified while signed out (owner_checked False); the user signs in
    as the repo's owner and the page refreshes: the copy becomes theirs —
    on the throttled path too."""
    from fused_render import github_setup
    who = {"login": None}
    asked = []

    def identity(refresh=False, **_):
        asked.append(refresh)
        if refresh:
            who["login"] = "alice"  # the sign-in that just finished
        return {"login": who["login"]} if who["login"] else None

    env.stub("gh", identity=identity)
    monkeypatch.setattr(github_setup, "summary_refreshed", lambda: asked.append("status") or {})

    def ensure(app_dir, **_):  # classify's one-time upgrade, against gh's answer
        rec = store.get(app_dir)
        if rec["kind"] == "github" and not rec["owner_checked"] and who["login"]:
            rec.update(owner_checked=True,
                       kind="own" if rec["repo"].startswith(who["login"] + "/") else "github")
            store.put(app_dir, rec)
        return store.get(app_dir)

    env.classify.ensure = ensure
    env.flags.update(changes=False, inbox=False)  # sources alone allows it
    d = env.app("a", owner_checked=False)
    assert _post(env, "refresh", path=d) == {"ok": True, "refreshed": True, "kind": "own"}
    assert asked == [True, "status"]
    # Throttled: the re-check still runs.
    who["login"] = None  # signed out again until this refresh asks afresh
    other = env.app("b", owner_checked=False, repo="carol/b")
    refresh._last_force[store.key(other)] = refresh.time.monotonic()
    asked.clear()
    assert _post(env, "refresh", path=other) == {"ok": True, "refreshed": False, "kind": "github"}
    assert asked == [True, "status"]
    assert store.get(other)["owner_checked"] is True


def test_sweep_counts_without_starting_reviews(env):
    mine = env.app("mine", kind="own", repo="bob/mine")
    env.app("theirs")
    assert refresh.sweep_once() == 1
    assert env.calls == [("ilist", False)]
    assert refresh.inbox_count(mine) == 1
    env.flags["inbox"] = False
    assert refresh.sweep_once() == 0


def test_per_app_memory_is_capped(env, monkeypatch):
    monkeypatch.setattr(refresh, "MAX_APPS", 3)
    for i in range(5):
        refresh.note_inbox(str(env.ws / "tag" / f"a{i}"), [1])
    assert len(refresh._counts) == 3
    assert refresh.inbox_count(str(env.ws / "tag" / "a4")) == 1
    assert refresh.inbox_count(str(env.ws / "tag" / "a0")) == 0
