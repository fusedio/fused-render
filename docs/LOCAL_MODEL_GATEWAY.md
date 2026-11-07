# Local model gateway — build contract

An engine-agnostic HTTP edge that exposes every local text runner
(`mlx-text`, `llamacpp-text`, `llamacpp-text-vulkan`, and any future one) to
agent CLIs and other OpenAI-compatible clients. Design settled 2026-11-07.

## 0. Why a layer, not an engine

The agent-facing contract (OpenAI chat completions, tool calls, SSE, model
listing, auth, cold-load semantics) belongs ABOVE the supervisor so that a
new engine implements only the small internal worker contract and is
reachable by OpenCode the day it lands. A llama-server side process was
rejected: it solves nothing for MLX (the engine on Apple Silicon), adds a
second binary per platform to vendor and audit, and puts the contract inside
one engine.

## 1. Surface

Mounted on the main server (`server/routers/v1.py`), prefix `/v1`:

| route | behaviour |
|---|---|
| `GET /v1/models` | every installed text model the resolved runner can load, `{object:"list", data:[{id, object:"model", owned_by:"fused", created, fused:{runner, loaded, context, tools}}]}` |
| `POST /v1/chat/completions` | OpenAI shape in (`model, messages, tools?, tool_choice?, stream?, max_tokens?, temperature?, top_p?, stop?`), OpenAI shape out (JSON or SSE `chat.completion.chunk` deltas, `[DONE]`); `finish_reason` `stop | length | tool_calls`; `usage {prompt_tokens, completion_tokens, total_tokens}` on the final chunk |
| `POST /v1/messages` | Anthropic messages shape, same semantics (phase 2; the router reserves the path and answers 501 until then) |

Auth: `Authorization: Bearer <key>`. The route is EXEMPT from the `X-Fused`
CSRF check (`common._require_fused`) because a bearer key is a stronger
guard than the header: keys are minted by `ai/gateway_keys.py` — one
per-machine key persisted at `<home>/ai/v1_key` (0600) for tools the user
configures by hand, plus per-session keys the OpenCode host mints and
registers for the life of the child. Unknown key → 401 with the OpenAI error
envelope. LAN (`lan.py`) does not allow-list `/v1`.

## 2. Semantics

- **Model id** = the catalog id (`Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf`,
  `mlx-community/Qwen3.5-4B-OptiQ-4bit`), exactly what `/api/ai` routes on.
- **Cold load**: a request for a model that is not resident calls
  `supervisor.load(model)` and WAITS on the job (bounded by
  `FUSED_RENDER_V1_LOAD_TIMEOUT_S`, default 600), sending SSE comment lines
  (`: loading\n\n`) every 5 s on a streaming request so the client's socket
  stays open; a non-streaming request simply blocks. A failed load → 503
  `{"error": {"type": "model_loading_failed"}}`.
- **Eviction**: the one-resident-model rule holds; a request for model B
  while A is resident evicts A exactly as a Playground load would, through
  the supervisor's own slot-conflict logic with requester `gateway`.
- **Queue**: the gateway serializes requests per worker in front of the
  worker's `GENERATE_LOCK` (an `asyncio.Lock` per model id) so parallel agent
  side requests wait instead of racing; a queued request past
  `FUSED_RENDER_V1_QUEUE_TIMEOUT_S` (120) → 429.
- **Tools**: `tools` are passed to the worker verbatim; the worker renders
  them through the model's own chat template. The reply text is run through
  `ai/toolcalls.py`, a family-keyed parser (`qwen`, `hermes`, `llama3`,
  `gemma`, `lfm`) that is streaming-safe: text outside a call marker is
  emitted as `delta.content`, a complete call is emitted as
  `delta.tool_calls[{index, id, type:"function", function{name, arguments}}]`
  and the final `finish_reason` becomes `tool_calls`. The family comes from
  `catalog.tool_family(model)` (new field, defaulting by name prefix). A model
  whose template has no `tools` branch is listed with `fused.tools: false`
  and a request naming `tools` for it → 400.
- **Context**: `/v1/models` reports `fused.context` from the catalog's new
  `context` field (default 32768 for agent-capable rows, 8192 otherwise);
  the load passes it as `n_ctx`; `fit` uses the same number for the KV term.
- **Translation**: OpenAI `messages` (system/user/assistant/tool roles,
  string or parts content, `tool_calls` on assistant turns) → the worker's
  `messages` list, with tool-result turns kept as `{"role": "tool",
  "tool_call_id", "content"}` for the template. Images → 400 (phase 2).

## 3. Internal worker contract (what each engine implements)

`POST /generate` gains optional `tools` (list, OpenAI function schema),
`stop` (list of strings) and the load gains `n_ctx` (int). The NDJSON `done`
row gains `finish_reason: "stop" | "length" | "cancel"`. Nothing else: no
OpenAI shapes, no SSE, no tool-call parsing inside an engine.

- `runners/llama_text.py`: `_N_CTX` becomes the load option (env
  `FUSED_AI_N_CTX` from the supervisor, default 8192); the Jinja render gets
  `tools=…`; `stop` is honoured; `finish_reason` reported.
- `runners/mlx_text/worker.py`: same three.
- `ai/supervisor.py`: `load(..., n_ctx=)` passes it to the worker env;
  `generate_text` passes `tools`/`stop` through.
- `ai/fit.py`: `KV_CACHE_CONTEXT_TOKENS` becomes the per-request context.

## 4. Files

- new: `server/routers/v1.py`, `ai/toolcalls.py`, `ai/gateway_keys.py`,
  `tests/test_v1_gateway.py`, `tests/test_toolcalls.py`
- edited: `server/app.py` (mount), `server/common.py` (CSRF exemption for
  `/v1`), `ai/supervisor.py`, `ai/runners/worker_base.py`, `ai/runners/llama_text.py`,
  `ai/runners/mlx_text/worker.py`, `ai/fit.py`, `ai/catalog.py` (context,
  tool_family), `lan.py` (explicitly NOT allow-listed; a test pins it).
