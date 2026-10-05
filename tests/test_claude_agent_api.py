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
                        lambda params, envelope, body=None:
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
    assert pool.budget("poll") == 20
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
    assert stub["file_owner"] == [
        {"ok": True, "result": {"run_id": "r-1", "session_id": "s-1"}}]


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
