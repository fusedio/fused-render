# Warm workers for apps: a zero-config `/api/run` that stays alive

**Status: proposal** (relates to `docs/ENGINE_HOST_DESIGN.md` from #736).

## 1. Problem

Every `fused.runPython("./x.py", …)` call is a **fresh short-lived subprocess**
that imports the module, runs `main()`, and exits. Nothing survives between
calls, so an app that talks to a remote service re-pays the whole cost every
call. Measured for `s3-browser` (botocore + AWS): each S3 read is **~7 s** —
~3.5–4.5 s of subprocess/engine bootstrap plus ~2.5 s to `import botocore` and
build the client, before the actual API call. A bucket switch fires two such
calls back-to-back (~14 s).

None of that is inherent. If the worker process *stayed alive*, `import botocore`
would run once and a warm client could be reused, turning each read into a
localhost round-trip + the AWS call.

The engine host (#736) already supervises long-lived workers — but only for
**built-in templates**, and only if the author writes a full socket-server
daemon and hands the server an interpreter and a daemon path
(`engine_host._validate` requires `<templates-root>/<id>/daemon.py`). A
**community app author** has none of that: they only write their own page and
`.py`, they never touch fused-render's code, and asking them to register or
declare anything in a config/manifest is the wrong model.

## 2. The idea, in one sentence

Add a **warm variant of `/api/run`**: the app page calls an endpoint naming its
**own** `.py` (exactly as `/api/run` already does), the server keeps that
worker process **alive** between calls instead of killing it, and every later
call re-invokes the script's `main()` in the already-warm process. **No
manifest, no registration, no file the author has to edit** — they swap one call
and get the speedup.

This mirrors what already exists:

| | Fresh (today) | Warm (proposed) |
|---|---|---|
| Endpoint | `POST /api/run` | `POST /api/engine` |
| Process | spawned per call, exits | spawned once, kept alive |
| Author writes | `main(**params)` | the **same** `main(**params)` |
| Module top (`import botocore`) | runs every call | runs **once** |
| Module globals (`_clients = {}`) | discarded each call | **persist** across calls |
| Interpreter chosen by | `projectenv` (per app) | `projectenv` (per app) — identical |
| Trust | app's own code, `X-Fused` guard | app's own code, `X-Fused` guard — identical |

## 3. Author experience (nothing to configure)

Today:

```js
const res = await fused.runPython("./s3.py", { action: "list_objects", bucket, prefix });
```

After:

```js
const s3 = fused.engine("./s3.py");                 // names their own file; no registration
const res = await s3.call({ action: "list_objects", bucket, prefix });
```

`s3.py` is **unchanged** — same `main(action, bucket, prefix, …)`. The only
thing the author *may* now do (optionally) is move expensive setup to module
scope so it survives between calls:

```python
import botocore.session          # runs once, not per call
_clients = {}                     # warm client cache, persists across calls

def main(action, account_id="", bucket="", prefix="", **kw):
    client = _clients.get(account_id) or _clients.setdefault(account_id, _build(account_id))
    ...
    return {"...": "json"}        # same JSON contract as /api/run
```

That's the whole change on the app side. The author never edits a manifest,
never opens fused-render's codebase, never picks an id.

## 4. How it works on the server

`/api/engine` reuses the machinery `/api/run` already has:

1. **Resolve the file and interpreter exactly like `/api/run`.** Body is
   `{ py, html, params }` — the same shape `run.py` accepts. Relative `py` is
   resolved against `html`; the interpreter comes from `projectenv`
   (`project_root_for` → `project_env_for`/`venv_dir_for`): bundled
   `sys.executable` when the app has no environment, its project venv otherwise.
   **No caller-supplied interpreter, no path allowlist** — same trust as
   `/api/run`.
2. **Key a warm worker by the resolved absolute `py` path** (per interpreter).
   The first call spawns it; later calls to the same file reuse it. The "engine
   id" is just the script path — nothing for the author to name or collide on.
3. **The warm worker is a standard shipped loop**, not something the author
   writes. It imports the target module once and then, per request, calls its
   `main(**params)` and returns the JSON — the same result contract `/api/run`
   produces (`{ok, result, error:{type,message,traceback}, stdout}`). Think of
   it as today's `executor._child.py` promoted from run-once to a serve loop.
4. **Supervision reuses the engine host** (`server/engine_host.py`): bind `:0`,
   status file, `/ping`, heal-on-failure (restart + retry once), idle-retire,
   kill-at-shutdown, and the proxy's cancel-on-disconnect — all already built in
   #736. The new part is the *standard worker* and the *resolve-from-`py`*
   bring-up, so no daemon authoring and no manifest are involved.

## 5. What "warm" changes — the contract (and its guardrails)

Reusing the process is the entire point, and it has exactly one consequence the
author must understand: **module-level state persists between calls.** That is
the feature (warm imports, cached clients) and the only footgun (a global left
in a bad state leaks to the next call). Guardrails:

- **Opt-in.** `/api/run` stays the default and stays always-fresh. A script only
  runs warm when the page calls `fused.engine(...)`, so nothing changes for
  existing apps. The author is choosing "keep me warm," so `main()` being safe to
  call repeatedly in one process is their side of the bargain (true for
  read-style handlers, which is the whole target).
- **Crash isolation is preserved.** The worker is still a subprocess; a crash or
  `sys.exit` kills the worker, not the server. The host restarts it and the next
  call re-warms — heal-on-failure already does this.
- **Edit-and-reload still works.** `/api/run` gets fresh code for free every
  call; a warm worker would hold stale code. The runtime already learns the
  file that ran (`result["resolved_py"]`, LR-2) and watches it; the host must
  **recycle the warm worker when its `py` (or its venv) changes on disk**, so
  saving the script behaves like it does today.
- **Per-call timeout unchanged.** The same ~60 s per-call budget applies; only
  the *process* outlives the call.
- **Idle-retire.** A warm worker is reaped after inactivity (e.g. 15 min) and a
  per-origin cap LRU-retires the rest, so a session that opens many warm apps
  can't accumulate processes. First call after retirement simply re-warms.

## 6. Concurrency

One warm worker per script would serialize concurrent calls (e.g. a listing and
a `head_object` fired together). Keep a **small pool per script** (say up to
N=4) so a handful of concurrent calls run in parallel, each on its own warm
process; calls beyond the pool queue briefly. This preserves the "fresh
subprocess" isolation between *concurrent* calls while still amortizing warmth.
(Single-worker is a valid first cut; the pool is a knob.)

## 7. Surface

**Endpoint** (new): `POST /api/engine` with `{ py, html, params }` — identical to
`/api/run` plus warm semantics. Optional `POST /api/engine/forget` `{ py, html }`
to drop a warm worker explicitly (idle-retire covers the common case). Both
carry the `X-Fused` guard, like `/api/run`.

**JS bridge** (new, in `static/runtime.js`): `fused.engine(pyPath, opts?)`
returns a handle:

```js
const eng = fused.engine("./s3.py");
await eng.call(params);        // POST /api/engine; same result + error shape as runPython
await eng.forget();           // optional explicit teardown
```

`call()` reuses `runPython`'s latest-wins **stale-cancel** channel (keyed by the
script path) so slider/scrub calls cancel the ones they pass, and the proxy's
cancel-on-disconnect interrupts the warm worker. Attribution/guard headers
(`X-Fused`, `X-Fused-Page`, `X-Fused-Call`) are added by the runtime exactly as
for `runPython`.

(Alternative surface, if you prefer one function: `fused.runPython(py, params,
{ warm: true })`. Same wire path; `fused.engine()` just reads more honestly as
"this process stays alive.")

## 8. Export / hosted (degrade, don't break)

There is no `:1777` server in an exported page, but a warm worker is **pure
optimization** — the same `main()` also runs per-call. So on hosted/exported,
`fused.engine("./x.py").call(params)` transparently falls back to
`fused.runPython("./x.py", params)`. Same app code, warm locally,
correct-but-slower remotely. No export rejection needed (unlike `fused.ai`).

## 9. Security

No new code-execution surface over `/api/run`:

- Same `X-Fused` guard (same-origin local page).
- The worker runs the **calling app's own resolved `.py`**, on the interpreter
  `projectenv` already chooses for it. Anyone who can hit `/api/engine` can
  already hit `/api/run` on the same file.
- No caller-supplied interpreter and no daemon-path allowlist to get wrong — the
  server resolves both, the way it already does.

The one genuinely new dimension is **process lifetime** (a warm worker that
wedges or loops). Mitigations already exist in the engine host: idle-retire,
per-origin cap, heal-on-failure, kill-at-shutdown, cancel-on-disconnect.

## 10. Relationship to the existing engine host (#736)

Two layers, one supervisor:

- **Engine host (present):** supervises an *arbitrary* long-lived child the
  caller provides — used by the map template, which needs a full tile server
  with its own routes. Powerful, but the author writes a whole daemon and the
  server restricts it to templates.
- **`/api/engine` (proposed):** a *zero-config* layer on top for the common case
  — "keep my `main()` warm." The author writes nothing but `main()`; the server
  supplies the standard worker loop and resolves everything from the `py` path.

`/api/engine` should **reuse** `engine_host`'s spawn/health/heal/reap/shutdown
primitives; it adds the standard worker and the resolve-from-`py` bring-up. Both
serve tiles/JSON through the same stable `:1777` origin. Templates keep their
current path unchanged.

## 11. Migration / back-compat

Purely additive. `/api/run` is untouched and stays the default. The map template
and the existing `engine_host` template path keep working. Apps opt in one call
at a time; an app that never calls `fused.engine()` sees no change.

## 12. Concrete change list (grounded in the current tree)

1. `projectenv.py` — expose one `interpreter_for(project_dir)` (bundled
   `sys.executable` vs `venv_dir_for()` python), factoring what `engine.py` /
   `executor.py` already decide, so `/api/run` and `/api/engine` share it.
2. New standard worker (e.g. `fused_render/engine_worker.py`) — imports the
   target module once, then loops: read `params`, call `main(**params)`, write
   the same result envelope `executor`/`_child.py` produce; recycle on `py`
   mtime change; honor per-call timeout.
3. `server/engine_host.py` — add a bring-up keyed by the resolved `py` path that
   spawns the **standard worker** with the app's resolved interpreter (no
   templates-root check, no caller `python`). Add idle-retire + per-origin cap +
   mtime recycle. Keep the existing template path as-is.
4. New `server/routers/` route (or fold into `run.py`): `POST /api/engine`
   (+ `/api/engine/forget`), resolving `py`/`html`/interpreter like `run.py`,
   then proxying to the warm worker via the engine host.
5. `static/runtime.js` — add `fused.engine(py, opts)` → `{ call, forget }` with
   ensure-once, the `runPython` stale-cancel channel, header attribution,
   healing, and the hosted `runPython` fallback.
6. Optional small per-script **pool** (§6).
7. Docs + tests (warm-state persistence, mtime recycle, crash-restart, idle
   retire, hosted fallback; existing template/map suites stay green).

## 13. Open questions for the team

- **Pool size** per script (1 to start, or N=4?) and **idle default**.
- **`/api/engine` vs `/api/run {warm:true}`** as the wire shape.
- **mtime recycle**: recycle the worker on any change to the resolved `py`, or
  the whole project? (Proposal: the `py` file, matching LR-2's watch.)
- **venv-not-built**: reuse the `/api/env/install` progress flow `/api/run`
  already drives.
- Should a warm worker be allowed to keep **non-recomputable** in-process state
  (then it can't degrade to `runPython` on hosted)? Proposal: no — warm is
  strictly an optimization; anything that needs it must also work per-call.

## 14. Worked example: `s3-browser`

- No new files, no metadata edits. `s3.py` keeps its `main()`; expensive setup
  (`import botocore`, a `_clients` cache) moves to module scope.
- The page swaps `fused.runPython("./s3.py", p)` for
  `fused.engine("./s3.py").call(p)`; `runPython` remains the hosted fallback
  automatically.
- Result: first read per session warms once (~5 s); every read after is a warm
  localhost round-trip + the AWS call — the ~7 s/read collapses toward network
  latency, and a bucket switch stops being a ~14 s two-subprocess stall.
