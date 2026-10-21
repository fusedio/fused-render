"""End-to-end acceptance of the v2 GitHub contribution loop (CONTRIB_LOOP.md §16).

Three people on three "machines" (one home + one Fused workspace each, switched
by env) against one FakeGitHub of real bare repos. Everything a person does is a
request to the HTTP layer the page uses (GET /api/contrib/app, POST
/api/contrib/action, /restore, /merge/accept, /inbox/*). The only direct module
calls are the two triggers the page cannot make: "app open" / "Claude turn
committed" (`git_upstream.sync_repo`), plus the install path itself
(`appfile_git.install` from a staged clone) and plain git for edits.

ONE test, the story's steps in order (`_step1` … `_step7`): each step builds on
the last, and xdist's default distribution would split separate test functions
across workers. A failure names its step in the traceback.

Every page read asserts: no git words in the sentence, labels, confirms,
Activity lines or job messages (§0.1); at least one button in every state but
"merging" (whose Keep/Discard live on the merge review, `panel.merge`). Every
step diffs every branch on the fake before and after: only what a click (or
the author's own auto-publish, P5) should move may move (P2).

Stand-ins: the GitHub sign-in snapshot (always signed in), the model
(`ai._run_cli`), Claude's merge (`merge_job.claude_resolver`), the install
page's stamp/meta writers. `refresh._reset()` before a page read stands in for
time passing past the open throttle.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from _contrib_gh_world import APP_PY, PAGE, claude_commit, git

H = {"X-Fused": "1"}
URL = "https://github.com/alice/sine"
ALL_ON = dict.fromkeys(("sources", "changes", "inbox", "loop", "remix"), True)
ALL_OFF = dict.fromkeys(("sources", "changes", "inbox", "loop", "remix"), False)
#: tests/test_contrib_actions.py's §0.1 list (+ the frontend's "main").
GIT_WORDS = re.compile(r"\b(?:(?i:branch\w*|rebas\w*|fork\w*|commit\w*|push\w*|conflict\w*"
                       r"|origin|sha|main)|HEAD)\b")


class Machine:
    def __init__(self, login, base):
        self.login = login
        self.home = os.path.join(base, login, "home")
        self.ws = os.path.join(base, login, "Fused")
        os.makedirs(self.home)
        os.makedirs(os.path.join(self.ws, "app"))
        self.app = os.path.join(self.ws, "app", "sine")
        self.flags = dict(ALL_ON)


class World:
    pass


W = World()
CUR: list = [None]
RESOLVER_CALLS: list = []


def _resolver(wt, files, context):
    """Stand-in for Claude: keep both sides of every overlap, ours first."""
    RESOLVER_CALLS.append(list(files))
    for rel in files:
        p = os.path.join(wt, rel)
        with open(p, encoding="utf-8") as fh:
            lines = fh.read().splitlines(keepends=True)
        out, mode, ours, theirs = [], None, [], []
        for line in lines:
            if line.startswith("<<<<<<<"):
                mode, ours, theirs = "ours", [], []
            elif line.startswith("|||||||") and mode:
                mode = "base"
            elif line.startswith("=======") and mode:
                mode = "theirs"
            elif line.startswith(">>>>>>>") and mode:
                out += ours + [t for t in theirs if t not in ours]
                mode = None
            elif mode == "ours":
                ours.append(line)
            elif mode == "theirs":
                theirs.append(line)
            elif mode is None:
                out.append(line)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("".join(out))
    return {"ok": True, "summary": "Kept both versions."}


def _model(system, prompt, *, model=None, timeout=None, **_):
    return json.dumps({"verdict": "fine", "summary": "Makes the wave louder.", "risks": [],
                       "title": "Louder wave", "description": "Louder."})


@pytest.fixture
def world(tmp_path, monkeypatch):
    from fused_render import app_fused_dir, appfile, git_upstream, github_setup
    from fused_render.contrib import ai, classify, flags, gh, jobs, merge_job, refresh

    mp = monkeypatch
    base = os.path.realpath(str(tmp_path))
    # Whatever an earlier test in this worker left behind.
    mp.setattr(jobs, "_jobs", {})
    mp.setattr(merge_job, "_jobs", {})
    mp.setattr(git_upstream, "_sync_failures", {})
    mp.setattr(git_upstream, "_pulled_events", [])
    mp.setattr(git_upstream, "_pending", {})
    CUR[0] = None
    RESOLVER_CALLS.clear()
    gitconfig = os.path.join(base, "gitconfig")
    with open(gitconfig, "w") as fh:
        fh.write("[user]\n\tname = Someone\n\temail = someone@example.com\n"
                 "[init]\n\tdefaultBranch = main\n")
    mp.setenv("GIT_CONFIG_GLOBAL", gitconfig)
    mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
    mp.setenv(gh.FAKE_ENV, os.path.join(base, "github"))
    fake = gh.fake()
    for k, v in fake.git_env().items():
        mp.setenv(k, v)
    mp.setattr(gh, "_FORK_POLL_STEP_S", 0.01)
    mp.setattr(flags, "_raw", lambda: dict(CUR[0].flags))
    mp.setattr(github_setup, "cached_summary",
               lambda: {"signed_in": True, "account": CUR[0].login, "stale": False})
    mp.setattr(github_setup, "summary", lambda *a, **k: None)
    mp.setattr(github_setup, "summary_refreshed", lambda *a, **k: None, raising=False)
    mp.setattr(refresh, "_INLINE", True)
    mp.setattr(git_upstream, "auto_sync_enabled", lambda: True)
    mp.setattr(merge_job, "claude_resolver", _resolver)
    mp.setattr(ai, "_run_cli", _model)
    # The install page's stamp/meta writers read the .fused file the link
    # downloaded; the clone + record + guards are what is under test.
    mp.setattr(appfile, "write_stamp", lambda *a, **k: None)
    mp.setattr(app_fused_dir, "ensure", lambda *a, **k: None)

    for login, name in (("alice", "Alice"), ("bob", "Bob"), ("carol", "Carol")):
        fake.add_user(login, name)
    fake.create_repo_as("alice", "sine", {"index.html": PAGE, "app.py": APP_PY})

    W.fake, W.base, W.mp, W.gh, W.classify = fake, base, mp, gh, classify
    W.alice, W.bob, W.carol = (Machine(n, base) for n in ("alice", "bob", "carol"))

    from fused_render.server.routers.contrib import router
    fapp = FastAPI()
    fapp.include_router(router)
    W.client = TestClient(fapp)
    yield W
    quiesce()
    refresh._reset()


# -- machine switching ----------------------------------------------------------


def quiesce(timeout=60):
    """Every contrib background thread (jobs, merge jobs, classification) of
    the current machine finished — env is process-wide, so nothing may run
    across a switch."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        busy = [t for t in threading.enumerate()
                if (t.name.startswith("contrib-") or t.name.startswith("git-"))
                and not t.name.startswith("contrib-bg")
                and "sweep" not in t.name and t.is_alive()]
        if not busy:
            return
        time.sleep(0.05)
    raise AssertionError(f"background work still running: {[t.name for t in busy]}")


def use(m: Machine):
    from fused_render import git_upstream
    from fused_render.contrib import classify, engine, inbox, proposals, refresh, store

    if CUR[0] is not m:
        quiesce()
    CUR[0] = m
    W.mp.setenv("FUSED_RENDER_HOME", m.home)
    W.mp.setenv("FUSED_RENDER_DIR", m.ws)
    W.fake.sign_in(m.login)
    W.gh.forget_identity()
    refresh._reset()
    for mod, names in ((classify, ("_kind_memo", "_guarded")), (store, ("_bind_memo",)),
                       (engine, ("_status_cache", "_taken_cache")),
                       (git_upstream, ("_checked", "_state", "_last_fetch")),
                       (inbox, ("_lists", "_asked")), (proposals, ("_last_refresh",))):
        for n in names:
            getattr(mod, n).clear()
    return m


# -- what the page does ------------------------------------------------------------


def _texts(out):
    panel = out.get("panel") or {}
    yield "sentence", panel.get("sentence") or ""
    for a in out.get("actions") or []:
        yield f"label {a['id']}", a.get("label") or ""
        yield f"confirm {a['id']}", a.get("confirm") or ""
    for line in out.get("activity") or []:
        yield "activity", line.get("text") or ""


def page(m: Machine, *, fresh=True):
    """GET /api/contrib/app as the open page polls it, with the invariants
    every read must hold."""
    from fused_render.contrib import refresh

    use(m)
    if fresh:
        refresh._reset()  # the page re-opened past the open throttle
    r = W.client.get("/api/contrib/app", params={"path": m.app})
    assert r.status_code == 200, r.text
    out = r.json()
    if out.get("ok") is False:
        return out
    for where, text in _texts(out):
        assert not GIT_WORDS.search(text), f"git word in {where}: {text!r}"
    panel = out.get("panel")
    if panel and panel.get("state") == "merging":
        # Keep / Discard the merge are the merge review's (panel.merge), by design.
        assert panel.get("merge"), panel
    elif panel:
        assert out.get("actions"), f"no button at all in state {panel.get('state')}: {panel}"
    quiesce()
    return out


def ids(out):
    return [a["id"] for a in out.get("actions") or []]


def wait_job(job_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = W.client.get("/api/contrib/job", params={"id": job_id}).json()
        if j.get("status") != "running":
            assert not GIT_WORDS.search(j.get("message") or ""), j.get("message")
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


def act(m: Machine, action, *, confirm=False, wait=True, **args):
    use(m)
    body = {"path": m.app, "action": action, "args": args}
    if confirm:
        body["confirm"] = True
    out = W.client.post("/api/contrib/action", json=body, headers=H).json()
    if wait and out.get("ok") and out.get("job") and not out.get("merge"):
        out["done"] = wait_job(out["job"])
    if out.get("message"):
        assert not GIT_WORDS.search(out["message"]), out["message"]
    return out


def post(m: Machine, route, **body):
    use(m)
    out = W.client.post(f"/api/contrib/{route}", json={"path": m.app, **body},
                        headers=H).json()
    if out.get("message"):
        assert not GIT_WORDS.search(out["message"]), out["message"]
    return out


def wait_merge(job_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = W.client.get("/api/contrib/job", params={"id": job_id}).json()
        st = (j.get("result") or {}).get("status")
        if j.get("status") != "running" or st in ("ready", "choice", "failed"):
            assert not GIT_WORDS.search(j.get("message") or ""), j.get("message")
            return j
        time.sleep(0.05)
    raise AssertionError(f"merge {job_id} still running")


# -- triggers the page cannot make -------------------------------------------------


def app_open(m: Machine):
    from fused_render import git_upstream

    use(m)
    res = git_upstream.sync_repo(m.app, action="Auto-update on app open", push=False)
    quiesce()
    return res


def turn_committed(m: Machine, files, message):
    """A Claude turn in the app: files edited, one commit, then the
    after-turn sync (push=True, as the agent runs it)."""
    from fused_render import git_upstream

    use(m)
    claude_commit(m.app, files, message)
    res = git_upstream.sync_repo(m.app, action="Claude turn", push=True)
    quiesce()
    return res


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return fh.read()


def head(root):
    return git(root, "rev-parse", "HEAD")


def tip_of(full="alice/sine", branch="main"):
    return W.fake.tip(full, branch)


def alice_publish(files, message):
    """Alice edits her own copy and fused's after-turn sync publishes it."""
    res = turn_committed(W.alice, files, message)
    if res.get("status") == "skipped" and res.get("why") == "held":
        # §16.12: a fresh clone knows only one GitHub tip ("unknown"), so its
        # first ahead-only publish waits for Publish now.
        out = page(W.alice)
        assert "publish-now" in ids(out), (ids(out), out["panel"])
        r = act(W.alice, "publish-now")
        assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    else:
        assert res.get("status") in ("synced",), json.dumps(res, default=str)
    assert head(W.alice.app) == tip_of(), "Alice's change did not reach GitHub"
    return tip_of()


def refs():
    """Every branch of every repo on the fake: {(full, ref): sha}."""
    out = {}
    root = os.path.join(W.fake.root, "repos")
    for owner in sorted(os.listdir(root)):
        for name in sorted(os.listdir(os.path.join(root, owner))):
            bare = os.path.join(root, owner, name)
            full = f"{owner}/{name[:-4] if name.endswith('.git') else name}"
            for line in git(bare, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
                ref, sha = line.split()
                if not ref.startswith("refs/pull/"):
                    out[(full, ref)] = sha
    return out


def moved(before, after):
    return {k for k in set(before) | set(after) if before.get(k) != after.get(k)}


def no_pushes(before, what):
    got = moved(before, refs())
    assert not got, f"{what} moved GitHub without a click: {sorted(got)}"


def install(m: Machine):
    from fused_render import appfile_git

    use(m)
    stage_root = appfile_git._staging_root(URL)
    shutil.rmtree(stage_root, ignore_errors=True)
    os.makedirs(stage_root)
    stage = os.path.join(stage_root, "sine")
    git(W.base, "clone", "-q", URL, stage)
    commit = head(stage)
    name, app_id = appfile_git.expected_identity(stage, URL)
    target = {"git": {"url": URL, "commit": commit}, "state": "new", "path": m.app,
              "found_by": None}
    done = appfile_git.install("x.fused", target,
                               {"name": name, "app_id": app_id, "entry": "index.html"},
                               force=False)
    assert done["action"] == "installed"
    quiesce()


def louder(n):
    return APP_PY.replace("return 1", f"return {n}")


# ==================================================================== the story


def _step1_publish_and_install():
    # Alice's own copy of her app (she published it: the repo on GitHub).
    use(W.alice)
    git(W.base, "clone", "-q", URL, W.alice.app)
    out = page(W.alice)
    assert out["ok"], out
    assert (out["panel"] or {}).get("kind") in ("own",), out["panel"]
    before = refs()
    for m in (W.bob, W.carol):
        install(m)
        out = page(m)
        p = out["panel"]
        assert p["kind"] == "github" and p["state"] == "following", p
        assert ids(out) == ["check"], ids(out)
        assert head(m.app) == tip_of()
    no_pushes(before, "install / first open")


def _step2_changes_bring_in_undo():
    before = refs()
    res = turn_committed(W.bob, {"app.py": louder(3)}, "louder wave")
    no_pushes(before, "Bob's Claude turn")
    out = page(W.bob)
    assert out["panel"]["state"] == "changes", out["panel"]
    assert "propose" in ids(out), ids(out)
    assert out["panel"]["files"] == ["app.py"], out["panel"]
    bob_mine = head(W.bob.app)

    alice_tip = alice_publish({"index.html": PAGE.replace("blue", "green")}, "green wave")
    before = refs()
    app_open(W.bob)
    assert read(W.bob.app, "index.html") == PAGE.replace("blue", "green")
    assert read(W.bob.app, "app.py") == louder(3)
    out = page(W.bob)
    assert out["panel"]["state"] == "changes", out["panel"]
    lines = [x for x in out["activity"] if "alice" in x.get("text", "")]
    assert lines, out["activity"]
    line = lines[0]
    assert line.get("undo"), f"automatic combine line has no Undo: {line}"
    assert "Claude:" not in line["text"], line

    app_open(W.carol)
    assert head(W.carol.app) == alice_tip
    out = page(W.carol)
    assert out["panel"]["state"] == "following" and ids(out) == ["check"], out
    no_pushes(before, "app opens")

    # Undo the automatic combine.
    r = post(W.bob, "restore", id=line["id"])
    assert r.get("ok"), r
    assert head(W.bob.app) == bob_mine
    assert read(W.bob.app, "index.html") == PAGE
    out = page(W.bob)
    assert "bring-in" in ids(out), ids(out)
    # The next automatic open must not silently redo what was just undone.
    app_open(W.bob)
    assert head(W.bob.app) == bob_mine, "auto-sync re-merged right after Undo"
    out = page(W.bob)
    assert "bring-in" in ids(out), ids(out)
    r = act(W.bob, "bring-in")
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    assert read(W.bob.app, "index.html") == PAGE.replace("blue", "green")
    assert read(W.bob.app, "app.py") == louder(3)
    git(W.bob.app, "merge-base", "--is-ancestor", alice_tip, "HEAD")
    no_pushes(before, "Undo / Bring in")


def _step3_propose_overlap_merge_update():
    write(W.bob.app, "notes.md", "why louder\n")
    out = page(W.bob)
    assert "notes.md" in (out["panel"].get("untracked") or []), out["panel"]
    before = refs()
    r = act(W.bob, "propose", title="Louder wave", description="Makes it louder.",
            include=["notes.md"])
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    changed = moved(before, refs())
    assert changed and all(full == "bob/sine" for full, _ in changed), changed
    pr, = W.fake.state()["pulls"]["alice/sine"]
    W.pr_number = pr["number"]
    W.pr_branch = pr["head_branch"]
    assert W.fake.read("bob/sine", "notes.md", f"refs/heads/{W.pr_branch}") == "why louder\n"
    W.sent = W.fake.tip("bob/sine", W.pr_branch)
    out = page(W.bob)
    assert out["panel"]["state"] == "proposed", out["panel"]

    # Alice's Proposals tab: the proposal, with Claude's review.
    deadline = time.time() + 60
    item = None
    while time.time() < deadline:
        out = page(W.alice)
        items = ((out.get("inbox") or {}).get("items")) or []
        item = next((i for i in items if i.get("number") == W.pr_number), None)
        if item and item.get("review_status") == "ready":
            break
        time.sleep(0.2)
    assert item and item.get("review_status") == "ready", item
    assert (item.get("review") or {}).get("verdict") == "fine", item

    # Alice changes the same line.
    before = refs()
    alice_publish({"app.py": louder(5)}, "even louder")
    pushed = moved(before, refs())
    assert pushed == {("alice/sine", "refs/heads/main")}, pushed
    before = refs()
    bob_head = head(W.bob.app)
    app_open(W.bob)
    assert head(W.bob.app) == bob_head, "overlap moved Bob's copy"
    out = page(W.bob)
    p = out["panel"]
    assert p["state"] == "needs-merge", p
    want = {"merge-claude", "keep-mine", "use-theirs"}
    assert want <= set(ids(out)), ids(out)
    assert any(a["id"] == "use-theirs" and "alice" in a["label"] for a in out["actions"])

    r = act(W.bob, "merge-claude")
    assert r.get("ok") and r.get("job"), r
    j = wait_merge(r["job"])
    assert (j.get("result") or {}).get("status") == "ready", j
    assert RESOLVER_CALLS and RESOLVER_CALLS[-1] == ["app.py"], RESOLVER_CALLS
    out = page(W.bob)
    assert out["panel"]["state"] == "merging", out["panel"]
    r = post(W.bob, "merge/accept", job=r["job"])
    assert r.get("ok"), r
    quiesce()
    git(W.bob.app, "merge-base", "--is-ancestor", tip_of(), "HEAD")
    git(W.bob.app, "merge-base", "--is-ancestor", bob_head, "HEAD")
    no_pushes(before, "overlap + Merge with Claude")

    out = page(W.bob)
    assert out["panel"]["state"] == "proposed", out["panel"]
    assert "update-proposal" in ids(out), (ids(out), out["panel"])
    r = act(W.bob, "update-proposal")
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    changed = moved(before, refs())
    assert changed == {("bob/sine", f"refs/heads/{W.pr_branch}")}, changed
    new = W.fake.tip("bob/sine", W.pr_branch)
    assert new == head(W.bob.app)
    git(W.fake.bare_path("bob/sine"), "merge-base", "--is-ancestor", W.sent, new)
    W.sent = new


def _step4_accept_squash_return():
    # A post-send edit Bob keeps.
    before = refs()
    turn_committed(W.bob, {"later.txt": "after sending\n"}, "later idea")
    no_pushes(before, "Bob's post-send turn")
    # Step 5's setup: Alice has an unpublished edit when she accepts.
    use(W.alice)
    claude_commit(W.alice.app, {"alice_notes.txt": "mine\n"}, "notes")
    W.alice_edit = head(W.alice.app)

    W.fake.set_merge_methods("alice/sine", merge=False, squash=True, rebase=False)
    before = refs()
    item = None
    deadline = time.time() + 60
    while time.time() < deadline:
        out = page(W.alice)
        items = ((out.get("inbox") or {}).get("items")) or []
        item = next((i for i in items if i.get("number") == W.pr_number), None)
        if item and item.get("review_status") == "ready" and \
                (item.get("review") or {}).get("head_sha") == item.get("head_sha"):
            break
        time.sleep(0.2)
    assert item is not None, out.get("inbox")
    assert item.get("fits") is True, item
    done = None
    for _ in range(4):
        r = post(W.alice, "inbox/accept", number=W.pr_number)
        assert r.get("ok") and r.get("job"), r
        done = wait_job(r["job"])
        if done.get("status") == "done" or done.get("reason") != "review-needed":
            break
        quiesce()
        time.sleep(0.5)
    assert done.get("status") == "done", done
    quiesce()
    W.accepted_tip = tip_of()
    pr, = W.fake.state()["pulls"]["alice/sine"]
    assert pr.get("merged"), pr
    changed = moved(before, refs())
    assert {k[0] for k in changed} <= {"alice/sine", "bob/sine"}, changed

    before = refs()
    app_open(W.bob)
    out = page(W.bob)
    p = out["panel"]
    assert p["state"] == "merged", p
    assert "merged your change" in p["sentence"], p
    assert "propose" in ids(out), ids(out)  # §16.12: merged with kept edits
    tip = tip_of()
    git(W.bob.app, "merge-base", "--is-ancestor", tip, "HEAD")
    diff = git(W.bob.app, "diff", "--name-only", tip, "HEAD").split()
    assert diff == ["later.txt"], diff
    first_parent = git(W.bob.app, "log", "--first-parent", "--format=%H", f"{tip}..HEAD").split()
    assert len(first_parent) == 1, first_parent
    assert any(x.get("undo") and "merged your change" in x.get("text", "")
               for x in out["activity"]), out["activity"]
    app_open(W.carol)
    assert head(W.carol.app) == tip
    out = page(W.carol)
    assert out["panel"]["state"] == "following", out["panel"]
    no_pushes(before, "the return / Carol's update")


def _step4b_undo_return_then_bring_in():
    """Partial cover for step 2's Undo path: the return line IS undoable."""
    before = refs()
    out = page(W.bob)
    landed = head(W.bob.app)
    line = next(x for x in out["activity"] if x.get("undo") and "merged your change" in x["text"])
    r = post(W.bob, "restore", id=line["id"])
    assert r.get("ok"), r
    assert head(W.bob.app) != landed
    out = page(W.bob)
    assert out["panel"]["state"] != "following" and "bring-in" in ids(out), (out["panel"], ids(out))
    app_open(W.bob)
    undone = head(W.bob.app)
    out = page(W.bob)
    if "bring-in" in ids(out):
        r = act(W.bob, "bring-in")
        assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    diff = git(W.bob.app, "diff", "--name-only", tip_of(), "HEAD").split()
    assert diff == ["later.txt"], (diff, undone == head(W.bob.app))
    no_pushes(before, "Undo / Bring in")


def _step5_alice_unpublished_edit_combined():
    from fused_render import git_upstream

    use(W.alice)
    tip = tip_of()
    assert W.fake.read("alice/sine", "alice_notes.txt") == "mine\n", "Alice's edit not published"
    assert W.fake.read("alice/sine", "later.txt") is None
    assert W.fake.read("alice/sine", "notes.md") == "why louder\n"
    assert head(W.alice.app) == tip
    rows = [r for r in git_upstream.sync_failures()
            if os.path.realpath(str(r.get("root") or r.get("path") or "")) == W.alice.app]
    assert not rows, rows
    out = page(W.alice)
    assert out["panel"]["state"] not in ("github-changed", "needs-merge", "edits"), out["panel"]


def _step6_flags_off_hand_edit():
    from fused_render import git_upstream
    from fused_render.contrib import store

    W.carol.flags = dict(ALL_OFF)
    use(W.carol)
    carol_head = head(W.carol.app)
    write(W.carol.app, "index.html", PAGE.replace("blue", "red"))
    alice_publish({"index.html": PAGE.replace("blue", "purple")}, "purple wave")
    before = refs()
    app_open(W.carol)
    assert head(W.carol.app) == carol_head
    assert read(W.carol.app, "index.html") == PAGE.replace("blue", "red")
    rec = store.get(W.carol.app) or {}
    assert not rec.get("needs_merge"), rec.get("needs_merge")
    rows = [r for r in git_upstream.sync_failures() if os.path.realpath(
        str(r.get("root") or r.get("path") or "")) == W.carol.app]
    assert rows and rows[0].get("reason") in ("dirty", "diverged", "git-failed"), \
        git_upstream.sync_failures()
    out = page(W.carol)
    assert out.get("reason") == "disabled", out
    # The same overlap, committed by hand.
    git(W.carol.app, "commit", "-q", "-am", "red")
    carol_head = head(W.carol.app)
    git_upstream._checked.clear()
    app_open(W.carol)
    assert head(W.carol.app) == carol_head
    rec = store.get(W.carol.app) or {}
    assert not rec.get("needs_merge"), rec.get("needs_merge")
    rows = [r for r in git_upstream.sync_failures() if os.path.realpath(
        str(r.get("root") or r.get("path") or "")) == W.carol.app]
    assert rows and any(r.get("reason") in ("diverged", "git-failed") for r in rows), rows
    no_pushes(before, "Carol's opens with the loop off")


def _step7_remix():
    out = page(W.bob)
    assert "remix" in ids(out), ids(out)
    before = refs()
    r = act(W.bob, "remix", name="sine-remix",
          credit="Based on alice's Sine.")
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    changed = moved(before, refs())
    repos = {k[0] for k in changed}
    assert len(repos) == 1 and next(iter(repos)).startswith("bob/"), changed
    W.remix = next(iter(repos))
    out = page(W.bob)
    assert out["panel"]["kind"] == "remix", out["panel"]

    alice_publish({"app.py": louder(5).replace("return 2", "return 4")}, "faster")
    before = refs()
    out = page(W.bob)
    p = out["panel"]
    assert p["state"] == "original-moved", p
    assert "bring-in" in ids(out), ids(out)
    no_pushes(before, "remix open")
    r = act(W.bob, "bring-in")
    assert r.get("ok") and r.get("job"), r
    j = wait_merge(r["job"])
    assert (j.get("result") or {}).get("status") == "ready", j
    r = post(W.bob, "merge/accept", job=r["job"])
    assert r.get("ok"), r
    quiesce()
    assert "return 4" in read(W.bob.app, "app.py")
    changed = moved(before, refs())
    assert {k[0] for k in changed} <= {W.remix}, changed
    if "return 4" not in (W.fake.read(W.remix, "app.py") or ""):
        out = page(W.bob)
        from fused_render import git_upstream
        raise AssertionError(f"remix not published: panel={out['panel']} ids={ids(out)} "
                             f"activity={[x['text'] for x in out['activity']][:3]} "
                             f"failures={git_upstream.sync_failures()}")
    out = page(W.bob)
    assert out["panel"]["state"] != "original-moved", out["panel"]


def test_contrib_loop_acceptance(world):
    _step1_publish_and_install()
    _step2_changes_bring_in_undo()
    _step3_propose_overlap_merge_update()
    _step4_accept_squash_return()
    _step4b_undo_return_then_bring_in()
    _step5_alice_unpublished_edit_combined()
    _step6_flags_off_hand_edit()
    _step7_remix()
