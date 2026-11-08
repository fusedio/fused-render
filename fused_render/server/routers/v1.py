"""The local model gateway (docs/LOCAL_MODEL_GATEWAY.md): `/v1`.

An engine-agnostic OpenAI-compatible edge over every local text runner the
supervisor can load — `mlx-text`, `llamacpp-text`, `llamacpp-text-vulkan`,
whatever lands next — so an agent CLI pointed at
`http://127.0.0.1:<port>/v1` with a key from `ai/gateway_keys.py` can list,
cold-load and talk to the same models `/api/ai` routes on, tool calls
included. Nothing here knows an engine: the worker contract is the small
NDJSON one (`{"type":"chunk"}` … `{"type":"done"}`), and the OpenAI shapes,
the SSE framing, the cold-load wait, the per-model queue and the tool-call
parsing (`ai/toolcalls.py`) all live in this one file ABOVE the supervisor.

Routes (prefix `/v1`):

* `GET  /models` — every installed text model of the resolved runner.
* `POST /chat/completions` — JSON or SSE (`stream: true`).
* `POST /messages` — reserved for the Anthropic shape (phase 2); 501.

Auth is `Authorization: Bearer <key>` (`gateway_keys.is_valid`): an unknown
key is a 401 in the OpenAI error envelope. These routes are EXEMPT from the
D3 `X-Fused` guard (`common.CSRF_EXEMPT_PREFIXES`) because the key is the
stronger guard, and `lan.py` never allow-lists them.

Failure envelopes are OpenAI's: `{"error": {"message", "type", "code"}}`.
A STREAMING request that fails after its headers went out (a cold load
that errors while `: loading` comments were keeping the socket open, a
worker that dies mid-reply) reports the same envelope as one in-band
`data:` line before `[DONE]`, since the 200 is already on the wire.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from fused_render.ai import gateway_keys, toolcalls

router = APIRouter(prefix="/v1")

#: How long a cold load may take before the request gives up on it (§2).
LOAD_TIMEOUT_ENV = "FUSED_RENDER_V1_LOAD_TIMEOUT_S"
DEFAULT_LOAD_TIMEOUT_S = 600.0
#: How long a request waits behind another on the same model (§2).
QUEUE_TIMEOUT_ENV = "FUSED_RENDER_V1_QUEUE_TIMEOUT_S"
DEFAULT_QUEUE_TIMEOUT_S = 120.0
#: The SSE comment cadence while a streaming request waits on a load.
LOAD_PING_ENV = "FUSED_RENDER_V1_LOAD_PING_S"
DEFAULT_LOAD_PING_S = 5.0

_ROLES = frozenset({"system", "user", "assistant", "tool", "developer"})

#: One `asyncio.Lock` per model id: the gateway's own queue in front of the
#: worker's `GENERATE_LOCK`, so parallel agent requests wait here (and can
#: be told 429) instead of racing the worker.
_locks: dict[str, asyncio.Lock] = {}


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _error(message: str, type_: str, status: int, code: str | None = None,
           **extra) -> JSONResponse:
    body = {"message": message, "type": type_, "code": code or type_}
    body.update(extra)
    return JSONResponse({"error": body}, status_code=status)


def _error_payload(message: str, type_: str, code: str | None = None) -> dict:
    return {"error": {"message": message, "type": type_, "code": code or type_}}


def _authorized(request: Request) -> JSONResponse | None:
    header = request.headers.get("authorization") or ""
    scheme, _, key = header.partition(" ")
    if scheme.lower() != "bearer" or not gateway_keys.is_valid(key.strip()):
        return _error(
            "Incorrect API key provided. Use the key from <home>/ai/v1_key or "
            "one minted for this session.", "invalid_api_key", 401)
    return None


def _lock_for(model: str) -> asyncio.Lock:
    lock = _locks.get(model)
    if lock is None:
        lock = _locks[model] = asyncio.Lock()
    return lock


# ---------------------------------------------------------------- /models

def _model_rows() -> list[dict]:
    from fused_render.ai import catalog, registry, supervisor

    runner = registry.for_capability(registry.TEXT_GENERATION)
    code = runner.code if runner is not None else None
    rows = []
    for entry in catalog.local_agent_models():
        loaded = supervisor.ready_worker(registry.TEXT_GENERATION, entry["id"]) is not None
        rows.append({
            "id": entry["id"], "object": "model", "owned_by": "fused",
            "created": 0,
            "fused": {"runner": code, "loaded": loaded,
                      "context": entry["context"], "tools": entry["tools"],
                      "label": entry.get("label"),
                      "max_output": entry.get("max_output")},
        })
    return rows


@router.get("/models")
def v1_models(request: Request):
    refused = _authorized(request)
    if refused:
        return refused
    return {"object": "list", "data": _model_rows()}


@router.post("/messages")
def v1_messages(request: Request):
    refused = _authorized(request)
    if refused:
        return refused
    return _error("The Anthropic messages shape is not implemented yet; use "
                  "/v1/chat/completions.", "not_implemented", 501)


# ------------------------------------------------------------- translation

class _BadRequest(ValueError):
    pass


def _flatten_content(content) -> str:
    """OpenAI `content` (string or parts list) -> plain text. Image parts are
    refused (phase 2, §2 "Translation")."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if not isinstance(part, dict):
                raise _BadRequest("message content parts must be objects")
            kind = part.get("type")
            if kind == "text":
                texts.append(str(part.get("text", "")))
            elif kind in ("image_url", "image", "input_image", "input_audio"):
                raise _BadRequest(f"'{kind}' content parts are not supported by the "
                                  f"local gateway yet; send text")
            else:
                raise _BadRequest(f"unknown content part type {kind!r}")
        return "".join(texts)
    raise _BadRequest("message content must be a string or a list of parts")


def translate_messages(messages) -> list[dict]:
    """OpenAI `messages` -> the worker's `messages` (§2 "Translation").
    `developer` folds into `system`; an assistant turn keeps its `tool_calls`
    (the template renders them); a `tool` turn keeps `tool_call_id`."""
    if not isinstance(messages, list) or not messages:
        raise _BadRequest("'messages' must be a non-empty list")
    out = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise _BadRequest(f"messages[{i}] must be an object")
        role = msg.get("role")
        if role not in _ROLES:
            raise _BadRequest(f"messages[{i}].role must be one of system, user, "
                              f"assistant, tool")
        if role == "developer":
            role = "system"
        row: dict = {"role": role, "content": _flatten_content(msg.get("content"))}
        if role == "assistant" and msg.get("tool_calls"):
            calls = msg["tool_calls"]
            if not isinstance(calls, list):
                raise _BadRequest(f"messages[{i}].tool_calls must be a list")
            row["tool_calls"] = calls
        if role == "tool":
            if not msg.get("tool_call_id"):
                raise _BadRequest(f"messages[{i}] (role tool) needs 'tool_call_id'")
            row["tool_call_id"] = msg["tool_call_id"]
            if msg.get("name"):
                row["name"] = msg["name"]
        out.append(row)
    return out


def _validate_tools(tools) -> list[dict]:
    if not isinstance(tools, list):
        raise _BadRequest("'tools' must be a list")
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict) or tool.get("type", "function") != "function":
            raise _BadRequest(f"tools[{i}] must be a {{type: 'function', function: {{…}}}} object")
        fn = tool.get("function")
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"]:
            raise _BadRequest(f"tools[{i}].function.name is required")
    return tools


def _build_request(body: dict) -> tuple[str, dict, dict]:
    """`(model, worker request, traits)` from an OpenAI chat body, or raise
    `_BadRequest`."""
    from fused_render.ai import catalog

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise _BadRequest("'model' is required")
    model = model.strip()
    traits = catalog.agent_traits(model)
    messages = translate_messages(body.get("messages"))

    tools = body.get("tools")
    tool_choice = body.get("tool_choice")
    if tools is not None:
        tools = _validate_tools(tools)
        if tool_choice == "none" or not tools:
            tools = None
    if tools and not traits["tools"]:
        raise _BadRequest(f"{model} has no tools branch in its chat template; "
                          f"'tools' cannot be used with it")

    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_completion_tokens")
    if max_tokens is None:
        max_tokens = traits["max_output"]
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        raise _BadRequest("'max_tokens' must be an integer") from None
    if max_tokens <= 0:
        raise _BadRequest("'max_tokens' must be positive")
    max_tokens = min(max_tokens, traits["context"])

    stop = body.get("stop")
    if isinstance(stop, str):
        stop = [stop]
    elif stop is not None and (not isinstance(stop, list)
                               or not all(isinstance(s, str) for s in stop)):
        raise _BadRequest("'stop' must be a string or a list of strings")

    def _num(name):
        value = body.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _BadRequest(f"'{name}' must be a number")
        return float(value)

    request = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": _num("temperature"),
        "top_p": _num("top_p"),
        "stop": stop or None,
        "tools": tools,
    }
    return model, {k: v for k, v in request.items() if v is not None}, traits


# -------------------------------------------------------------- generation

def _pump(model: str, request: dict, queue: asyncio.Queue, loop) -> None:
    """Run the supervisor's blocking NDJSON generator on a thread, handing
    each event to the event loop. `None` closes the queue."""
    from fused_render.ai import supervisor

    try:
        for event in supervisor.generate_text(
                model, request, requester_id=supervisor.GATEWAY_REQUESTER_ID,
                requester_label="Local model gateway"):
            loop.call_soon_threadsafe(queue.put_nowait, event)
    except Exception as e:  # noqa: BLE001 - every failure must reach the client
        loop.call_soon_threadsafe(
            queue.put_nowait, {"type": "done", "ok": False, "error": str(e)})
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, None)


class _Reply:
    """Accumulates a worker's NDJSON into OpenAI pieces as it streams."""

    def __init__(self, model: str, family: str):
        self.id = "chatcmpl-" + uuid.uuid4().hex
        self.created = int(time.time())
        self.model = model
        self.parser = toolcalls.StreamParser(family)
        self.text_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.tool_calls: list[dict] = []
        self.done: dict | None = None
        self.error: str | None = None

    def _call_row(self, call: dict) -> dict:
        args = call.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args if args is not None else {})
        row = {"index": len(self.tool_calls), "id": "call_" + uuid.uuid4().hex[:24],
               "type": "function",
               "function": {"name": call.get("name", ""), "arguments": args}}
        self.tool_calls.append(row)
        return row

    def take(self, event: dict) -> list[tuple]:
        """One worker event -> OpenAI events: `("text", str)` and
        `("call", row)`. Records `done`/`error` as they arrive."""
        kind = event.get("type")
        if kind == "chunk":
            text = str(event.get("text") or "")
            if event.get("channel") == "reasoning":
                # Split by the worker (docs/LOCAL_MODEL_GATEWAY.md §3), never
                # here: deliberation is served as `reasoning_content` and
                # never reaches the tool parser — a model thinking aloud
                # about a call has not made it. The answer does.
                self.reasoning_parts.append(text)
                return [("reasoning", text)] if text else []
            return self._events(self.parser.feed(text))
        if kind == "done":
            self.done = event
            if event.get("ok") is False:
                self.error = str(event.get("error") or "the model failed")
            return self._events(self.parser.finish())
        return []

    def _events(self, parsed: list[tuple]) -> list[tuple]:
        out = []
        for kind, value in parsed:
            if kind == "text":
                self.text_parts.append(value)
                out.append(("text", value))
            else:
                out.append(("call", self._call_row(value)))
        return out

    @property
    def finish_reason(self) -> str:
        if self.tool_calls:
            return "tool_calls"
        if (self.done or {}).get("finish_reason") == "length":
            return "length"
        return "stop"

    @property
    def usage(self) -> dict:
        done = self.done or {}
        prompt = done.get("input_tokens") or 0
        completion = done.get("tokens") or 0
        return {"prompt_tokens": int(prompt), "completion_tokens": int(completion),
                "total_tokens": int(prompt) + int(completion)}

    def chunk(self, delta: dict, finish_reason=None, usage=None) -> dict:
        body = {"id": self.id, "object": "chat.completion.chunk",
                "created": self.created, "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
        if usage is not None:
            body["usage"] = usage
        return body

    def completion(self) -> dict:
        message: dict = {"role": "assistant", "content": "".join(self.text_parts)}
        if self.reasoning_parts:
            # DeepSeek's/vLLM's spelling, which the AI SDK's openai-compatible
            # provider (OpenCode's) reads as a reasoning part.
            message["reasoning_content"] = "".join(self.reasoning_parts)
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
            if not message["content"]:
                message["content"] = None
        return {"id": self.id, "object": "chat.completion", "created": self.created,
                "model": self.model,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": self.finish_reason}],
                "usage": self.usage}


def _sse(payload) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _ensure_loaded(model: str, n_ctx: int, *, ping=None):
    """Cold load (§2): resident already -> returns at once; otherwise start
    the load and wait on it, calling `ping()` (an awaitable factory, for the
    streaming path's comments) every `LOAD_PING` seconds. Raises what
    `supervisor.await_loaded` raises."""
    from fused_render.ai import registry, supervisor

    if supervisor.ready_worker(registry.TEXT_GENERATION, model) is not None:
        return
    timeout = _env_float(LOAD_TIMEOUT_ENV, DEFAULT_LOAD_TIMEOUT_S)
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        None, lambda: supervisor.await_loaded(model, timeout=timeout, n_ctx=n_ctx))
    if ping is None:
        await future
        return
    interval = _env_float(LOAD_PING_ENV, DEFAULT_LOAD_PING_S)
    while True:
        done, _ = await asyncio.wait({future}, timeout=interval)
        if done:
            future.result()
            return
        await ping()


def _load_failure(exc: Exception):
    """(status, envelope) for a cold load that did not end in a ready worker."""
    from fused_render.ai import supervisor

    if isinstance(exc, supervisor.ModelSlotConflict):
        wire = exc.to_wire()
        return 409, {"error": {"message": str(exc), "type": "model_slot_conflict",
                               "code": "model_slot_conflict", **wire}}
    return 503, _error_payload(str(exc), "model_loading_failed")


@router.post("/chat/completions")
async def v1_chat_completions(request: Request):
    refused = _authorized(request)
    if refused:
        return refused
    try:
        body = await request.json()
    except ValueError:
        return _error("request body must be JSON", "invalid_request_error", 400)
    if not isinstance(body, dict):
        return _error("request body must be a JSON object", "invalid_request_error", 400)
    try:
        model, worker_request, traits = _build_request(body)
    except _BadRequest as e:
        return _error(str(e), "invalid_request_error", 400)
    stream = bool(body.get("stream"))

    lock = _lock_for(model)
    try:
        await asyncio.wait_for(lock.acquire(),
                               _env_float(QUEUE_TIMEOUT_ENV, DEFAULT_QUEUE_TIMEOUT_S))
    except asyncio.TimeoutError:
        return _error(f"{model} is busy with another request; try again",
                      "rate_limit_exceeded", 429)

    reply = _Reply(model, traits["tool_family"])
    if not stream:
        try:
            return await _complete(model, worker_request, traits, reply)
        finally:
            lock.release()

    async def events():
        try:
            async for piece in _stream(model, worker_request, traits, reply):
                yield piece
        finally:
            lock.release()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


async def _generate(model: str, worker_request: dict, reply: _Reply):
    """Yield `("text", str)` / `("call", row)` from the worker, through the
    reply's parser. `reply.error` is set when the worker failed."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    loop.run_in_executor(None, _pump, model, worker_request, queue, loop)
    while True:
        event = await queue.get()
        if event is None:
            break
        for piece in reply.take(event):
            yield piece
    if reply.done is None:
        reply.error = reply.error or "the model process closed the stream early"
        for piece in reply.take({"type": "done", "ok": False, "error": reply.error}):
            yield piece


async def _complete(model, worker_request, traits, reply) -> JSONResponse:
    try:
        await _ensure_loaded(model, traits["context"])
    except Exception as e:  # noqa: BLE001 - mapped below
        status, payload = _load_failure(e)
        return JSONResponse(payload, status_code=status)
    async for _ in _generate(model, worker_request, reply):
        pass
    if reply.error:
        return _error(reply.error, "ai_unavailable", 502)
    return JSONResponse(reply.completion())


async def _stream(model, worker_request, traits, reply):
    pending: list[bytes] = []

    async def ping():
        pending.append(b": loading\n\n")

    # Load first, with comments: each `ping` queues a comment that the loop
    # below drains, because this generator is the only thing that can write
    # to the socket.
    load = asyncio.ensure_future(_ensure_loaded(model, traits["context"], ping=ping))
    interval = _env_float(LOAD_PING_ENV, DEFAULT_LOAD_PING_S)
    while not load.done():
        await asyncio.wait({load}, timeout=min(interval, 0.25))
        while pending:
            yield pending.pop(0)
    try:
        load.result()
    except Exception as e:  # noqa: BLE001 - reported in-band; headers are out
        _, payload = _load_failure(e)
        yield _sse(payload)
        yield b"data: [DONE]\n\n"
        return

    yield _sse(reply.chunk({"role": "assistant", "content": ""}))
    async for kind, value in _generate(model, worker_request, reply):
        if kind == "text":
            yield _sse(reply.chunk({"content": value}))
        elif kind == "reasoning":
            yield _sse(reply.chunk({"reasoning_content": value}))
        else:
            yield _sse(reply.chunk({"tool_calls": [value]}))
    if reply.error:
        yield _sse(_error_payload(reply.error, "ai_unavailable"))
        yield b"data: [DONE]\n\n"
        return
    yield _sse(reply.chunk({}, finish_reason=reply.finish_reason, usage=reply.usage))
    yield b"data: [DONE]\n\n"
