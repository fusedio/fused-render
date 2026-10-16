"""Tests for the contribution loop's HTTP surface (server/routers/contrib.py)
and its listing/background side (contrib/refresh.py).

A bare FastAPI app carrying just the routers under test (the
test_server_github.py pattern), the REAL record store and flags under a
per-test home and workspace, and stand-ins for the modules whose behaviour
belongs to other test files (engine, classify, proposals, gh, merge_job,
remix, contributors): installed into `sys.modules`, which is what the
router's lazy `_mod()` imports resolve to. A stand-in that must never be
reached (GitHub on a read path, git on the listing) raises.

What every route must get right, and what is pinned here: the X-Fused
guard, the workspace-app containment rule, the phase flag checked BEFORE
any classification, the record-kind check, plain refusals, one job for a
double-clicked Send, reads that never wait on GitHub, and a listing that
gains `contrib` only while the sources phase is on — and pays no git for it.
"""
import os
import sys
import threading
import types

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from fused_render.contrib import flags, jobs, refresh, store
from fused_render.contrib.gh import GitHubError

H = {"X-Fused": "1"}


def _boom(*_a, **_k):
    raise AssertionError("must not be called on this path")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, ws = tmp_path / "home", tmp_path / "ws"
    home.mkdir()
    ws.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    on = {"sources": True, "changes": True, "inbox": True, "loop": True, "remix": True}
    monkeypatch.setattr(flags, "_raw", lambda: dict(on))
    monkeypatch.setattr(refresh, "_INLINE", True)
    refresh._reset()

    from fused_render import github_setup
    monkeypatch.setattr(github_setup, "_read_cache",
                        lambda: {"signed_in": True, "account": "bob", "checked_at": 9e18})
    monkeypatch.setattr(github_setup, "_too_old", lambda c: False)
    monkeypatch.setattr(github_setup, "summary", _boom)

    calls = []

    def stub(name, **fns):
        mod = types.ModuleType(f"fused_render.contrib.{name}")
        for k, v in fns.items():
            setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
        return mod

    def ensure(app_dir, **_):
        calls.append(("ensure", app_dir))
        return store.get(app_dir)

    ns = types.SimpleNamespace(home=home, ws=ws, flags=on, calls=calls, stub=stub)
    ns.classify = stub("classify", ensure=ensure)
    ns.engine = stub("engine", status=lambda d: {"kind": "github", "state": "changes",
                                                   "changes": 2, "unproposed": 1,
                                                   "needs_merge": False, "merge_job": None,
                                                   "can_push": False},
                     discard=lambda d, withdraw=False: {"ok": True, "withdrew": withdraw},
                     push_to_main=lambda d: {"ok": True})
    ns.gh = stub("gh", GitHubError=GitHubError, repo=_boom)
    ns.proposals = stub("proposals", refresh=lambda d, force=False: calls.append(("prefresh", force)),
                        draft=lambda d: {"title": "T", "description": "D"},
                        send=_boom, update=_boom, withdraw=lambda d: {"ok": True})
    ns.remix = stub("remix", check_original=lambda d: calls.append(("original", d)),
                    draft=_boom, publish=_boom, bring_in=lambda d: "mj-1")
    ns.contributors = stub("contributors", list=lambda d: [{"login": "bob", "name": "Bob", "commits": 2}])
    ns.merge_job = stub("merge_job", get=lambda i: None, start=_boom, accept=_boom, discard=_boom)
    ns.inbox = stub("inbox", list=lambda d, review=True: calls.append(("ilist", review)) or [{"number": 7}],
                    cached=lambda d: [{"number": 7, "title": "x"}], invalidate=lambda d: None,
                    review=lambda d, n: "rv-1", try_open=lambda d, n, confirm=False: {"ok": True, "confirm": confirm},
                    try_remove=lambda d, n: {"ok": True}, accept=_boom,
                    decline=lambda d, n, note="": {"ok": True, "note": note})

    def app(name, kind="github", **extra):
        d = ws / "tag" / name
        d.mkdir(parents=True)
        (d / "index.html").write_text('<meta name="fused-app">hi')
        if kind is not None:
            rec = store.new_record(kind, repo=extra.pop("repo", "alice/x" if kind == "github" else None),
                                   branch="main", classified_by="migration")
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


def test_a_path_inside_the_app_resolves_to_the_app(env):
    d = env.app("a")
    out = _post(env, "propose/draft", path=os.path.join(d, "index.html"))
    assert out["ok"] is True and out["job"]
    assert jobs.wait(out["job"])["result"] == {"title": "T", "description": "D"}


# -- proposals ------------------------------------------------------------------


def test_double_clicked_send_runs_one_job(env):
    d = env.app("a")
    gate, sent = threading.Event(), []

    def send(app_dir, *, title, description, note):
        sent.append(title)
        gate.wait(5)
        return {"number": 3}

    env.proposals.send = send
    a = _post(env, "propose/send", path=d, title="Brighter", description="", note="")
    b = _post(env, "propose/send", path=d, title="Brighter", description="", note="")
    gate.set()
    assert a["ok"] and a["job"] == b["job"]
    assert jobs.wait(a["job"])["status"] == "done"
    assert sent == ["Brighter"]


def test_send_refusals(env, monkeypatch):
    d = env.app("a")
    assert _post(env, "propose/send", path=d, title="")["reason"] == "missing"
    open_p = env.app("b", proposal={"number": 1, "cache": {"state": "open"}})
    assert _post(env, "propose/send", path=open_p, title="x")["reason"] == "already-proposed"
    assert _post(env, "propose/update", path=d)["reason"] == "no-proposal"
    assert _post(env, "propose/withdraw", path=d)["reason"] == "no-proposal"
    from fused_render import github_setup
    monkeypatch.setattr(github_setup, "_read_cache",
                        lambda: {"signed_in": False, "account": None, "checked_at": 0})
    out = _post(env, "propose/send", path=d, title="x")
    assert out == {"ok": False, "reason": "signed-out",
                   "message": "Sign in to GitHub to send your proposal."}


def test_a_github_failure_fails_the_job_with_its_plain_sentence(env):
    d = env.app("a")

    def send(app_dir, **_):
        raise GitHubError("forbidden", "GitHub didn't let you do that.")

    env.proposals.send = send
    job = _post(env, "propose/send", path=d, title="x")["job"]
    jobs.wait(job)
    out = env.client.get("/api/contrib/job", params={"id": job}).json()
    assert out["status"] == "failed"
    assert out["message"] == "GitHub didn't let you do that."


def test_a_module_refusal_fails_the_job_rather_than_finishing_it(env):
    d = env.app("a")
    env.proposals.update = lambda app_dir: {"ok": False, "reason": "x", "message": "Plain."}
    d2 = env.app("b", proposal={"number": 1, "cache": {"state": "open"}})
    job = _post(env, "propose/update", path=d2)["job"]
    jobs.wait(job)
    out = env.client.get("/api/contrib/job", params={"id": job}).json()
    assert (out["status"], out["message"]) == ("failed", "Plain.")
    assert d  # the first app has no proposal: refused before any job


def test_an_unexpected_exception_never_reaches_the_page(env):
    d = env.app("a", proposal={"number": 1, "cache": {"state": "open"}})

    def withdraw(app_dir):
        raise RuntimeError("fatal: refusing to merge unrelated histories")

    env.proposals.withdraw = withdraw
    out = _post(env, "propose/withdraw", path=d)
    assert out["ok"] is False and "fatal" not in out["message"]


def test_discard_and_push_main(env):
    d = env.app("a")
    assert _post(env, "discard", path=d) == {"ok": True, "withdrew": False}
    assert _post(env, "discard", path=d, withdraw=True)["withdrew"] is True
    # can_push False from the engine's cached status: refused, gh never asked.
    assert _post(env, "push-main", path=d)["reason"] == "not-allowed"
    env.engine.status = lambda app_dir: {"can_push": None}
    env.gh.repo = lambda full: {"permissions": {"push": True}}
    assert _post(env, "push-main", path=d) == {"ok": True}


# -- merges and jobs ------------------------------------------------------------


def test_merge_start_needs_a_recorded_overlap(env):
    d = env.app("a")
    assert _post(env, "merge/start", path=d)["reason"] == "nothing-to-merge"
    started = []
    env.merge_job.start = lambda app_dir, **kw: started.append(kw) or "mj-9"
    d2 = env.app("b", needs_merge={"onto": "aaa", "upstream": "bbb", "since": 1.0})
    assert _post(env, "merge/start", path=d2) == {"ok": True, "job": "mj-9"}
    assert started == [{"purpose": "needs-merge", "onto": "aaa", "upstream": "bbb"}]
    # An own app's overlap is the inbox phase's own-sync merge.
    own = env.app("c", kind="own", repo="bob/c",
                  needs_merge={"onto": "c1", "upstream": "c0", "since": 1.0})
    env.flags["changes"] = False
    assert _post(env, "merge/start", path=own)["ok"] is True
    assert started[-1]["purpose"] == "own-sync"
    assert _post(env, "merge/start", path=d2)["reason"] == "disabled"


def test_merge_accept_only_for_this_apps_job(env):
    a, b = env.app("a"), env.app("b")
    env.merge_job.current = lambda d: {"id": "mj", "status": "ready"} if d == a else None
    env.merge_job.accept = lambda i: {"ok": True, "message": "Kept."}
    assert _post(env, "merge/accept", path=b, job="mj")["reason"] == "unknown-job"
    assert _post(env, "merge/accept", path=a, job="nope")["reason"] == "unknown-job"
    assert _post(env, "merge/accept", path=a, job="mj") == {"ok": True, "message": "Kept."}


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
    assert _post(env, "inbox/try", path=d, number=7)["confirm"] is False
    assert _post(env, "inbox/try", path=d, number=7, confirm=True)["confirm"] is True
    assert _post(env, "inbox/decline", path=d, number=7, note=" thanks ")["note"] == "thanks"


def test_accepting_two_proposals_runs_two_jobs(env):
    d = env.app("mine", kind="own", repo="bob/mine")
    gate, accepted = threading.Event(), []

    def accept(app_dir, number):
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


def test_a_cold_github_cache_is_measured_before_refusing_a_send(env, monkeypatch):
    from fused_render import github_setup
    d = env.app("a")
    monkeypatch.setattr(github_setup, "_read_cache", lambda: None)
    monkeypatch.setattr(github_setup, "summary", lambda: {"signed_in": True, "account": "bob"})
    env.proposals.send = lambda app_dir, **_: {"number": 1}
    assert _post(env, "propose/send", path=d, title="x")["ok"] is True


# -- reads ----------------------------------------------------------------------


def test_app_read_assembles_cached_state_without_github(env):
    d = env.app("a", proposal={"number": 4, "url": "u", "title": "Brighter",
                               "cache": {"state": "open", "comment": {"author": "alice", "body": "nice",
                                                                      "at": "t"}}})
    out = env.client.get("/api/contrib/app", params={"path": d}).json()
    assert out["ok"] is True
    assert out["copy"]["state"] == "changes"
    assert out["proposal"] == {"number": 4, "url": "u", "title": "Brighter", "state": "commented",
                               "needs_merge": False, "unproposed": 1,
                               "comment": {"author": "alice", "body": "nice", "at": "t"}}
    assert out["inbox"] is None  # not an own app
    assert out["contributors"][0]["login"] == "bob"
    assert out["github"] == {"signed_in": True, "login": "bob"}
    assert out["share_url"] is None  # not a repo at all


def test_app_read_states_and_settled(env):
    d = env.app("a", proposal={"number": 4, "cache": {"state": "merged"}, "settled_seen": False})
    get = lambda: env.client.get("/api/contrib/app", params={"path": d}).json()  # noqa: E731
    assert get()["proposal"]["state"] == "merged"
    assert _post(env, "ack", path=d, kind="settled") == {"ok": True}
    assert get()["proposal"] is None
    mine = env.app("m", kind="own", repo="bob/m")
    out = env.client.get("/api/contrib/app", params={"path": mine}).json()
    assert out["inbox"] == {"items": [{"number": 7, "title": "x"}], "count": 1}


def test_app_read_disabled_and_unrecorded(env):
    d = env.app("a", kind=None)
    env.flags["sources"] = False
    out = env.client.get("/api/contrib/app", params={"path": d}).json()
    assert out["ok"] is False and out["reason"] == "disabled" and out["flags"]["sources"] is False
    env.flags["sources"] = True
    out = env.client.get("/api/contrib/app", params={"path": d}).json()
    assert out["pending"] is True and out["copy"] is None


# -- the listing ----------------------------------------------------------------


def test_listing_gains_contrib_only_with_sources_on_and_runs_no_git(env, monkeypatch):
    theirs = env.app("theirs")
    env.app("mine", kind="own", repo="bob/mine")
    env.app("remix", kind="own", repo="bob/remix", remix_of={"repo": "alice/x", "branch": "main"})
    env.app("new", kind=None)
    env.engine.status = _boom
    import subprocess
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
    assert rows["remix"]["contrib"]["label"] == "Remix of alice/x"
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
                   remix_of={"repo": "alice/x", "branch": "main"})
    env.calls.clear()
    refresh.note_app_opened(mine)
    assert ("ilist", True) in env.calls and ("original", mine) in env.calls
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
    assert _post(env, "refresh", path=d) == {"ok": True, "refreshed": True}
    assert ("prefresh", True) in env.calls
    assert _post(env, "refresh", path=d) == {"ok": True, "refreshed": False}


def test_sweep_counts_without_starting_reviews(env):
    mine = env.app("mine", kind="own", repo="bob/mine")
    env.app("theirs")
    assert refresh.sweep_once() == 1
    assert env.calls == [("ilist", False)]
    assert refresh.inbox_count(mine) == 1
    env.flags["inbox"] = False
    assert refresh.sweep_once() == 0
