# Warm workers for apps: `/api/engine`

`POST /api/engine` is a warm variant of `/api/run`. It takes the same
`{ py, html, params }` body and returns the same result envelope, but the worker
process that runs the script's `main(**params)` is kept **alive** between calls,
so module-level imports run once and module globals persist. It is zero-config:
an app author names their own `.py` and gets the speedup with no manifest, no
registration, and no daemon to write. `/api/run` is unchanged and stays the
always-fresh default.

Related: `docs/ENGINE_HOST_DESIGN.md` (the template engine host from #736, whose
spawn/health/heal/shutdown primitives this reuses).

## Why

Every `fused.runPython("./x.py", …)` call is a fresh short-lived subprocess that
imports the module, runs `main()`, and exits. Nothing survives between calls, so
an app that talks to a remote service re-pays the whole cost every call — for
`s3-browser`, ~3.5–4.5 s of subprocess/engine bootstrap plus ~2.5 s to
`import botocore` and build the client before the actual API call. If the worker
stays alive, `import botocore` runs once and a warm client is reused, collapsing
each read toward network latency.

The engine host already supervises long-lived workers, but only for built-in
templates, and only if the author writes a full socket-server daemon and the
server is handed an interpreter and a daemon path under a templates root. A
community app author has none of that. `/api/engine` is the zero-config layer for
the common case — "keep my `main()` warm" — on top of the same supervisor.

## Author experience

```js
const s3 = fused.engine("./s3.py");                 // names their own file
const res = await s3.call({ action: "list_objects", bucket, prefix });
```

`s3.py` is unchanged from what `/api/run` would run — same `main(...)`, same JSON
contract. The one thing the author *may* now do is move expensive setup to module
scope so it survives between calls:

```python
import botocore.session          # runs once, not per call
_clients = {}                     # warm cache, persists across calls

def main(action, account_id="", bucket="", prefix="", **kw):
    client = _clients.get(account_id) or _clients.setdefault(account_id, _build(account_id))
    ...
    return {"...": "json"}        # same JSON contract as /api/run
```

## Request / response

`POST /api/engine`, `X-Fused: 1` guarded, body `{ py, html, params }`:

- `py` — absolute, or relative to `html` (resolved exactly as `run.py` does; a
  `..` that climbs out is rejected).
- `html` — the calling page's path, used to resolve a relative `py`.
- `params` — the object splatted into `main(**params)`.

The response is byte-identical in shape to `/api/run`:

```json
{ "ok": true, "result": <json>, "stdout": "...", "resolved_py": "/abs/x.py" }
```

On a script failure `ok` is `false` and `error` carries `{ type, message,
traceback }`; `resolved_py` is always present so the runtime can watch the file
that ran for auto-reload (LR-2). A server-level problem (not a script error)
comes back as an HTTP error with `{ "error": "..." }`:

- `404` — no such Python file.
- `409` — the project's venv is not built yet (see *Not yet implemented*).
- `502` — the warm worker could not be started.

`POST /api/engine/forget` `{ py, html }` drops a worker explicitly and returns
`{ "ok": true }`. It is best-effort: forgetting a worker that was never started
or already retired is a no-op success. Idle-retire covers the common case.

## How it works on the server

`routers/engine.py` resolves the file and interpreter exactly like `run.py`:
`projectenv.project_env_for(resolved)` then `projectenv.interpreter_for(...)` —
the app's own `sys.executable` when the folder declares no environment, else that
folder's venv python. No caller-supplied interpreter and no path allowlist: it
runs the calling app's own resolved `.py`, so it adds no code-execution surface
over `/api/run`. It then calls `engine_host.ensure_app(resolved, interpreter)`
and forwards `params` to the worker's `/call` through `routers/engines._forward`,
reusing that proxy's heal-on-failure and cancel-on-disconnect.

### The worker (`fused_render/engine_worker.py`)

A standard shipped loop — the author writes none of it. It is
`executor._child.py` promoted from run-once to a serve loop: it imports the
target module once and then answers many `POST /call` requests in the same
interpreter. It fits the engine-host child contract (`--status/--cache/--version`
plus a `--module <abs .py>` argument), binds `127.0.0.1:0`, generates a token,
publishes `{port, token, pid, version}` to its status file atomically before
serving, and validates the token on every `/ping` and `/call`. Its `/call`
envelope is byte-for-byte `_child.py`'s, plus `resolved_py`.

### Interpreter validation

`engine_host._validate_interpreter` requires the interpreter to be one of ours —
this app's own `sys.executable`, or a python from the home venv store — and is
shared by the template path and the warm-app path. In both, the *server*
resolves the interpreter (never the caller), so this is an invariant check, not a
trust boundary.

### Bring-up and reuse

`ensure_app` keys a worker by `app_engine_id(resolved_py)` — `app_` plus a
sha256 of the absolute path — so two pages that resolve the same file share one
warm worker (per interpreter; a different interpreter forces a respawn via
`_matches`). Status and log files live under `home_dir()/cache/engine-workers/`,
never beside the user's code. First call spawns; later calls reuse a worker that
is alive and answers `/ping`. `APP_WORKER_VERSION` is bumped when the worker
contract changes, so a worker from an older app version is retired rather than
reused.

### Refresh on edit (mtime)

The worker checks the module's mtime on every call and re-imports when it changed
on disk, so editing the `.py` takes effect the way it does for `/api/run` (which
gets fresh code for free every call).

### Concurrency

Calls run in parallel in the one warm process. Only the brief import /
mtime-check / `main` lookup is serialized; `main()` itself runs outside the lock,
so I/O-bound handlers (an S3 list, an HTTP fetch) overlap while the GIL is
released during that I/O, and they share the one warm client cache. `print()` is
captured per call — not per process — through a contextvar-routed stdout, so
concurrent calls never scramble each other's output.

### Idle-retire, heal, shutdown

A daemon sweeper (started on the first bring-up, waking every 60 s) retires any
warm worker idle for `APP_IDLE_RETIRE_S` (15 min); the first call after
retirement re-warms. A crash or `sys.exit` kills the worker, not the server —
the host restarts it and the next call re-warms (heal-on-failure). Every warm
worker is an engine-host child, so the app's `_shutdown_engines` hook kills them
all at shutdown via `stop_all()`.

### The `fused.engine` bridge (`static/runtime.js`)

`fused.engine(py, opts?)` returns `{ call(params, callOpts?), forget() }`.
`call()` POSTs `/api/engine`, unwraps `{ ok, result }`, and throws an `Error`
carrying `.type` / `.message` / `.traceback` / `.stdout` exactly like
`runPython`. It shares `runPython`'s latest-wins **stale-cancel** channel (keyed
by the `.py` path) so slider/scrub calls cancel the ones they supersede, uses the
same `X-Fused` / attribution headers, watches `resolved_py` for auto-reload, and
signals `noteFsChanged()` after every call (even a failed one).

### Hosted / exported fallback

A warm worker is pure optimization — the same `main()` also runs per call. In the
local runtime, if the server becomes unreachable mid-session (a `fetch`
`TypeError`, never an HTTP status), a call degrades transparently to
`fused.runPython` so the page keeps working, correct-but-not-warm. The separate
hosted/exported runtime (shipped in the `fused` wheel) mirrors `fused.engine` as
a straight alias to `runPython`, since it has no local server to warm. No export
rejection is needed (unlike `fused.ai`).

## Security

No new code-execution surface over `/api/run`: same `X-Fused` guard, the worker
runs the calling app's own resolved `.py` on the interpreter `projectenv` already
chooses, and there is no caller-supplied interpreter or daemon-path allowlist —
the server resolves both. Anyone who can hit `/api/engine` can already hit
`/api/run` on the same file. The one genuinely new dimension is process lifetime;
it is bounded by idle-retire, heal-on-failure, kill-at-shutdown, and
cancel-on-disconnect.

## Not yet implemented

The following are deliberately deferred; the current single-worker path is
correct without them:

- **Multi-process pool per script.** In-process threads already parallelize
  I/O-bound `main()`. A pool would only add value for CPU-bound (GIL-bound)
  `main()`.
- **Per-origin LRU cap.** Idle-retire bounds accumulation over time; a hard cap
  on concurrently warm workers per page is not yet enforced.
- **Building an unbuilt venv.** The warm path does not drive the
  `/api/env/install` progress flow. A folder whose venv has never been installed
  returns `409`; the user opens the file once through `/api/run` (which installs
  it), then the warm path finds the venv ready.
- **A per-call watchdog inside the worker.** The ~60 s per-call budget is
  enforced parent-side by the proxy; the worker does not kill its own thread.
