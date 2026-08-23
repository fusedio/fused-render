# A Python client for the fused AI API

Today `fused.ai` exists only as JavaScript (`fused_render/static/runtime.js`) —
a page-only surface. Every Python process that wants local inference
re-implements the model layer from scratch. This document describes what was
actually built to close that gap (SPEC PY-19, DECISIONS D449-D451) — an
earlier draft of this file sketched a wider surface than shipped; the
"Superseded" section at the bottom names what was cut and why.

## What this replaces

A Python process wanting `fused.ai`'s local-model story today hand-rolls: a
hardcoded model-id ladder per tier, a home-grown resident-process manager, a
scratch-dir + `status.json` + `.ready`-marker progress protocol for a detached
worker, its own cancellation plumbing, and its own "is this on disk"
snapshot check. All of that already exists behind an HTTP API this server
runs:

| Reinvented | The API that already does it |
|---|---|
| hardcoded model-id ladder | `GET /api/ai/catalog` (`ai.models.catalog()`) |
| resident-model load/warm-up | `POST /api/ai/runtime/load` (`ai.models.load()`) |
| a scratch-dir progress protocol for a detached worker | the job registry (`jobs.py`) — `jobId` from the POST, `GET /api/jobs` to watch |
| cancellation plumbing | `POST /api/ai/cancel` (`ai.cancel()`) |
| "is it on disk" snapshot check | `GET /api/ai/catalog`'s `downloaded` flag |

## Shape of the client

**One stdlib-only module: `fused_render/templates/shared/fused_ai.py`.**
Stdlib-only is not taste, it is SPEC PY-15: a `.py` data file runs in a
subprocess with `PYTHONPATH` stripped and must not import `fused_render` —
the same reason `templates/shared/appenv.py` is stdlib-only. `fused_ai.py`
imports its sibling `appenv.py` for the shell-home-dir contract rather than
re-deriving it, so the two travel together whenever either is vendored.

### Surface — a 1:1 mirror of the JS, minus the promises

```python
from fused_ai import ai

ai.text("summarise this", model="mlx-community/Qwen3-4B-Instruct-2507-4bit")
for chunk in ai.stream("write a haiku"):      # NDJSON {"type":"chunk","text"} frames
    print(chunk, end="")

ai.transcribe(path="/tmp/rec.webm", model="mlx-community/whisper-turbo")
ai.image(prompt="a cat", width=512)
ai.embed(texts=["a", "b"])
ai.models.list() / .catalog() / .load(id) / .download(id) / .unload(id)
ai.cancel("text-generation")
```

Same names, same option names (pinned against the server's own
`_IMAGE_OPTIONS`/`_TRANSCRIBE_OPTIONS` in `routers/ai_runtime.py` by
`tests/test_fused_ai_client.py`, the same drift guard `runtime.js` already
has). `AiError` carries `type`, `message`, and the HTTP `status`, mapped off
the house `{ok, error:{type, message}}` wire shape (or the plainer
`{"error": "..."}` a job-backed endpoint's own validation returns).

**`ServerNotRunning` is a separate exception from `AiError`.** A caller's
sensible response to "the app isn't running" (start it, or give up) differs
from its response to "the call failed" (retry, report), so folding the two
into one type would make every catch site re-derive which is which from the
message text.

**Blocking by default (`wait=True`):** `transcribe`, `image`, and
`models.load`/`download` POST to a job-backed endpoint that answers
immediately with a `jobId` — a Python caller has a thread to spend, so the
job protocol is folded into the call: POST, then poll `GET /api/jobs` for
that id until it reaches a terminal state, then return the settled reply.
`wait=False` returns the immediate `{"jobId": ..., "path": ..., ...}` reply
for a caller that wants to drive its own loop; `on_progress=` receives each
polled job row; `timeout=` bounds the wait. A job whose reporter died shows
up as `stalled` (`jobs.py::is_stalled`) well before its ten-minute registry
eviction, and the wait loop raises immediately on that flag rather than
polling out the full window — a hang this module can detect and refuses to
reproduce. This is the one place the Python surface deliberately differs
from the JS one: `await` becomes `return`.

**The request envelope is closed (D413) and this module does not
re-validate it.** An option the server does not recognise is a 400 from the
server itself; a third copy of the whitelist here (beside `runtime.js`'s and
the server's own `_IMAGE_OPTIONS`/`_TRANSCRIBE_OPTIONS`) is exactly how the
three would drift, so the module's own `_IMAGE_WIRE_KEYS`/
`_TRANSCRIBE_WIRE_KEYS` exist only so a test can pin them against the
server's constants — never to gate a caller's call.

**Relative paths are resolved locally, not sent with a `base`.**
`/api/ai/transcribe` and `/api/ai/image` accept a relative `path`/`image`
only alongside an absolute `base` naming the calling *page* (RH-1) — a
concept this module has none of. Instead `transcribe()`/`image()` call
`os.path.abspath()` on a relative path before sending it: `_child.py`
already chdirs a running `.py` to its own directory, so cwd already means
"beside this file", matching the page-relative semantics without a `base`
parameter.

Every POST carries `X-Fused: 1` — not authentication (D3), it forces a CORS
preflight a foreign page cannot pass — set by the module itself so no caller
has to know the rule exists.

## Two gaps this closes

### 1. Origin discovery for a process the server did not spawn

Server children inherit `FUSED_RENDER_ORIGIN`
(`server/app.py::set_server_origin_env`), so a `.py` data file is already
covered. A user-launched process inherits nothing and cannot compute the
port — the desktop launcher auto-picks a free one, so `_branch.branch_port()`
is only right for a bare `fused-render` run.

**Fix:** the server writes its origin to `server.json` under the shell home
dir (`shell.storage.home_dir()` — already branch-resolved) at bind time,
alongside `set_server_origin_env`/`export_app_env`, and removes it on
ordinary shutdown. Writing is best-effort and non-fatal — this runs before
the socket bind, and a failed write must not read as a failed server start.
A stale file (a crashed server) is expected; staleness is the *client's*
problem, detected with a connect probe rather than a heartbeat from the
server side.

`fused_ai.py`'s `resolve_origin()` order:
`FUSED_RENDER_ORIGIN` (via `appenv.origin()`) -> `server.json`,
connect-probed -> `ServerNotRunning`. No `branch_port()` fallback: this
module cannot re-derive branch-ref resolution without importing
`fused_render`, and a guessed-but-wrong port is worse than a clear "nothing
is running".

### 2. `X-Fused: 1` on every mutating POST

Not authentication (D3) — it forces a CORS preflight a foreign page cannot
pass. The client sets it on every POST so no caller has to know.

Neither gap is in the AI layer itself: the endpoints as they stand already
answer everything this client needs.

## Reaching the module — three ways, not two

1. **A user `.py` under a running server** gets `import fused_ai` for free.
   Both execution engines seed the shared-template dir onto the user
   module's own `sys.path` — `_child.py`'s worker (the built-in executor) and
   `engine.py`'s generated wrapper (the fused engine) — in lockstep, changed
   together in the same commit. The dir is **appended**, never inserted at
   `[0]`, so a user's own same-named module still wins. Each engine derives
   the path from its own file's location (`fused_render/templates/shared`,
   a fixed sibling), never from an env var: `_child.py` runs as a standalone
   script and `engine.py` bakes a path into generated source, so neither can
   assume the server's environment reached the child.
2. **An external process** (the desktop-launched-app case `server.json`
   exists for) reads `server.json`, `sys.path.insert`s its `shared` value,
   then `import fused_ai`. A three-line bootstrap:

   ```python
   import json, os, sys
   with open(os.path.expanduser("~/.fused-render/server.json")) as f:
       sys.path.insert(0, json.load(f)["shared"])
   import fused_ai
   ```

   (A real bootstrap should also honor `FUSED_RENDER_HOME`/branch nesting the
   way `appenv.home_dir()` does, and connect-probe before trusting the file —
   see `resolve_origin()`'s own logic, which this bootstrap is deliberately
   simpler than.)
3. **An app wanting zero coupling to fused-render at all** vendors
   `fused_ai.py` and `appenv.py` together — the two files are the whole
   dependency, and neither is useful alone.

## Superseded from the earlier draft — not built, and why

- **No `fused_render/client/` package and no pip-install re-export.**
  Rejected: it would drag ~150MB of fastapi/duckdb/pyarrow into a caller
  that wants a 300-line HTTP shim. There is no `pip install
  fused-render[client]` form of this — the three paths above are the whole
  distribution story.
- **Exported pages are not a fourth consumer.** `export.py` does not copy
  `templates/shared/` into an exported bundle, and an exported page has no
  server behind it to call in the first place — there is nothing for
  `fused_ai.py` to reach even if it were vendored in.
