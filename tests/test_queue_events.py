"""The project queue's event transport — the three posts and the one endpoint.

design.md: "No polls in queue logic". The queue only ever moves on events, and
three of them are known nowhere but inside a process the server spawned and
does not otherwise talk to — the session host (a turn ended; the session is
gone) and the permission server (a card went up). Both are TEMPLATES: stdlib
only, no `fused_render` import, so the wire between them and the queue is one
small HTTP POST each.

What is under test here is that wire, end to end but in three pieces:

* the host posts `turn_ended` on the True->False edge and `exited` exactly once
  on its way out, and stays silent with no origin in the environment;
* the permission server posts `card_raised` when it parks the request file, and
  BEFORE it blocks on the answer;
* the endpoint guards on X-Fused, shrugs while the flag is off, resolves the
  task key off the run dir when the body did not carry one, and hands the
  manager the event.

The manager itself is a fake everywhere below (`queue_manager.reset_for_tests`)
— what it DOES with an event has its own suite; what this file cares about is
that the event arrives, once, with the right task key.
"""
import importlib.util
import json
import os
import threading
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from fused_render import project_queue, queue_manager, tasks_store
from fused_render.server import create_app

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")
HEADERS = {"X-Fused": "1"}


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("queue_events_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------- the fake wire


class _Posts:
    """Every POST the module under test made, captured off `urlopen`.

    The real call is patched rather than a real server stood up because the
    posting side is deliberately deaf: it swallows every failure, so a test
    that pointed it at a dead port would pass whether or not the request was
    ever built. Capturing the `Request` object is the only way to assert the
    header, the method and the body actually went out right.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.seen = []

    def urlopen(self, req, timeout=None):
        with self.lock:
            self.seen.append({
                "url": req.full_url,
                "method": req.get_method(),
                "fused": req.headers.get("X-fused"),
                "timeout": timeout,
                "body": json.loads(req.data.decode("utf-8")),
            })

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return b"{}"

        return _Resp()

    def of_kind(self, kind):
        with self.lock:
            return [p for p in self.seen if p["body"].get("kind") == kind]

    def wait_for(self, kind, count=1, timeout=5.0):
        """The posts run on daemon threads, so every assertion about one is a
        wait, not a read."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = self.of_kind(kind)
            if len(got) >= count:
                return got
            time.sleep(0.01)
        raise AssertionError(
            "no %r post after %ss (saw %r)"
            % (kind, timeout, [p["body"].get("kind") for p in self.seen]))


@pytest.fixture()
def posts(monkeypatch):
    p = _Posts()
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", p.urlopen)
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:9/")
    return p


# ------------------------------------------------------------- the session host


class _FakeStdin:
    def __init__(self):
        self.closed = False

    def write(self, data):
        pass

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeCli:
    """Never dies on its own until `dead` is flipped — the test drives the
    lifetime, the way the real CLI's own exit would."""

    def __init__(self):
        self.stdin = _FakeStdin()
        self.dead = False

    def poll(self):
        return 7 if self.dead else None

    def wait(self, timeout=None):
        pass


@pytest.fixture()
def host_run(tmp_path):
    run_dir = tmp_path / "runs" / "20260917-120000-abcd"
    run_dir.mkdir(parents=True)
    (run_dir / "out.jsonl").write_text("")
    (run_dir / "meta.json").write_text(json.dumps({"session_id": "sess-1"}))
    (run_dir / "host.json").write_text("{}")
    return run_dir


def _drive_host(monkeypatch, host, run_dir, states):
    """Run `_reap_loop` against a fake CLI, feeding it `states` one per tick
    and killing the CLI once they run out."""
    monkeypatch.setattr(host, "_IDLE_REAP_SECONDS", 9999)
    monkeypatch.setattr(host, "_DRAIN_INTERVAL_SECONDS", 0.001)
    cli = _FakeCli()
    seq = list(states)

    def fake_state(agent, rd, cache):
        if seq:
            return seq.pop(0)
        cli.dead = True
        return (False, False)

    monkeypatch.setattr(host, "_turn_state_if_grown", fake_state)
    host._reap_loop(None, str(run_dir), cli, str(run_dir / "host.json"))
    return cli


def test_the_host_posts_turn_ended_once_when_a_result_row_lands(
        monkeypatch, posts, host_run):
    """True->False is the event; False->False, five times a second for the
    rest of an idle session, is not."""
    host = _load("session_host")
    _drive_host(monkeypatch, host, host_run,
                [(True, False), (False, False), (False, False), (False, False)])

    ended = posts.wait_for("turn_ended")
    assert len(ended) == 1, "the closed turn was announced on every idle tick"
    body = ended[0]["body"]
    assert body["run_id"] == "20260917-120000-abcd"
    assert body["session_id"] == "sess-1"
    assert ended[0]["url"] == "http://127.0.0.1:9/api/tasks/queue/event"
    assert ended[0]["method"] == "POST"
    assert ended[0]["fused"] == "1", "the endpoint's D3 guard would 403 this"
    assert ended[0]["timeout"] == host._EVENT_TIMEOUT


def test_the_host_announces_a_second_turn_on_the_same_session(
        monkeypatch, posts, host_run):
    """One host owns the CLI's stdin for a WHOLE chat: a follow-up reopens the
    turn on the same process, and its end is a second event, not a repeat."""
    host = _load("session_host")
    _drive_host(monkeypatch, host, host_run,
                [(True, False), (False, False), (True, False), (False, False)])

    assert len(posts.wait_for("turn_ended", 2)) == 2


def test_the_host_posts_exited_with_the_childs_return_code(
        monkeypatch, posts, host_run):
    host = _load("session_host")
    _drive_host(monkeypatch, host, host_run, [(True, False), (False, False)])

    exited = posts.wait_for("exited")
    assert len(exited) == 1
    assert exited[0]["body"]["code"] == 7
    assert exited[0]["body"]["run_id"] == "20260917-120000-abcd"


def test_the_host_posts_exited_on_the_idle_reap_too(monkeypatch, posts, host_run):
    """The reap `break` ends the session just as finally as the CLI dying, and
    the folder's owner is just as gone."""
    host = _load("session_host")
    monkeypatch.setattr(host, "_IDLE_REAP_SECONDS", 0.001)
    monkeypatch.setattr(host, "_DRAIN_INTERVAL_SECONDS", 0.001)
    cli = _FakeCli()
    monkeypatch.setattr(host, "_turn_state_if_grown",
                        lambda agent, rd, cache: (False, False))
    host._reap_loop(None, str(host_run), cli, str(host_run / "host.json"))

    assert len(posts.wait_for("exited")) == 1


def test_exited_is_posted_as_soon_as_the_child_is_gone(monkeypatch, posts,
                                                        host_run):
    """M7, 2026-09-17. An error, a rate limit or a cancel tears the turn down
    with no `result` row ever written, so `exited` is the ONLY word the queue
    gets — and if it waited on the 30 s idle reap, the folder stayed held for
    half a minute after the process was dust. The loop's own condition is
    `cli.poll() is None`, so it leaves on the very next drain tick; nothing here
    may reintroduce a per-idle-tick check.

    Real sleeps, default drain interval: what is measured is the lag, so the
    knob under test is not turned off."""
    host = _load("session_host")
    monkeypatch.setattr(host, "_IDLE_REAP_SECONDS", 9999)
    assert host._DRAIN_INTERVAL_SECONDS <= 1.0
    cli = _FakeCli()

    # The turn NEVER closes — no result row, ever. The child just dies.
    def fake_state(agent, rd, cache):
        return (True, False)

    monkeypatch.setattr(host, "_turn_state_if_grown", fake_state)

    def kill():
        time.sleep(0.05)
        cli.dead = True
        killed.append(time.monotonic())

    killed = []
    threading.Thread(target=kill, daemon=True).start()
    host._reap_loop(None, str(host_run), cli, str(host_run / "host.json"))

    exited = posts.wait_for("exited")
    assert len(exited) == 1
    assert posts.of_kind("turn_ended") == [], "no result row ever landed"
    assert time.monotonic() - killed[0] < 1.0, \
        "the folder stayed held long after the process was gone"


def test_a_post_that_cannot_connect_is_tried_once_more(monkeypatch, posts,
                                                       host_run):
    """Belt for the spawn storm: the one failure worth retrying is the one that
    is over a second later — the server restarting, a listen backlog full for
    an instant. One retry, then silence."""
    host = _load("session_host")
    monkeypatch.setattr(host, "_EVENT_RETRY_SECONDS", 0.01)
    assert host._EVENT_TIMEOUT == 5.0
    tries = []
    real = posts.urlopen

    def flaky(req, timeout=None):
        tries.append(req.full_url)
        if len(tries) == 1:
            raise OSError("connection refused")
        return real(req, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    host._send_event("http://127.0.0.1:9" + host._EVENT_PATH,
                     {"kind": "exited", "run_id": "r1"})

    assert len(tries) == 2
    assert len(posts.of_kind("exited")) == 1


def test_a_post_that_keeps_failing_gives_up_after_the_retry(monkeypatch, posts,
                                                            host_run):
    host = _load("session_host")
    monkeypatch.setattr(host, "_EVENT_RETRY_SECONDS", 0.01)
    tries = []

    def boom(req, timeout=None):
        tries.append(1)
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    host._send_event("http://127.0.0.1:9" + host._EVENT_PATH, {"kind": "exited"})

    assert len(tries) == 2, "one retry, not a loop"


def test_the_host_falls_back_to_the_resumed_session_id(
        monkeypatch, posts, host_run):
    """A resume writes `resumed_from` and no minted `session_id`; that is still
    the conversation the Tasks page keys on."""
    (host_run / "meta.json").write_text(json.dumps({"resumed_from": "sess-old"}))
    host = _load("session_host")
    _drive_host(monkeypatch, host, host_run, [(True, False), (False, False)])

    assert posts.wait_for("turn_ended")[0]["body"]["session_id"] == "sess-old"


def test_the_host_sends_an_empty_session_id_when_meta_is_unreadable(
        monkeypatch, posts, host_run):
    """Not an error and not a guess — the endpoint re-derives it from the run
    dir, which is the authority anyway."""
    (host_run / "meta.json").write_text("{ not json")
    host = _load("session_host")
    _drive_host(monkeypatch, host, host_run, [(True, False), (False, False)])

    assert posts.wait_for("turn_ended")[0]["body"]["session_id"] == ""


def test_the_host_posts_nothing_without_an_origin(monkeypatch, posts, host_run):
    """A host running standalone (or started by an older server) has nobody to
    tell, and must not spend a thread or a socket finding that out."""
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host = _load("session_host")
    _drive_host(monkeypatch, host, host_run, [(True, False), (False, False)])

    time.sleep(0.1)
    assert posts.seen == []


def test_a_dead_server_never_costs_the_session_a_turn(monkeypatch, posts, host_run):
    """Fire and forget means FORGET: the post raising must not reach the reap
    loop, which is also the thing draining the user's follow-up messages."""
    import urllib.request

    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    host = _load("session_host")
    cli = _drive_host(monkeypatch, host, host_run, [(True, False), (False, False)])

    assert cli.stdin.closed, "the loop tore the session down as usual"
    assert not (host_run / "host.json").exists()


# --------------------------------------------------------- the permission server


def test_the_permission_server_posts_card_raised_when_it_parks_a_request(
        tmp_path, monkeypatch, posts):
    srv = _load("permission_server")
    run_dir = tmp_path / "runs" / "20260917-130000-beef"
    perm = run_dir / "perm"
    perm.mkdir(parents=True)
    monkeypatch.setattr(srv, "PERM_DIR", str(perm))

    parked = {}

    def fake_await(req_id):
        # The card is on screen for as long as this blocks — the post must
        # already have gone out by now, or the queue hears about a card only
        # once it has been answered.
        parked["id"] = req_id
        parked["posted"] = len(posts.of_kind("card_raised"))
        return {"decision": "allow"}

    monkeypatch.setattr(srv, "_await_decision", fake_await)
    srv._handle_approve({"tool_name": "Bash", "input": {"command": "ls"}})

    raised = posts.wait_for("card_raised")
    assert len(raised) == 1
    body = raised[0]["body"]
    assert body["run_id"] == "20260917-130000-beef", \
        "the run id is the perm dir's parent, not the perm dir"
    assert body["request_id"] == parked["id"]
    assert raised[0]["fused"] == "1"
    assert os.path.exists(os.path.join(perm, parked["id"] + ".req.json"))


def test_the_app_state_request_raises_no_card(tmp_path, monkeypatch, posts):
    """`app_state` parks a request file too, in the OTHER directory, and it is
    deliberately not a card — nothing is waiting on a human."""
    srv = _load("permission_server")
    state = tmp_path / "runs" / "20260917-130000-beef" / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(srv, "STATE_DIR", str(state))
    monkeypatch.setattr(srv, "_await_answer", lambda *a, **kw: {"state": {}})
    srv._handle_app_state({"reason": "after an edit"})

    time.sleep(0.1)
    assert posts.of_kind("card_raised") == []


def test_the_permission_server_posts_card_cleared_when_the_decision_lands(
        tmp_path, monkeypatch, posts):
    """The card came down; the task is a RUNNING task again and wants its
    folder back. Posted from the wait itself, because that is the one place
    every route to a decision passes through — the page's endpoint, the
    terminal, a file dropped beside the request."""
    srv = _load("permission_server")
    perm = tmp_path / "runs" / "20260917-130000-beef" / "perm"
    perm.mkdir(parents=True)
    monkeypatch.setattr(srv, "PERM_DIR", str(perm))
    (perm / "req-7.res.json").write_text(json.dumps({"decision": "allow"}))

    assert srv._await_decision("req-7") == {"decision": "allow"}

    cleared = posts.wait_for("card_cleared")
    assert len(cleared) == 1
    body = cleared[0]["body"]
    assert body["run_id"] == "20260917-130000-beef"
    assert body["request_id"] == "req-7"
    assert cleared[0]["fused"] == "1"


def test_a_card_that_timed_out_is_cleared_too(tmp_path, monkeypatch, posts):
    """Nobody answered, so this server wrote the deny itself — the card is off
    the screen and the CLI is already acting on a verdict either way."""
    srv = _load("permission_server")
    perm = tmp_path / "runs" / "r1" / "perm"
    perm.mkdir(parents=True)
    monkeypatch.setattr(srv, "PERM_DIR", str(perm))
    monkeypatch.setattr(srv, "WAIT_TIMEOUT", 0.0)

    assert srv._await_decision("req-9")["decision"] == "deny"
    assert len(posts.wait_for("card_cleared")) == 1


def test_one_card_raises_once_and_clears_once(tmp_path, monkeypatch, posts):
    """End to end through the tool handler: up when the request is parked, down
    when the answer lands, and exactly one of each."""
    srv = _load("permission_server")
    perm = tmp_path / "runs" / "r1" / "perm"
    perm.mkdir(parents=True)
    monkeypatch.setattr(srv, "PERM_DIR", str(perm))

    def answer_soon():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reqs = [f for f in os.listdir(perm) if f.endswith(".req.json")]
            if reqs:
                req_id = reqs[0][: -len(".req.json")]
                (perm / (req_id + ".res.json")).write_text(
                    json.dumps({"decision": "allow"}))
                return
            time.sleep(0.01)

    threading.Thread(target=answer_soon, daemon=True).start()
    srv._handle_approve({"tool_name": "Bash", "input": {"command": "ls"}})

    assert len(posts.wait_for("card_raised")) == 1
    assert len(posts.wait_for("card_cleared")) == 1


def test_the_permission_server_posts_nothing_without_an_origin(
        tmp_path, monkeypatch, posts):
    srv = _load("permission_server")
    perm = tmp_path / "runs" / "r1" / "perm"
    perm.mkdir(parents=True)
    monkeypatch.setattr(srv, "PERM_DIR", str(perm))
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    monkeypatch.setattr(srv, "_await_decision", lambda req_id: {"decision": "allow"})
    srv._handle_approve({"tool_name": "Bash", "input": {"command": "ls"}})

    time.sleep(0.1)
    assert posts.seen == []


# ------------------------------------------------------------------ the endpoint


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    house = tmp_path / "home"
    (house / ".fused-render").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(house))
    monkeypatch.setenv("USERPROFILE", str(house))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(house / ".fused-render"))
    return house


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state" / "claude-sessions"
    d.mkdir(parents=True)
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _clear_caches():
    project_queue.reset_cache()
    yield
    project_queue.reset_cache()


@pytest.fixture()
def flag(home):
    def on(value=True):
        (home / ".fused-render" / "prefs.json").write_text(
            json.dumps({"project_queue_enabled": value}))
        assert project_queue.enabled() is value
    return on


class _FakeManager:
    def __init__(self):
        self.calls = []

    def turn_ended(self, task_key, run_id=""):
        self.calls.append(("turn_ended", task_key, run_id))

    def exited(self, task_key, run_id="", code=None):
        self.calls.append(("exited", task_key, run_id, code))

    def card_raised(self, task_key, run_id=""):
        self.calls.append(("card_raised", task_key, run_id))

    def card_cleared(self, task_key, run_id, request_id):
        self.calls.append(("card_cleared", task_key, run_id, request_id))


@pytest.fixture()
def manager(monkeypatch):
    fake = _FakeManager()
    queue_manager.reset_for_tests(fake)
    # `queue_manager.get()` is still the API STUB while task T1 is in flight
    # (it raises NotImplementedError and never consults `_manager`). Patching
    # it to hand back the installed manager is what the real one will do, so
    # this line becomes a no-op equivalent the day T1 lands rather than
    # something to come back and delete.
    monkeypatch.setattr(queue_manager, "get",
                        lambda: queue_manager._manager or fake)
    yield fake
    queue_manager.reset_for_tests(None)


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def runs(tmp_path, monkeypatch):
    """A fake agent module whose RUNS is ours — the endpoint's task-key
    fallback reads `meta.json` out of it."""
    root = tmp_path / "agent-runs"
    root.mkdir()

    class _Agent:
        RUNS = str(root)

    monkeypatch.setattr(project_queue, "agent_module", lambda: _Agent)
    return root


def test_the_endpoint_needs_the_fused_header(client):
    r = client.post("/api/tasks/queue/event",
                    json={"kind": "turn_ended", "run_id": "r1"})
    assert r.status_code == 403


def test_an_unknown_kind_is_a_400(client, flag, manager):
    flag(True)
    r = client.post("/api/tasks/queue/event", headers=HEADERS,
                    json={"kind": "exploded", "run_id": "r1",
                          "session_id": "sess-1"})
    assert r.status_code == 400
    assert manager.calls == []


def test_the_flag_off_shrugs(client, flag, manager):
    """The posts are unconditional; THIS is the gate. Nothing is read, nothing
    is resolved, and the manager is never even built."""
    flag(False)
    r = client.post("/api/tasks/queue/event", headers=HEADERS,
                    json={"kind": "turn_ended", "run_id": "r1",
                          "session_id": "sess-1"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "ignored": True}
    assert manager.calls == []


@pytest.mark.parametrize("kind,expected", [
    ("turn_ended", ("turn_ended", "sess-1", "r1")),
    ("card_raised", ("card_raised", "sess-1", "r1")),
])
def test_an_event_reaches_the_manager(client, flag, manager, kind, expected):
    flag(True)
    r = client.post("/api/tasks/queue/event", headers=HEADERS,
                    json={"kind": kind, "run_id": "r1", "session_id": "sess-1",
                          "request_id": "req-9"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert manager.calls == [expected]


def test_card_cleared_reaches_the_manager_with_its_request_id(client, flag,
                                                              manager):
    """The request id is not decoration: the manager matches the answer against
    the card it parked, and a `card_cleared` for a question that is not the one
    outstanding must not un-block the task."""
    flag(True)
    r = client.post("/api/tasks/queue/event", headers=HEADERS,
                    json={"kind": "card_cleared", "run_id": "r1",
                          "session_id": "sess-1", "request_id": "req-7"})
    assert r.status_code == 200
    assert manager.calls == [("card_cleared", "sess-1", "r1", "req-7")]


def test_a_manager_with_no_card_cleared_yet_is_not_an_error(client, flag,
                                                            manager,
                                                            monkeypatch):
    """`getattr`-guarded while T1's method lands: a half-landed tree drops the
    event, it does not 500 the daemon thread that sent it."""
    flag(True)
    monkeypatch.delattr(type(manager), "card_cleared", raising=True)
    r = client.post("/api/tasks/queue/event", headers=HEADERS,
                    json={"kind": "card_cleared", "run_id": "r1",
                          "session_id": "sess-1", "request_id": "req-7"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert manager.calls == []


def test_exited_carries_the_return_code(client, flag, manager):
    flag(True)
    r = client.post("/api/tasks/queue/event", headers=HEADERS,
                    json={"kind": "exited", "run_id": "r1",
                          "session_id": "sess-1", "code": 7})
    assert r.status_code == 200
    assert manager.calls == [("exited", "sess-1", "r1", 7)]


def test_a_missing_code_is_none_not_a_guess(client, flag, manager):
    flag(True)
    client.post("/api/tasks/queue/event", headers=HEADERS,
                json={"kind": "exited", "run_id": "r1", "session_id": "sess-1"})
    assert manager.calls == [("exited", "sess-1", "r1", None)]


def test_the_task_key_is_resolved_from_the_run_dir(client, flag, manager, runs):
    """The body's `session_id` is an optimisation: a host that could not read
    `meta.json` when it looked sends nothing, and the run dir answers."""
    flag(True)
    run = runs / "r1"
    run.mkdir()
    (run / "meta.json").write_text(json.dumps({"session_id": "sess-from-disk"}))

    r = client.post("/api/tasks/queue/event", headers=HEADERS,
                    json={"kind": "turn_ended", "run_id": "r1"})
    assert r.status_code == 200
    assert manager.calls == [("turn_ended", "sess-from-disk", "r1")]


def test_a_resumed_run_resolves_to_the_session_it_continued(
        client, flag, manager, runs):
    flag(True)
    run = runs / "r1"
    run.mkdir()
    (run / "meta.json").write_text(json.dumps({"resumed_from": "sess-old"}))

    client.post("/api/tasks/queue/event", headers=HEADERS,
                json={"kind": "card_raised", "run_id": "r1"})
    assert manager.calls == [("card_raised", "sess-old", "r1")]


def test_an_unresolvable_run_is_a_400_not_a_guess(client, flag, manager, runs):
    flag(True)
    r = client.post("/api/tasks/queue/event", headers=HEADERS,
                    json={"kind": "turn_ended", "run_id": "nothing-here"})
    assert r.status_code == 400
    assert manager.calls == []


def test_a_traversing_run_id_never_becomes_a_path(client, flag, manager, runs):
    flag(True)
    r = client.post("/api/tasks/queue/event", headers=HEADERS,
                    json={"kind": "turn_ended", "run_id": "../../etc"})
    assert r.status_code == 400
    assert manager.calls == []


def test_a_manager_that_throws_is_never_a_500(client, flag, manager,
                                              monkeypatch):
    """The caller is a deaf daemon thread; a status code it cannot read is
    worth less than a logged traceback, and a 500 in the server's own access
    log for every turn would bury the real ones."""
    flag(True)

    def boom(task_key, run_id=""):
        raise RuntimeError("index is wedged")

    monkeypatch.setattr(manager, "turn_ended", boom)
    r = client.post("/api/tasks/queue/event", headers=HEADERS,
                    json={"kind": "turn_ended", "run_id": "r1",
                          "session_id": "sess-1"})
    assert r.status_code == 200
    assert r.json() == {"ok": False}


# ------------------------------------------------------- the startup wiring


def _queue_startup(app):
    """`create_app`'s queue handler, off the list it publishes for exactly this
    (`app.state.startup_handlers`)."""
    for handler in app.state.startup_handlers:
        if handler.__name__ == "_startup_queue_manager":
            return handler
    raise AssertionError("create_app registered no queue startup handler")


def _run(handler):
    import asyncio

    asyncio.run(handler())


@pytest.fixture(autouse=True)
def restore_factory():
    """The wiring tests below register the REAL factory on purpose. Put back
    whatever was there so a later test cannot build a live manager by accident."""
    before = queue_manager._factory
    yield
    queue_manager.set_factory(before)


def test_startup_registers_the_factory_explicitly(tmp_path, flag, monkeypatch):
    """THE WIRING IS A RULE, NOT AN IMPORT ORDER. The factory is registered when
    the tasks router is imported and `schedule._qm()` re-registers it as a
    fallback, but the scheduler, the transport, the doors and `/api/run`'s gate
    all reach the manager — so which of them happened to be first must not
    decide whether this process has one. Startup says it once, out loud."""
    flag(False)
    app = create_app(start_dir=str(tmp_path))
    queue_manager.set_factory(None)
    _run(_queue_startup(app))
    assert queue_manager._factory is not None
    # Flag off: registered and NOT built. Building reconciles, and reconciling
    # spawns — a switched-off queue must start nothing.
    assert queue_manager.peek() is None


def test_startup_resumes_the_lines_when_the_flag_is_on(tmp_path, flag, monkeypatch):
    """A restart with a half-run line resumes it now, not on whatever event
    happens to arrive first. On a daemon thread, because resuming can spawn
    several Claude processes and must not hold up the first page paint."""
    flag(True)
    app = create_app(start_dir=str(tmp_path))
    reconciled = []

    class _Manager:
        def reconcile(self):
            reconciled.append(True)

    queue_manager.reset_for_tests(_Manager())
    try:
        _run(_queue_startup(app))
        app.state.queue_resume.join(timeout=5)
        assert reconciled == [True]
    finally:
        queue_manager.reset_for_tests(None)


def test_a_queue_that_cannot_resume_does_not_take_the_server_down(tmp_path, flag,
                                                                  monkeypatch):
    flag(True)
    app = create_app(start_dir=str(tmp_path))

    def boom():
        raise RuntimeError("index unreadable")

    monkeypatch.setattr(queue_manager, "get", boom)
    _run(_queue_startup(app))
    app.state.queue_resume.join(timeout=5)
