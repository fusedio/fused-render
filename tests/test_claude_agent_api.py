"""`POST /api/claude/agent` and its two siblings (server/routers/claude_agent.py).

The chat's backend used to be reached through `/api/run`, one interpreter per
call. It is now one in-process module behind its own router, and the router's
contract is what the React chat's AgentError mapping
(frontend/src/apps/claude/protocol/agent.ts) is written against:

  * 403 without `X-Fused` (D3), before anything else;
  * 400 `{"error": "<string>"}` for an action off the allowlist or params the
    handler cannot bind;
  * 200 with the handler's dict verbatim — a handler's own `{"error": ...}`
    included, because that is an answer, not a failure;
  * 500 `{"error": {type, message, traceback}}` when the handler raised, the
    shape `/api/run`'s error object had;
  * 504 `{"error": {type: "Timeout", message}}` when the per-action budget
    expired;
  * `start`/`send` run the folder-busy gate first, and a refusal is a 200
    `{"error": <refusal>}` with the handler never called.

The handler is stubbed (the agent module is swapped for a namespace with a
`main`), so these pin the router, not agent.py.
"""
import inspect
import time
import types

import pytest
from fastapi.testclient import TestClient

from fused_render import calls, claude_agent
from fused_render.claude_agent import gate, pool
from fused_render.server import create_app
from fused_render.server.routers import claude_agent as router

FUSED = {"X-Fused": "1"}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture
def stub(monkeypatch):
    """Swap the agent module for one whose `main` the test defines, and record
    what the gate's two halves were asked."""
    seen = {"main": [], "file_owner": []}

    def install(main):
        def recording_main(*a, **kw):
            seen["main"].append(kw)
            return main(*a, **kw)
        # bind_params reads the signature, so the wrapper must carry main's.
        recording_main.__signature__ = inspect.signature(main)
        monkeypatch.setattr(claude_agent, "agent_module",
                            lambda: types.SimpleNamespace(main=recording_main))

    monkeypatch.setattr(gate, "_folder_busy", lambda params, body=None: "")
    monkeypatch.setattr(gate, "_file_owner",
                        lambda params, envelope, body=None, late=False:
                        seen["file_owner"].append(envelope))
    seen["install"] = install
    return seen


def _post(client, body, headers=FUSED):
    return client.post("/api/claude/agent", json=body, headers=headers)


# ------------------------------------------------------------------ the door

def test_a_post_without_x_fused_is_refused(client, stub):
    stub["install"](lambda action="", file="": {"ok": 1})
    r = _post(client, {"action": "poll"}, headers={})
    assert r.status_code == 403
    assert stub["main"] == []


@pytest.mark.parametrize("body", [{"action": "rm_rf"}, {}, {"action": 7}])
def test_an_action_off_the_allowlist_is_a_400_before_anything_runs(client, stub, body):
    stub["install"](lambda action="", file="": {"ok": 1})
    r = _post(client, body)
    assert r.status_code == 400
    assert isinstance(r.json()["error"], str)
    assert stub["main"] == []


def test_params_the_handler_cannot_bind_are_a_400(client, stub):
    def main(action, file, required_thing):
        return {}
    stub["install"](main)
    r = _post(client, {"action": "poll", "file": "/x"})
    assert r.status_code == 400
    assert "required_thing" in r.json()["error"]
    assert stub["main"] == []


def test_the_allowlist_names_only_handlers_the_real_module_has():
    agent = claude_agent.agent_module()
    for action, handler in router.ACTIONS.items():
        assert callable(getattr(agent, handler, None)), (action, handler)


# ---------------------------------------------------------------- the answer

def test_a_handlers_answer_is_the_body_verbatim(client, stub):
    stub["install"](lambda action="", file="": {"done": True, "text": "héllo", "n": [1, 2]})
    r = _post(client, {"action": "poll", "file": "/x"})
    assert r.status_code == 200
    assert r.json() == {"done": True, "text": "héllo", "n": [1, 2]}
    assert stub["main"] == [{"action": "poll", "file": "/x"}]


def test_a_handlers_own_error_is_a_200_passthrough(client, stub):
    stub["install"](lambda action="", file="": {"error": "no such run"})
    r = _post(client, {"action": "poll"})
    assert r.status_code == 200
    assert r.json() == {"error": "no such run"}


def test_keys_the_handler_does_not_take_are_dropped_not_refused(client, stub):
    """The page always sends `_file` and sometimes `queue_claim` — the gate's
    inputs, not the handler's. A strict bind would 400 every send."""
    stub["install"](lambda action="", file="": {"ok": True})
    r = _post(client, {"action": "poll", "file": "/x", "_file": "/x", "queue_claim": "t"})
    assert r.status_code == 200
    assert stub["main"] == [{"action": "poll", "file": "/x"}]


def test_a_raising_handler_is_a_500_with_the_run_error_shape(client, stub):
    def main(action="", file=""):
        raise ValueError("bad target")
    stub["install"](main)
    r = _post(client, {"action": "poll"})
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["type"] == "ValueError"
    assert err["message"] == "bad target"
    assert "Traceback" in err["traceback"] and "bad target" in err["traceback"]


def test_a_handler_past_its_budget_is_a_504(client, stub, monkeypatch):
    def main(action="", file=""):
        time.sleep(0.5)
        return {"late": True}
    stub["install"](main)
    monkeypatch.setattr(pool, "budget", lambda action: 0.05)
    r = _post(client, {"action": "poll"})
    assert r.status_code == 504
    err = r.json()["error"]
    assert err["type"] == "Timeout"
    assert "poll" in err["message"]
    assert "traceback" not in err


def test_the_per_action_budgets_match_the_contract():
    # 60, not 20: the poll that sees a turn end runs `_commit_turn` (three git
    # calls on 30 s timeouts + a 3 s self-HTTP), and a shorter budget 504s
    # exactly the poll carrying the finished reply.
    assert pool.budget("poll") == 60
    assert "poll" not in pool.BUDGETS_S
    for action in ("live_run", "live_host", "defaults", "terminal_command",
                   "sessions", "history"):
        assert pool.budget(action) == 30, action
    assert pool.budget("cancel") == 15
    assert pool.budget("start") == pool.budget("decide") == 60


# ------------------------------------------------------------------ the gate

@pytest.mark.parametrize("action", ["start", "send"])
def test_a_busy_folder_refuses_a_start_or_send_as_a_200(client, stub, monkeypatch, action):
    stub["install"](lambda action="", file="": {"run_id": "r-1"})
    asked = []

    def busy(params, body=None):
        asked.append(params["action"])
        return "Another task is running in this folder."
    monkeypatch.setattr(gate, "_folder_busy", busy)
    r = _post(client, {"action": action, "file": "/w/a.html", "_file": "/w/a.html"})
    assert r.status_code == 200
    assert r.json() == {"error": "Another task is running in this folder."}
    assert asked == [action]
    assert stub["main"] == [], "a refused start must not reach the handler"
    assert stub["file_owner"] == []


def test_the_owner_is_filed_after_the_handler_even_when_it_raised(client, stub):
    """A start that raised must still reach `_file_owner`, which is what gives
    back a placeholder claim the gate minted for it."""
    def main(action="", file=""):
        raise RuntimeError("spawn failed")
    stub["install"](main)
    r = _post(client, {"action": "start", "file": "/w/a.html"})
    assert r.status_code == 500
    assert len(stub["file_owner"]) == 1
    assert stub["file_owner"][0]["ok"] is False


def test_the_owner_sees_the_handlers_result_on_success(client, stub):
    stub["install"](lambda action="", file="": {"run_id": "r-1", "session_id": "s-1"})
    r = _post(client, {"action": "start", "file": "/w/a.html"})
    assert r.status_code == 200
    [env] = stub["file_owner"]
    assert env["ok"] is True
    assert env["result"] == {"run_id": "r-1", "session_id": "s-1"}


def test_the_envelope_carries_the_handlers_own_duration(client, stub, monkeypatch):
    """`duration_ms` is what `calls.enrich_run` files as `run_ms` — the number
    `/api/run`'s child used to report for every chat call."""
    def main(action="", file=""):
        time.sleep(0.05)
        return {"ok": 1}
    stub["install"](main)
    seen = []
    monkeypatch.setattr(calls, "enrich_run",
                        lambda call, **kw: seen.append(kw["result"]))
    r = _post(client, {"action": "poll"})
    assert r.status_code == 200
    assert r.json() == {"ok": 1}, "duration_ms never reaches the body"
    [env] = seen
    assert env["duration_ms"] >= 40, env

    def boom(action="", file=""):
        raise RuntimeError("x")
    stub["install"](boom)
    seen.clear()
    r = _post(client, {"action": "poll"})
    assert r.status_code == 500
    assert isinstance(seen[0]["duration_ms"], float)


def test_a_start_past_its_budget_frees_the_folder_now_and_files_late(
        client, stub, monkeypatch):
    """The 504 goes out while `_start` is still running. The `admit:`
    placeholder is dropped at once, so the user's retry is not refused by a
    folder held for a run that may never exist; the late `_start` still files
    its owner when it lands."""
    import threading

    release = threading.Event()
    landed = threading.Event()

    def main(action="", file=""):
        release.wait(10)
        return {"run_id": "r-late", "session_id": "s-late"}
    stub["install"](main)
    monkeypatch.setattr(pool, "budget", lambda action: 0.05)
    monkeypatch.setattr(gate, "_queue_target", lambda params: "/w")
    dropped = []
    monkeypatch.setattr(gate, "_drop_placeholder",
                        lambda key, body=None: dropped.append(key))
    filed = stub["file_owner"]
    lates = []
    monkeypatch.setattr(gate, "_file_owner",
                        lambda params, envelope, body=None, late=False:
                        (filed.append(envelope), lates.append(late), landed.set()))
    r = _post(client, {"action": "start", "file": "/w/a.html"})
    assert r.status_code == 504
    assert r.json()["error"]["type"] == "Timeout", "it ran; it was just slow"
    assert dropped == ["/w"], "the placeholder goes back with the 504"
    assert filed == [], "the run has not landed yet"
    release.set()
    assert landed.wait(10)
    assert filed[0]["ok"] is True
    assert filed[0]["result"]["run_id"] == "r-late"
    assert lates == [True], "a late start is filed as late"


class _FakeQueue:
    """The two queue_manager calls `_file_owner` makes, on one folder."""

    def __init__(self, owner=None):
        self.owners = {"/w": owner} if owner else {}
        self.started_calls = []

    def owner(self, key):
        return self.owners.get(key)

    def consume_claim(self, key, token):
        pass

    def started(self, key, task, run_id, session_id):
        self.started_calls.append(task)
        self.owners[key] = {"task": task, "run_id": run_id}


@pytest.mark.parametrize("current, files", [
    # The retry's real run owns it.
    ({"task": "s-B", "run_id": "r-B"}, False),
    # A same-session retry: task S, exactly the name A carries — but run B.
    ({"task": "s-A", "run_id": "r-B"}, False),
    # Somebody else's live placeholder, with a run already filed on it.
    ({"task": "admit:other", "run_id": "r-B", "claims": ["other-tok"]}, False),
    # A's OWN placeholder: its admit token is still among the claims.
    ({"task": "admit:mine", "run_id": "r-X", "claims": ["mine-tok"]}, True),
    # Somebody else's placeholder with no run filed yet and none of A's
    # tokens (A's own admit placeholder was dropped when its start timed out).
    ({"task": "admit:tok"}, False),
    # ANOTHER session's tokenless `claim_took` reservation: task s-B, no run.
    ({"task": "s-B", "run_id": ""}, False),
    # A's OWN session reservation: task s-A, no run yet.
    ({"task": "s-A", "run_id": ""}, True),
    # This very run.
    ({"task": "s-A", "run_id": "r-A"}, True),
    # Free.
    (None, True),
])
def test_a_late_start_never_takes_the_folder_from_a_real_owner(
        monkeypatch, current, files):
    """Run A's start 504'd, the user retried, run B was filed as the folder's
    owner — then A lands. Filing A would hand the folder back to the run the
    user gave up on, so it is skipped (A is still in the runs dir and listed in
    Tasks). It files only into a free folder, its own placeholder, this run,
    or a run-less reservation under A's OWN name — never by name once a run
    is filed (a same-session retry carries A's session id), and never into
    another session's run-less reservation (Bugbot, PR #1409)."""
    from fused_render import queue_manager

    fake = _FakeQueue(dict(current) if current else None)
    monkeypatch.setattr(queue_manager, "get", lambda: fake)
    monkeypatch.setattr(gate, "_queue_target", lambda params: "/w")
    envelope = {"ok": True, "result": {"run_id": "r-A", "session_id": "s-A"}}
    gate._file_owner({"action": "start", "file": "/w/a.html"}, envelope,
                     {"_queue_admit_token": "mine-tok"}, late=True)
    if files:
        assert fake.started_calls == ["s-A"]
        assert fake.owners["/w"]["run_id"] == "r-A"
    else:
        assert fake.started_calls == []
        assert fake.owners["/w"] == current, "the retry's owner is untouched"
    # Not late (the ordinary path): unchanged — `started` decides.
    fake2 = _FakeQueue({"task": "s-B", "run_id": "r-B"})
    monkeypatch.setattr(queue_manager, "get", lambda: fake2)
    gate._file_owner({"action": "start", "file": "/w/a.html"}, envelope, {})
    assert fake2.started_calls == ["s-A"]


def test_a_send_past_its_budget_drops_nothing(client, stub, monkeypatch):
    import threading

    release = threading.Event()
    stub["install"](lambda action="", file="": release.wait(10) and {"sent": True})
    monkeypatch.setattr(pool, "budget", lambda action: 0.05)
    monkeypatch.setattr(gate, "_queue_target", lambda params: "/w")
    dropped = []
    monkeypatch.setattr(gate, "_drop_placeholder",
                        lambda key, body=None: dropped.append(key))
    r = _post(client, {"action": "send", "file": "/w/a.html"})
    release.set()
    assert r.status_code == 504
    assert dropped == []


@pytest.fixture
def one_busy_worker(monkeypatch):
    """Every action routed to a ONE-worker pool whose only worker is held, so
    a submitted job waits its whole budget in the queue and never runs."""
    from concurrent.futures import ThreadPoolExecutor
    import threading

    busy = ThreadPoolExecutor(max_workers=1)
    hold = threading.Event()
    busy.submit(hold.wait, 10)
    monkeypatch.setattr(pool, "pool_for", lambda action: busy)
    monkeypatch.setattr(pool, "budget", lambda action: 0.1)
    yield busy
    hold.set()
    busy.shutdown(wait=True)


@pytest.mark.parametrize("action", ["start", "send", "poll"])
def test_a_call_that_never_got_a_worker_is_a_notrun_504(
        client, stub, monkeypatch, one_busy_worker, action):
    """The budget ran out in the QUEUE: the handler ran zero times and never
    will. That is "NotRun", not "Timeout" — and nothing waits for a late
    outcome: the gate files it at once as the failure it is (a start's
    placeholder dropped, a send's claim restored), exactly once."""
    stub["install"](lambda action="", file="": {"run_id": "never"})
    lates = []
    monkeypatch.setattr(gate, "_file_owner",
                        lambda params, envelope, body=None, late=False:
                        (stub["file_owner"].append(envelope), lates.append(late)))
    r = _post(client, {"action": action, "file": "/w/a.html"})
    assert r.status_code == 504
    err = r.json()["error"]
    assert err["type"] == "NotRun", err
    assert "never ran" in err["message"] and action in err["message"]
    one_busy_worker.shutdown(wait=False, cancel_futures=True)
    time.sleep(0.2)
    assert stub["main"] == [], "the handler never ran"
    assert len(stub["file_owner"]) == 1, "filed once, now, with no late filing"
    assert stub["file_owner"][0]["error"]["type"] == "NotRun"
    assert lates == [False]


def test_a_start_the_queue_admitted_gives_its_placeholder_back_on_504(
        client, monkeypatch, tmp_path, one_busy_worker):
    """Flag ON. `/api/tasks/queue/admit` minted the placeholder for a brand-new
    chat and the gate spent its `queue_claim` on it — leaving no token on the
    body for `_drop_placeholder`. A 504 must still free the folder, or it holds
    until the placeholder's TTL against the user's own retry."""
    from fused_render import project_queue, queue_manager, tasks_store

    monkeypatch.setattr(tasks_store, "STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    folder = str(tmp_path / "proj")
    monkeypatch.setattr(project_queue, "enabled", lambda: True)
    monkeypatch.setattr(project_queue, "queue_key", lambda target: folder)
    m = queue_manager.QueueManager(
        spawn=lambda f, t: None, deliver=lambda a: None,
        running=lambda r: False, blocked=lambda r: False,
        pending_due=lambda: [])
    m.reconcile()
    queue_manager.reset_for_tests(m)
    ok, _took, token = m.claim_for_send(folder, "admit:" + "a" * 32)
    assert ok and token
    assert m.owner(folder)["task"].startswith("admit:")

    def main(action="", file="", queue_claim=""):
        return {"run_id": "never"}
    main.__signature__ = inspect.signature(main)
    monkeypatch.setattr(claude_agent, "agent_module",
                        lambda: types.SimpleNamespace(main=main))
    r = _post(client, {"action": "start", "file": folder + "/a.html",
                       "queue_claim": token})
    assert r.status_code == 504
    assert m.owner(folder) is None, "the admitted placeholder was released"


def test_a_spent_token_releases_only_its_own_placeholder(tmp_path, monkeypatch):
    from fused_render import queue_manager, tasks_store

    monkeypatch.setattr(tasks_store, "STATE_DIR", str(tmp_path))
    m = queue_manager.QueueManager(
        spawn=lambda f, t: None, deliver=lambda a: None,
        running=lambda r: False, blocked=lambda r: False,
        pending_due=lambda: [])
    m.reconcile()
    _, _, token = m.claim_for_send("/f", "admit:" + "b" * 32)
    assert m.release_spent_placeholder("/f", token) is False, "not spent yet"
    assert m.consume_claim("/f", token)
    assert m.release_spent_placeholder("/f", "someone-else") is False
    assert m.owner("/f") is not None
    assert m.release_spent_placeholder("/f", token) is True
    assert m.owner("/f") is None


# ---------------------------------------------------- the call log attribution

def test_the_chats_page_id_is_first_party():
    assert calls.is_first_party(claude_agent.CLAUDE_PAGE_ID)


def test_a_chat_call_is_recorded_as_first_party(client, monkeypatch):
    """The call log keeps showing chat calls: a record exists because the page
    sent `X-Fused-Page`, it is filed first-party, and it is attributed to
    agent.py."""
    seen = {}
    real = calls.enrich_run

    def spy(call, **kw):
        seen["call"] = call
        return real(call, **kw)
    monkeypatch.setattr(calls, "enabled", lambda: True)
    monkeypatch.setattr(calls, "enrich_run", spy)
    r = _post(client, {"action": "shots_dir"},
              headers={**FUSED, "X-Fused-Page": claude_agent.CLAUDE_PAGE_ID})
    assert r.status_code == 200
    call = seen["call"]
    assert call["first_party"] is True and call["outcome"] == "ok"
    assert call["entrypoint"].endswith("agent.py")


# ------------------------------------------------- the real module, end to end

def test_the_real_agent_answers_its_read_actions(client, tmp_path):
    """No stub: the in-process module behind the router answers the actions
    the chat fires on open, and the gate's extra keys (`_file`,
    `queue_claim`) are dropped before `main`."""
    f = str(tmp_path / "x.py")
    r = _post(client, {"action": "live_run", "file": f, "_file": f, "queue_claim": ""})
    assert r.status_code == 200, r.text
    assert r.json() == {"run_id": ""}
    r = _post(client, {"action": "sessions"})
    assert r.status_code == 200 and "error" in r.json()  # no file: a refusal, not a crash
    r = _post(client, {"action": "poll", "run_id": "../bad"})
    assert r.status_code == 200, r.text
    r = _post(client, {"action": "shots_dir"})
    assert r.status_code == 200 and r.json()["dir"]


# ------------------------------------------------------------ the two siblings

def test_app_entry_resolves_a_folders_page(client, tmp_path):
    # Tagged: the shared rule only calls a page an app entry when it says so.
    (tmp_path / "index.html").write_text('<html><head><meta name="fused-app" /></head></html>')
    r = client.post("/api/claude/app-entry", json={"dir": str(tmp_path)}, headers=FUSED)
    assert r.status_code == 200
    assert r.json() == {"entry": str(tmp_path / "index.html")}
    r = client.post("/api/claude/app-entry", json={"dir": str(tmp_path / "nope")},
                    headers=FUSED)
    assert r.json() == {"entry": None}


@pytest.mark.parametrize("body", [{}, {"dir": ""}, {"dir": "   "}])
def test_app_entry_for_no_folder_is_none_not_the_servers_cwd(client, tmp_path,
                                                            monkeypatch, body):
    """abspath("") is the cwd. A cwd that happens to hold a tagged page must
    not become the pane of a chat that named no folder."""
    (tmp_path / "index.html").write_text('<html><head><meta name="fused-app" /></head></html>')
    monkeypatch.chdir(tmp_path)
    r = client.post("/api/claude/app-entry", json=body, headers=FUSED)
    assert r.status_code == 200
    assert r.json() == {"entry": None}


@pytest.mark.parametrize("path", ["/api/claude/app-entry", "/api/claude/artifacts"])
def test_the_siblings_carry_the_same_guard(client, path):
    assert client.post(path, json={}).status_code == 403


def test_artifacts_answers_in_process(client, monkeypatch):
    from fused_render.claude_agent import artifacts

    seen = {}

    def fake_main(action="list", file="", session_id="", cwd=""):
        seen.update(action=action, file=file, session_id=session_id)
        return {"artifacts": []}
    monkeypatch.setattr(artifacts, "main", fake_main)
    r = client.post("/api/claude/artifacts",
                    json={"action": "live", "file": "/x", "session_id": "s"},
                    headers=FUSED)
    assert r.status_code == 200
    assert r.json() == {"artifacts": []}
    assert seen == {"action": "live", "file": "/x", "session_id": "s"}
