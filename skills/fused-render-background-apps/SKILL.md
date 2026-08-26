---
name: fused-render-background-apps
description: How to give a fused-render app folder its own long-running daemon that the server supervises — resident, exempt from idle-retire, killed with the server, resurrected at every server start while the user has it enabled. Use when a folder needs to keep running after its page closes, hold a persistent connection or in-memory state across calls, poll something on a timer, or run native desktop UI (tray icon, menu-bar item); when the user mentions "background app", "daemon", `[tool.fused-render.app]`, `kind = "background"`, `fused.app.status/enable/disable/stop/restart/call`, or asks why a warm worker "keeps dying" after 15 minutes idle.
---

# Background apps: a folder's own long-running daemon

A background app is a **third** way a folder's Python runs alongside `/api/run` and the warm `/api/engine` worker — and picking the wrong one is the main mistake here. Read the table before writing anything.

| Path | Lifetime | State survives between calls? | Idle-retired? |
|---|---|---|---|
| `/api/run` (`fused.runPython`) | Fresh subprocess **per call**, killed at a 60 s timeout (`fused-render-authoring`'s "Long-running work") | No — nothing persists | N/A, it's already gone |
| Warm `/api/engine` worker | Persistent interpreter, spawned on first call | Yes — imports, globals, loaded models | **Yes, after 15 min idle** (`engine_host.APP_IDLE_RETIRE_S`) |
| Background app | Resident daemon, spawned at enable and at every server start | Yes — it's a whole separate process with its own lifecycle | **No** — that's the entire point |

Reach for a background app only when the warm worker's idle-retire is the actual problem: a websocket/poll server, something holding a device handle or a long-lived socket, a tray/menu-bar presence, or state that must survive an idle gap longer than 15 minutes. If your state just needs to survive between two calls that happen within a few minutes of each other, the warm worker (already zero-config — no manifest, no enable step) is simpler and is what `/api/engine` already gives you for free.

## The manifest

A folder opts in with a table in its own `pyproject.toml`:

```toml
[tool.fused-render.app]
kind = "background"
daemon = "daemon.py"   # a filename, resolved inside this folder
```

`background_apps.load_manifest` (`fused_render/background_apps.py`) reads this and **never raises** — a bad manifest just reads as "no manifest" rather than reaching `engine_host` as an opaque `python <folder>` bring-up failure. It rejects, silently:

- `kind` anything other than `"background"`, or the table missing entirely.
- `daemon` missing, not a string, or empty.
- `daemon` resolving **outside** the folder — `daemon = "../elsewhere.py"` or a symlink that escapes, realpath-checked (the same containment guard `registered_apps.py` uses for its own folders).
- `daemon` naming a **directory**, not a file (`daemon = "."` passes the containment check trivially — a folder is "inside" itself — so this needs its own `os.path.isfile` check).

There's no error surfaced for a rejected manifest short of the folder just not showing an enable option; check `GET /api/apps/background/status?html=<page>` if you're unsure whether yours parsed.

## The daemon contract

Your `daemon.py` is a plain script, run directly as `[interpreter, daemon.py, --status <path>, --cache <dir>, --version <str>]` (`engine_host._spawn`) — not imported, not run through `_child.py`, no framework to subclass. `tests/fixtures/background_app/daemon.py` is the minimal stdlib-only reference; read it in full before writing your own; this section only calls out what it does and *why*, so you don't cargo-cult past the reasoning:

1. **Parse `--status`, `--cache`, `--version`** (argparse, all three `required=True`).
2. **Bind `("127.0.0.1", 0)`** — port 0 asks the OS for a free port; never hardcode one.
3. **Publish `{port, token, pid}` to the status file, atomically, BEFORE serving** — write to a temp file in the same directory, then `os.replace()` over the real path. This ordering is not a style choice: `_spawn` polls the status file in a loop, and binding-then-publishing-then-serving is *the only sequence with no race* — the parent can never observe a port that isn't yet accepting connections, because the file doesn't exist until after `bind()` succeeds. Publish before you call `serve_forever()`.
4. **Generate a random token** (`secrets.token_urlsafe(32)` in the fixture) and require it on every request — `?t=<token>` in the fixture, checked with `secrets.compare_digest`. The parent proxies all traffic through the stable server origin (`/api/engines/<id>/proxy/...`); the browser never sees your port or token directly, but your daemon still has to enforce it, because it's listening on a loopback port any local process can otherwise reach.
5. **Answer `GET /ping`** with `{"ok": true, "version": <the --version you were given>}`. `_spawn`'s bootstrap wait polls this after the status file lands, and `ensure_background`'s reuse check pings it on every subsequent call — a `/ping` that returns the wrong `version` string is treated as a stale child and respawned.
6. **Answer `GET /quit`** by starting `shutdown()` on a separate thread (never call `server.shutdown()` from inside the request handler thread that's serving it — it deadlocks `ThreadingHTTPServer`). `stop()`/`disable()`/a respawn go through `engine_host`'s own SIGTERM→SIGKILL tree-kill (`_kill_tree`), not `/quit` — nothing in the shipped code calls it — but it costs nothing to implement and is a cleaner exit path for your own tooling.

Everything else — what routes you serve, what state you hold, what background threads you run — is yours. The fixture also serves `POST /count` as a worked example of the request shape `fused.app.call()` actually sends (a JSON body, POST only — the runtime hardcodes the verb).

## Driving it from the page: `fused.app`

```js
await fused.app.enable();          // persist "keep this running" + start it now
const st = await fused.app.status(); // {enabled, running, pid, version, engine_id}
const res = await fused.app.call("/count", { hello: "world" }); // POST, proxied to the daemon
await fused.app.stop();            // kill it now, stays enabled
await fused.app.restart();         // respawn
await fused.app.disable();         // kill it AND un-enable
```

Every method except `call` sends **this page's own path**, never a folder path — the server resolves which app folder the page belongs to server-side, the same `resolve_py` pattern `/api/run`/`/api/engine` already use, so there's no path-typed API to defend. `call(path, body)` reaches the daemon directly through `/api/engines/<engine_id>/proxy/<path>`, resolving `engine_id` from a cached `status()` call (fetching one first if none is cached yet) and rejecting client-side if the app isn't known to be running — call `enable()` first.

**`stop()` and `disable()` are not interchangeable — there is a test for exactly this (`test_api_stop_vs_disable_distinguished`, `tests/test_background_apps.py`):**

- `stop()` kills the daemon **right now** but leaves it in the enabled store. The server's startup resurrection hook (or a later `enable()`/`restart()`) brings it back. This is "quit this app for now."
- `disable()` kills the daemon **and removes it from `background_apps.json`**. Nothing brings it back — not a server restart, not anything — until something calls `enable()` again. This is "turn it off."

If your page conflates the two (e.g. treats `disable()` as just "stop it" and forgets it also un-persists, or calls `stop()` when the user actually asked to uninstall), the daemon either comes back uninvited on the next server launch or refuses to survive a page reload the user expected it to survive.

## Enablement is the user's decision, not yours

Opening a folder, or even the page loading and calling `fused.app.status()`, **never starts or persists anything**. `enabled_paths()` reads `<home_dir()>/background_apps.json` — the *only* thing "enabled" means — and the startup resurrection hook only resurrects folders already in that store. Your daemon comes up when something explicitly calls `POST /api/apps/background/enable` (i.e. `fused.app.enable()`), typically from a button the user clicks, never from page load. Do not call `enable()` in an `onload`/init path — that would install a permanent, server-launch-surviving daemon on someone's machine the moment they open your folder, without them asking for it.

## The venv precondition, its 409, and where the daemon actually runs

`enable()` (and `restart()`'s ensure-background fallback) return **409** when the folder's own project venv isn't built yet:

```json
{"error": "<folder> needs its project environment built before its background app can start; open it once (or call fused.runPython) to install it, then retry.", "status": 409}
```

Building a venv can take minutes, and this endpoint won't do it inside a POST — open the page once, or call `fused.runPython`, to trigger the build, then retry `enable()`. This is the identical stance `/api/engine`'s warm-worker dispatch already takes; see `fused-render-authoring` for the general venv-precondition rule and what "open it once" builds.

**The ordering trap specific to background apps (D499):** the daemon runs on **the folder's own declared environment**, resolved from the folder itself — never from an ancestor project. `background_apps.interpreter_for` calls `projectenv.has_project_env(folder)` on the app's own folder only; it does **not** walk upward the way a plain `.py` script's environment resolution does. If your folder declares no `[project]` deps of its own, your daemon runs on `sys.executable` (this app's own bundled interpreter) regardless of what any parent directory declares — `import fused_render` is **not** available there. And your folder's `[project]` dependency list (in its own `pyproject.toml`) is the **complete** list your daemon gets; nothing from this app's own bundled dependency set is unioned in. Declare everything your `daemon.py` imports, stdlib aside.

(A background app whose folder is nested deeper than `<fused_dir>/<tag>/<name>` may currently 409 forever on project-boundary resolution — that's a known bug being fixed elsewhere, not documented behavior. The rule above — the folder is the boundary — is what to build to.)

## Two things verified against the code, not assumed

**Does a background daemon get `templates/shared` seeded onto `sys.path`? No.** `import fused_ai` does **not** work in a background daemon.

- A `.py` run under `/api/run` gets it: `fused_render/_child.py:68-69` explicitly does `sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "shared"))` before running the module — that's why `import fused_ai` works there and in the warm `/api/engine` worker (`engine_worker.py`, same pattern).
- A background daemon is **not** spawned through `_child.py` at all. `engine_host._spawn` runs `[child.python, child.daemon, --status, ..., --cache, ..., --version, ...]` directly as the process's own `argv[0]` script — there is no import-and-seed step, and nothing in `engine_host.py` appends `templates/shared` to anything.
- Worse, if your daemon runs on a project venv (not `sys.executable`), `_spawn_env` actively **strips** `PYTHONPATH`/`PYTHONHOME`/`PYTHONEXECUTABLE`/`PYTHONSTARTUP` from its environment (`engine_host.py:277-301`) for venv hermeticity — the same stripped-var set `_env_install_worker.py`'s `_STRIPPED_ENV_VARS` uses elsewhere. Even an inherited `PYTHONPATH` wouldn't survive.

If you want `fused_ai` or any other `templates/shared` helper in your daemon, copy the module in (the same "copy this reader" pattern `fused-render-index` uses for its duckdb reader) rather than assuming an import will resolve.

**Does `reinit()` replay apply to a background app? No — your daemon must survive its own restart on its own.**

- `engine_host.reinit(engine_id, key, path, payload)` records a request so a **template daemon**'s restart can replay it into the fresh child before any retried request lands — that's what makes a map-tile daemon's death invisible to the page (`engine_host.py`'s module docstring, and `_replay`/`restart`).
- Nothing in `fused_render/background_apps.py` or `server/routers/background_apps.py` ever calls `engine_host.reinit()` or `forget()` — grep confirms the only callers are in `server/routers/engines.py`, the template-daemon proxy path.
- `engine_host.restart(engine_id)` still calls `_replay(child)` unconditionally for every kind, but for a `bg_*` engine_id `_reinit` was never populated, so replay is a no-op empty loop — there's nothing for it to replay even by accident.

Practically: a background app is not one of the descriptors a page "registers" the way a template registers a viewport. If your daemon dies and gets respawned (`restart()`, or a fresh server launch), it starts with **whatever state its own startup code rebuilds** — nothing outside re-POSTs anything into it for you. Persist to disk under your own cache dir (`background_apps.cache_dir_for(engine_id)` — under `home_dir()`, never beside the user's code) or rebuild from scratch in your own `main()`, and design for the daemon dying and restarting as a normal event, not an exceptional one.

## Cross-platform: native desktop work

A daemon doing native desktop UI — a macOS menu-bar item, a Windows/Linux tray icon — will not run unmodified on another platform, and there's no cross-platform API in scope here to hand you one. Two facts to work from instead of building a fourth implementation:

- **fused-render already ships three tray/menu-bar backends.** `fused_render/supervisor/tray.py` is the platform-neutral seam (`TrayAction`, `_State`, `TrayHandle`, the retry-with-backoff `start()`), dispatching by `sys.platform` at call time to `fused_render/supervisor/_win32/tray.py` (pystray) or `fused_render/supervisor/_linux/tray.py` (StatusNotifierItem over D-Bus); `fused_render/menubar_pin.py` is the separate macOS AppKit/NSPopover implementation. Guard any platform-specific branch in your own daemon (`sys.platform`) rather than writing a fourth backend from scratch — read the existing one for your target platform as a reference before reinventing its retry/backoff or lifecycle handling.
- **On macOS, `NSApplication.run()` cannot leave the main thread.** Every AppKit call in `menubar_pin.py` is documented "main thread only," and cross-thread work hops onto it with `PyObjCTools.AppHelper.callAfter(fn)` (used throughout `menubar_pin.py`, e.g. line 217, 260, 648) — the codebase's actual established hop, not merely a documented option. If your daemon's HTTP handler thread needs to touch AppKit state, post the work through `callAfter` (or `NSOperationQueue.mainQueue()`, the lower-level equivalent) rather than calling into AppKit directly from the handler thread.

## Pitfalls checklist

- Writing `daemon.py` to publish the status file AFTER calling `serve_forever()` → the parent's bootstrap poll never sees it in time, or worse, sees a port that isn't accepting connections yet; publish immediately after `bind()`, before serving.
- Calling `server.shutdown()` from inside the request handler answering `/quit` → deadlocks `ThreadingHTTPServer`; shut down from a separate thread.
- Expecting `fused.app.enable()` from page load to be harmless → it installs a server-launch-surviving daemon the user never asked for. Gate it behind an explicit user action.
- Treating `stop()` and `disable()` as the same action → `stop()` comes back at the next server start; `disable()` doesn't. A test (`test_api_stop_vs_disable_distinguished`) fails if this collapses.
- Assuming `import fused_ai` (or any `templates/shared` helper) works in a daemon because it works in `/api/run` → it doesn't; `_child.py`'s `sys.path` seeding never runs for a background daemon spawn. Copy the module in.
- Expecting a daemon's descriptors to survive a restart the way a template's do → `reinit()`/`_replay` is never invoked for background apps; your daemon must rebuild or persist its own state.
- Declaring a dependency only in this app's own environment and expecting it to reach the daemon → the folder's own `[project]` table is the complete dependency list; nothing bundled is unioned in.
- Placing `daemon` outside the folder (`../daemon.py`) or naming a directory (`daemon = "."`) → both are silently rejected by `load_manifest`; the manifest reads as absent with no error surfaced to the page.
- Calling `enable()` before the folder's venv is built → 409; open the page once (or `fused.runPython`) first, per the same stance `/api/engine` already takes.
- Reaching for a background app when the warm `/api/engine` worker's 15-minute idle-retire was never actually the problem → the manifest, enable step, and daemon HTTP contract are all overhead a zero-config warm worker doesn't need.
- Writing a fourth tray/menu-bar implementation instead of reading `supervisor/tray.py` (+ its `_win32`/`_linux` backends) or `menubar_pin.py` first.
- Touching AppKit state from your daemon's HTTP handler thread on macOS → `NSApplication.run()` requires the main thread; hop with `PyObjCTools.AppHelper.callAfter`.

## When to switch skills

- Writing or debugging the page's own `.html`/`.py` — `fused.runPython`, params, file IO, the venv-build precondition in general → **`fused-render-authoring`**.
- The warm, zero-config `/api/engine` worker (persistent interpreter, 15-minute idle-retire, no manifest) instead of a resident daemon → **`fused-render-authoring`**'s "Long-running work" section.
- Reading or querying the machine-wide file index from your daemon or page → **`fused-render-index`** (its "copy the reader, don't import fused_render" pattern is the same one this skill points you at for `templates/shared`).
- Calling AI models, local or hosted, from a page or from Python → **`fused-render-ai`**.
- Registering a preview template for a file extension (a different, page-rendering concept from a background daemon) → **`fused-render-custom-templates`**.
- Just opening/running the app or a view → **`fused-render-usage`**.
