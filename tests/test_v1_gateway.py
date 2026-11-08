"""The local model gateway (docs/LOCAL_MODEL_GATEWAY.md): `server/routers/v1.py`
with the supervisor FAKED — a scripted `generate_text`, a `ready_worker`
switch, a fake `await_loaded` — so no model ever loads. The app under test is
a bare FastAPI with the `/v1` router for speed; one test builds the real
`create_app` to pin the X-Fused contrast against `/api/ai`.
"""
import importlib.util
import json
import os
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fused_render import lan
from fused_render.ai import catalog, gateway_keys, registry, supervisor
from fused_render.server import common
from fused_render.server.routers import v1

QWEN = "Qwen3.5-4B-Q4_K_M.gguf"
NOTOOLS = "someone/plain-chat-model"

TRAITS = {
    QWEN: {"context": 32768, "tool_family": "qwen", "tools": True, "max_output": 4096},
    NOTOOLS: {"context": 8192, "tool_family": "hermes", "tools": False, "max_output": 1024},
}


class FakeSupervisor:
    """What the router reaches for on `supervisor`: a resident switch, a
    scripted reply, a load that can succeed, fail or dawdle."""

    def __init__(self):
        self.resident = {QWEN}
        self.script = [{"type": "chunk", "text": "Hello"}, {"type": "chunk", "text": " world"},
                       {"type": "done", "ok": True, "tokens": 2, "input_tokens": 7,
                        "finish_reason": "stop"}]
        self.requests = []
        self.load_calls = []
        self.load_delay = 0.0
        self.load_error = None
        self.gate = None  # a threading.Event the generator waits on, if set

    def ready_worker(self, capability, model=None):
        assert capability == registry.TEXT_GENERATION
        return object() if model in self.resident else None

    def generate_text(self, model, body, *, requester_id="", requester_label=""):
        self.requests.append((model, body, requester_id))
        if self.gate is not None:
            self.gate.wait(10)
        for event in self.script:
            yield event

    def await_loaded(self, model, capability=registry.TEXT_GENERATION, *, timeout=600.0,
                     n_ctx=None, **_):
        self.load_calls.append({"model": model, "timeout": timeout, "n_ctx": n_ctx})
        if self.load_delay:
            time.sleep(self.load_delay)
        if self.load_error:
            raise supervisor.SupervisorError(self.load_error)
        self.resident.add(model)
        return object()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    gateway_keys._reset_for_tests()
    fake = FakeSupervisor()
    monkeypatch.setattr(supervisor, "ready_worker", fake.ready_worker)
    monkeypatch.setattr(supervisor, "generate_text", fake.generate_text)
    monkeypatch.setattr(supervisor, "await_loaded", fake.await_loaded)
    monkeypatch.setattr(catalog, "agent_traits",
                        lambda model: dict(TRAITS.get(model, TRAITS[NOTOOLS])))
    monkeypatch.setattr(catalog, "local_agent_models", lambda: [
        {"id": QWEN, "label": "Qwen3.5 4B (Q4_K_M)", "context": 32768,
         "max_output": 4096, "tools": True},
        {"id": NOTOOLS, "label": "Plain", "context": 8192, "max_output": 1024,
         "tools": False},
    ])
    monkeypatch.setattr(registry, "for_capability",
                        lambda cap: type("R", (), {"code": "llamacpp-text"})())
    v1._locks.clear()
    yield fake
    gateway_keys._reset_for_tests()
    v1._locks.clear()


@pytest.fixture
def client(fake):
    app = FastAPI()
    app.include_router(v1.router)
    return TestClient(app)


def auth(key=None):
    return {"Authorization": f"Bearer {key or gateway_keys.machine_key()}"}


def chat(model=QWEN, **over):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    body.update(over)
    return body


def sse_events(text):
    """`[("comment", str) | ("data", obj|"[DONE]")]` from an SSE body."""
    out = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        if block.startswith(":"):
            out.append(("comment", block))
        elif block.startswith("data: "):
            payload = block[len("data: "):]
            out.append(("data", "[DONE]" if payload == "[DONE]" else json.loads(payload)))
    return out


# -------------------------------------------------------------------- auth

def test_401_without_a_key_in_the_openai_envelope(client):
    for method, path in (("get", "/v1/models"), ("post", "/v1/chat/completions"),
                         ("post", "/v1/messages")):
        r = client.post(path, json=chat()) if method == "post" else client.get(path)
        assert r.status_code == 401, path
        assert r.json()["error"]["type"] == "invalid_api_key"
        assert r.json()["error"]["code"] == "invalid_api_key"
        assert r.json()["error"]["message"]


def test_wrong_key_and_wrong_scheme_are_401(client):
    assert client.get("/v1/models", headers={"Authorization": "Bearer nope"}).status_code == 401
    key = gateway_keys.machine_key()
    assert client.get("/v1/models", headers={"Authorization": f"Basic {key}"}).status_code == 401


def test_machine_key_and_session_key_both_open_the_door(client):
    assert client.get("/v1/models", headers=auth()).status_code == 200
    session = gateway_keys.mint_session_key()
    assert client.get("/v1/models", headers=auth(session)).status_code == 200
    gateway_keys.release_key(session)
    assert client.get("/v1/models", headers=auth(session)).status_code == 401


def test_messages_is_reserved_with_501(client):
    r = client.post("/v1/messages", json={}, headers=auth())
    assert r.status_code == 501
    assert r.json()["error"]["type"] == "not_implemented"


# ----------------------------------------------------------------- /models

def test_models_shape(client, fake):
    r = client.get("/v1/models", headers=auth())
    body = r.json()
    assert body["object"] == "list"
    ids = [row["id"] for row in body["data"]]
    assert ids == [QWEN, NOTOOLS]
    row = body["data"][0]
    assert row["object"] == "model" and row["owned_by"] == "fused"
    assert "created" in row
    assert row["fused"] == {"runner": "llamacpp-text", "loaded": True, "context": 32768,
                            "tools": True, "label": "Qwen3.5 4B (Q4_K_M)", "max_output": 4096}
    assert body["data"][1]["fused"]["loaded"] is False
    assert body["data"][1]["fused"]["tools"] is False


# ------------------------------------------------------------- completions

def test_non_streaming_happy_path(client, fake):
    r = client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == QWEN
    choice = body["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "Hello world"}
    assert choice["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    model, request, requester = fake.requests[0]
    assert model == QWEN and requester == "gateway"
    assert request["messages"] == [{"role": "user", "content": "hi"}]
    assert request["max_tokens"] == 4096  # the row's max_output
    assert "tools" not in request and "stop" not in request
    assert fake.load_calls == []  # resident: no load


def test_streaming_happy_path(client, fake):
    with client.stream("POST", "/v1/chat/completions", json=chat(stream=True),
                       headers=auth()) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = sse_events(r.read().decode())
    datas = [v for k, v in events if k == "data"]
    assert datas[-1] == "[DONE]"
    chunks = datas[:-1]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert [c["choices"][0]["delta"].get("content") for c in chunks[1:-1]] == ["Hello", " world"]
    final = chunks[-1]
    assert final["choices"][0]["delta"] == {}
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    assert len({c["id"] for c in chunks}) == 1


def test_length_finish_reason_maps_through(client, fake):
    fake.script[-1]["finish_reason"] = "length"
    r = client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert r.json()["choices"][0]["finish_reason"] == "length"


def test_translation_of_roles_parts_and_tool_turns(client, fake):
    body = chat(messages=[
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "42"},
    ], tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        stop=["<END>"], max_tokens=99999, temperature=0.1)
    r = client.post("/v1/chat/completions", json=body, headers=auth())
    assert r.status_code == 200, r.text
    _, request, _ = fake.requests[0]
    assert request["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "ab"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "content": "42", "tool_call_id": "call_1"},
    ]
    assert request["tools"] == body["tools"]
    assert request["stop"] == ["<END>"]
    assert request["max_tokens"] == 32768  # capped at the row's context
    assert request["temperature"] == 0.1


def test_image_parts_are_400(client):
    body = chat(messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}])
    r = client.post("/v1/chat/completions", json=body, headers=auth())
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize("bad", [
    {"model": ""}, {"messages": []}, {"messages": [{"role": "robot", "content": "x"}]},
    {"max_tokens": -1}, {"stop": 5}, {"tools": "f"},
    {"messages": [{"role": "tool", "content": "x"}]},
])
def test_malformed_bodies_are_400(client, bad):
    r = client.post("/v1/chat/completions", json=chat(**bad), headers=auth())
    assert r.status_code == 400, r.text
    assert r.json()["error"]["type"] == "invalid_request_error"


# -------------------------------------------------------------------- tools

QWEN_TOOL_REPLY = [
    {"type": "chunk", "text": "Checking. <tool_"},
    {"type": "chunk", "text": 'call>{"name": "get_weather", "arguments": {"city": "Paris"}}</tool_call>'},
    {"type": "done", "ok": True, "tokens": 20, "input_tokens": 50, "finish_reason": "stop"},
]
TOOLS = [{"type": "function", "function": {
    "name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]


def test_qwen_tool_call_non_streaming(client, fake):
    fake.script = QWEN_TOOL_REPLY
    r = client.post("/v1/chat/completions", json=chat(tools=TOOLS), headers=auth())
    assert r.status_code == 200, r.text
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Checking. "
    call, = choice["message"]["tool_calls"]
    assert call["type"] == "function" and call["id"].startswith("call_") and call["index"] == 0
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}


def test_qwen_tool_call_streaming(client, fake):
    fake.script = QWEN_TOOL_REPLY
    with client.stream("POST", "/v1/chat/completions", json=chat(tools=TOOLS, stream=True),
                       headers=auth()) as r:
        events = sse_events(r.read().decode())
    chunks = [v for k, v in events if k == "data" and v != "[DONE]"]
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert deltas[0] == {"role": "assistant", "content": ""}
    assert deltas[1] == {"content": "Checking. "}
    tool_delta = deltas[2]["tool_calls"][0]
    assert tool_delta["index"] == 0 and tool_delta["type"] == "function"
    assert tool_delta["function"]["name"] == "get_weather"
    assert json.loads(tool_delta["function"]["arguments"]) == {"city": "Paris"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"]["total_tokens"] == 70
    assert events[-1] == ("data", "[DONE]")


def test_tools_on_a_model_without_a_tools_branch_is_400(client, fake):
    fake.resident.add(NOTOOLS)
    r = client.post("/v1/chat/completions", json=chat(NOTOOLS, tools=TOOLS), headers=auth())
    assert r.status_code == 400
    assert "tools" in r.json()["error"]["message"]
    assert fake.requests == []
    # `tool_choice: "none"` drops the tools and goes through.
    r = client.post("/v1/chat/completions",
                    json=chat(NOTOOLS, tools=TOOLS, tool_choice="none"), headers=auth())
    assert r.status_code == 200
    assert "tools" not in fake.requests[0][1]


# ---------------------------------------------------------------- cold load

def test_cold_load_non_streaming_blocks_then_answers(client, fake, monkeypatch):
    monkeypatch.setenv(v1.LOAD_TIMEOUT_ENV, "33")
    fake.resident.clear()
    r = client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert r.status_code == 200, r.text
    assert fake.load_calls == [{"model": QWEN, "timeout": 33.0, "n_ctx": 32768}]
    assert r.json()["choices"][0]["message"]["content"] == "Hello world"


def test_cold_load_streaming_emits_loading_comments(client, fake, monkeypatch):
    monkeypatch.setenv(v1.LOAD_PING_ENV, "0.05")
    fake.resident.clear()
    fake.load_delay = 0.4
    with client.stream("POST", "/v1/chat/completions", json=chat(stream=True),
                       headers=auth()) as r:
        assert r.status_code == 200
        events = sse_events(r.read().decode())
    comments = [v for k, v in events if k == "comment"]
    assert comments and all(c == ": loading" for c in comments)
    assert len(comments) >= 3
    # The comments precede every data frame, and the reply still arrives.
    first_data = next(i for i, (k, _) in enumerate(events) if k == "data")
    assert all(k == "comment" for k, _ in events[:first_data])
    assert events[-1] == ("data", "[DONE]")
    assert "".join(v["choices"][0]["delta"].get("content", "")
                   for k, v in events if k == "data" and v != "[DONE]") == "Hello world"


def test_failed_load_is_503_model_loading_failed(client, fake):
    fake.resident.clear()
    fake.load_error = "the model failed to load: out of memory"
    r = client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "model_loading_failed"
    assert "out of memory" in r.json()["error"]["message"]
    assert fake.requests == []


def test_failed_load_on_a_stream_is_reported_in_band(client, fake):
    fake.resident.clear()
    fake.load_error = "nope"
    with client.stream("POST", "/v1/chat/completions", json=chat(stream=True),
                       headers=auth()) as r:
        events = sse_events(r.read().decode())
    assert events[-2][1]["error"]["type"] == "model_loading_failed"
    assert events[-1] == ("data", "[DONE]")


def test_slot_conflict_is_409(client, fake, monkeypatch):
    fake.resident.clear()

    def conflict(model, **_):
        raise supervisor.ModelSlotConflict(registry.TEXT_GENERATION, [QWEN, "other"], "other")

    monkeypatch.setattr(supervisor, "await_loaded", conflict)
    r = client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert r.status_code == 409
    assert r.json()["error"]["type"] == "model_slot_conflict"
    assert r.json()["error"]["resident"] == "other"


def test_worker_failure_is_502(client, fake):
    fake.script = [{"type": "chunk", "text": "par"},
                   {"type": "done", "ok": False, "error": "the model process did not answer"}]
    r = client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "ai_unavailable"


# -------------------------------------------------------------------- queue

def test_queued_request_past_the_timeout_is_429(client, fake, monkeypatch):
    monkeypatch.setenv(v1.QUEUE_TIMEOUT_ENV, "0.2")
    fake.gate = threading.Event()
    started = threading.Event()
    results = {}

    def first():
        with client.stream("POST", "/v1/chat/completions", json=chat(stream=True),
                           headers=auth()) as r:
            started.set()
            results["first"] = (r.status_code, r.read())

    worker = threading.Thread(target=first)
    worker.start()
    assert started.wait(5)
    time.sleep(0.05)  # let the first request take the model's lock
    r = client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "rate_limit_exceeded"
    fake.gate.set()
    worker.join(5)
    assert results["first"][0] == 200
    assert b"[DONE]" in results["first"][1]
    # The lock is released once the stream ends: a third request goes through.
    r = client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert r.status_code == 200


def test_different_models_do_not_queue_behind_each_other(client, fake, monkeypatch):
    monkeypatch.setenv(v1.QUEUE_TIMEOUT_ENV, "0.2")
    fake.resident.add(NOTOOLS)
    fake.gate = threading.Event()
    started = threading.Event()

    def first():
        with client.stream("POST", "/v1/chat/completions", json=chat(stream=True),
                           headers=auth()) as r:
            started.set()
            r.read()

    worker = threading.Thread(target=first)
    worker.start()
    assert started.wait(5)
    time.sleep(0.05)
    fake.gate.set()  # NOTOOLS's generator must not wait on QWEN's gate
    r = client.post("/v1/chat/completions", json=chat(NOTOOLS), headers=auth())
    assert r.status_code == 200
    worker.join(5)


# ------------------------------------------------- the guards around the door

def test_v1_is_exempt_from_x_fused_while_api_ai_still_requires_it(fake, tmp_path):
    from fused_render.server.app import create_app

    app = create_app(str(tmp_path), lean=True)
    client = TestClient(app)
    # No X-Fused anywhere: the gateway answers on its bearer key alone...
    r = client.post("/v1/chat/completions", json=chat(), headers=auth())
    assert r.status_code == 200, r.text
    # ...and /api/ai still refuses a blind cross-origin POST (D3).
    r = client.post("/api/ai", json={"prompt": "hi"})
    assert r.status_code == 403
    assert "X-Fused" in r.text


def test_csrf_exemption_is_stated_in_common():
    assert common.csrf_exempt("/v1")
    assert common.csrf_exempt("/v1/chat/completions")
    assert not common.csrf_exempt("/v10/x")
    assert not common.csrf_exempt("/api/ai")
    assert common._require_fused(None, "/v1/models") is None
    assert common._require_fused(None, "/api/ai") is not None
    assert common._require_fused(None) is not None


def test_lan_never_allow_lists_v1():
    assert not any(path.startswith("/v1") for path in lan._EXACT)
    assert not any(prefix.startswith("/v1") for prefix in lan._PREFIXES)


# ------------------------------------------- the worker contract (unit level)

BASE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "fused_render", "ai", "runners", "worker_base.py")


@pytest.fixture
def worker_base():
    spec = importlib.util.spec_from_file_location("worker_base_v1_under_test", BASE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_generate_accepts_tools_and_stop(worker_base):
    opts = worker_base.generation_options({
        "messages": [], "tools": TOOLS, "stop": ["<END>", ""], "max_tokens": "12"})
    assert opts == {"tools": TOOLS, "stop": ["<END>"], "max_tokens": 12}
    assert worker_base.generation_options({}) == {"tools": None, "stop": [], "max_tokens": None}
    assert worker_base.generation_options({"stop": "x", "tools": []})["stop"] == ["x"]
    assert worker_base.generation_options({"tools": []})["tools"] is None


@pytest.mark.parametrize("body", [
    {"tools": "f"}, {"tools": [1]}, {"stop": 5}, {"stop": [1]},
    {"max_tokens": "lots"}, {"max_tokens": 0}, [],
])
def test_worker_generate_refuses_malformed_tools_and_stop(worker_base, body):
    with pytest.raises(worker_base.BadRequest):
        worker_base.generation_options(body)


def test_worker_finish_reason(worker_base):
    fr = worker_base.finish_reason
    assert fr(cancelled=True) == "cancel"
    assert fr(count=10, max_tokens=10) == "length"
    assert fr(count=3, max_tokens=10) == "stop"
    assert fr(count=10, max_tokens=10, hit_stop=True) == "stop"
    assert fr(count=10, max_tokens=None) == "stop"


def test_llama_text_reads_n_ctx_from_env(monkeypatch):
    path = os.path.join(os.path.dirname(BASE_PATH), "llama_text.py")
    source = open(path, encoding="utf-8").read()
    assert 'os.environ.get("FUSED_AI_N_CTX"' in source
    assert "_DEFAULT_N_CTX = 8192" in source
    assert "tools=tools" in source and "stop=options[\"stop\"]" in source
    mlx = open(os.path.join(os.path.dirname(BASE_PATH), "mlx_text", "worker.py"),
               encoding="utf-8").read()
    assert 'os.environ.get("FUSED_AI_N_CTX"' in mlx
    assert 'kwargs["tools"] = tools' in mlx
    assert '"finish_reason": finish' in mlx


def test_supervisor_passes_n_ctx_to_the_worker_env(monkeypatch):
    monkeypatch.setattr(supervisor, "_mirror_ok", lambda model: None)
    monkeypatch.setattr(supervisor, "_await_hardware_cache", lambda: None)
    monkeypatch.setattr(supervisor.fit, "available_budget_bytes", lambda hardware=None: None)
    env = supervisor._child_env("tok", "m", registry.TEXT_GENERATION, n_ctx=32768)
    assert env["FUSED_AI_N_CTX"] == "32768"
    monkeypatch.setenv("FUSED_AI_N_CTX", "stale")
    env = supervisor._child_env("tok", "m", registry.TEXT_GENERATION)
    assert "FUSED_AI_N_CTX" not in env
    assert supervisor.GATEWAY_REQUESTER_ID == "gateway"
    assert supervisor.Worker(model="m", capability="c", runner_code="r").n_ctx is None


def test_reasoning_streams_as_reasoning_content_and_never_as_the_answer(client, fake):
    """A thinking model's deliberation (split by the worker onto the
    `reasoning` channel, docs/LOCAL_MODEL_GATEWAY.md §3) reaches the client as
    `reasoning_content`, not `content` — OpenCode showed the deliberation as
    the reply otherwise — and a tool call mentioned while thinking is not a
    call; the one after `</think>` is."""
    fake.script = [
        {"type": "chunk", "text": "I could call <tool_call>{\"name\":\"nope\"}</tool_call> ",
         "channel": "reasoning"},
        {"type": "chunk", "text": "but first", "channel": "reasoning"},
        {"type": "chunk", "text": "Sure. <tool_call>"},
        {"type": "chunk", "text": '{"name": "get_weather", "arguments": {"city": "Paris"}}</tool_call>'},
        {"type": "done", "ok": True, "tokens": 20, "input_tokens": 50, "finish_reason": "stop"},
    ]
    body = {"model": QWEN, "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]}
    r = client.post("/v1/chat/completions", json=body, headers=auth())
    assert r.status_code == 200, r.text
    msg = r.json()["choices"][0]["message"]
    assert msg["reasoning_content"].startswith("I could call <tool_call>")
    assert msg["content"] == "Sure. "
    assert [c["function"]["name"] for c in msg["tool_calls"]] == ["get_weather"]
    r = client.post("/v1/chat/completions", json=dict(body, stream=True), headers=auth())
    deltas = [json.loads(l[6:])["choices"][0]["delta"] for l in r.text.splitlines()
              if l.startswith("data: ") and l != "data: [DONE]"]
    assert "".join(d.get("reasoning_content", "") for d in deltas).endswith("but first")
    assert "".join(d.get("content", "") for d in deltas) == "Sure. "
    assert sum(len(d.get("tool_calls", [])) for d in deltas) == 1
