---
name: fused-render-background-apps
description: Give a fused-render app folder its own long-running daemon the server supervises — a `daemon =` HTTP surface (resident) or a `main =` warm worker (idle-reaped), opt-in autostart. Use when a folder must keep running after its page closes, hold a connection or native tray UI, or when a `main =` daemon "keeps dying" after 15 minutes idle; covers [tool.fused-render.app] and fused.daemon.*.
---

# Background apps: a folder's own long-running daemon

A background app is a **second** way a folder's Python runs alongside `/api/run` — and picking the wrong protocol within it is the main mistake here. Read the table before writing anything.

| Path | Lifetime | State survives between calls? | Idle-reaped? |
|---|---|---|---|
| `/api/run` (`fused.runPython`) | Fresh subprocess **per call**, killed at a 60 s timeout | No — nothing persists | N/A, it's already gone |
| Background app, `main =` protocol | The shipped worker, spawned on first call, warm | Yes — imports, globals, loaded models | **Yes, after `idle_timeout_s` idle** (default 900s / 15 min) |
| Background app, `daemon =` protocol | Your own resident daemon, spawned by `start()` and (only if opted in) at every server start | Yes — a whole separate process with its own lifecycle | **No** by default — that's the point of writing your own daemon |

Both protocols are declared in the same `[tool.fused-render.app]` manifest and share the whole rest of this skill — autostart, the preview guard, reinit, cross-platform tray work. Reach for `daemon =` only when `main =`'s idle-reap is the actual problem: a websocket/poll server, something holding a device handle or a long-lived socket, a tray/menu-bar presence, or state that must survive an idle gap longer than `idle_timeout_s`. State that only needs to survive between calls a few minutes apart already has `main =`, which needs no daemon script of your own — you write a plain `main(**params)`, the same shape `/api/run` calls.

## The manifest

A folder opts in with a table in its own `pyproject.toml`, declaring exactly one of two protocol keys:

```toml
[tool.fused-render.app]
daemon = "daemon.py"    # your own HTTP surface; resident by default (idle_timeout_s=0)
```

```toml
[tool.fused-render.app]
main = "compute.py"     # the shipped worker calls main(**params); reaped after
                        # idle_timeout_s idle seconds (default 900)
```

An optional `idle_timeout_s = <float>` overrides that protocol's default — `0` means resident/never-reaped, so a `main =` folder can opt into staying warm forever, and a `daemon =` folder can opt into idle retirement.

`background_apps.load_manifest` reads this and **never raises** — a bad manifest reads as "no manifest" rather than reaching `engine_host` as an opaque `python <folder>` bring-up failure. It rejects, silently:

- Declaring **both** `daemon` and `main`, or **neither** — exactly one protocol key is required, never inferred.
- The declared key's value missing, not a string, or empty.
- The value resolving **outside** the folder — `daemon = "../elsewhere.py"` or a symlink that escapes, realpath-checked (the same containment guard `registered_apps.py` uses).
- The value naming a **directory** (`daemon = "."` passes containment trivially — a folder is "inside" itself — so it needs its own `os.path.isfile` check).

No error is surfaced for a rejected manifest short of the folder simply not offering a start option; check `GET /api/apps/background/status?html=<page>` if you're unsure yours parsed.

## The daemon contract

Your `daemon.py` is a plain script, run directly as `[interpreter, daemon.py, --status <path>, --cache <dir>, --version <str>]` (`engine_host._spawn`) — not imported, not run through `_child.py`, no framework to subclass. `tests/fixtures/background_app/daemon.py` is the minimal stdlib-only reference; read it in full before writing your own. What it does, and why:

1. **Parse `--status`, `--cache`, `--version`** (argparse, all three `required=True`).
2. **Bind `("127.0.0.1", 0)`** — port 0 asks the OS for a free port; never hardcode one.
3. **Publish `{port, token, pid}` to the status file, atomically, BEFORE serving** — temp file in the same directory, then `os.replace()`. Not a style choice: `_spawn` polls the status file, and bind → publish → serve is *the only sequence with no race*, because the file cannot exist until `bind()` has succeeded. Publish before `serve_forever()`.
4. **Generate a random token** (`secrets.token_urlsafe(32)`) and require it on every request — `?t=<token>`, checked with `secrets.compare_digest`. The parent proxies all traffic through the stable server origin (`/api/engines/<id>/proxy/...`) so the browser never sees your port or token, but you still enforce it: you are listening on a loopback port any local process can reach.
5. **Answer `GET /ping`** with `{"ok": true, "version": <the --version you were given>}`. The bootstrap wait polls this, and every later reuse check pings it — a `/ping` returning the wrong `version` is treated as a stale child and respawned.
6. **Answer `GET /quit`** by starting `shutdown()` on a separate thread (calling `server.shutdown()` from the handler thread serving it deadlocks `ThreadingHTTPServer`). Nothing shipped calls `/quit` — `stop()` and respawns go through `engine_host`'s SIGTERM→SIGKILL tree-kill — but it is a cleaner exit path for your own tooling.

Everything else — routes, state, threads — is yours. The fixture also serves `POST /count` as a worked example of the request shape `fused.daemon.call()` sends (JSON body, POST only; the runtime hardcodes the verb).

## Asking the server about yourself, from inside the daemon

Every `fused.daemon` method sends the caller's page path as `html`, which the server resolves to an app folder. Your daemon has no page — so it uses `templates/shared/background_app.py` instead, which reads the `FUSED_RENDER_APP_DIR` that `engine_host._spawn_env` exports into every `kind="background"` child.

**A bare `import background_app` does not work here.** `/api/run` and the warm worker get `templates/shared` appended to `sys.path` for free (`_child.py`); a background daemon is spawned as a plain script with no such seeding, so the import raises `ModuleNotFoundError`. Bootstrap it off the discovery file `server/app.py` writes at bind time — `server.json`'s `shared` key names the exact `templates/shared` this server build ships, and `FUSED_RENDER_HOME_DIR` (branch-resolved for a dev worktree server, falling back to `~/.fused-render`) says where to find it:

```python
import json, os, sys

def _bootstrap_background_app():
    home_dir = os.environ.get("FUSED_RENDER_HOME_DIR") or os.path.expanduser(
        "~/.fused-render")
    with open(os.path.join(home_dir, "server.json"), encoding="utf-8") as f:
        info = json.load(f)
    if info["shared"] not in sys.path:
        sys.path.insert(0, info["shared"])
    import background_app
    return background_app

background_app = _bootstrap_background_app()

background_app.status()             # {"running", "autostart", "pid", "version", "engine_id", "protocol"}
background_app.stop()               # kills THIS process's daemon; autostart untouched
background_app.set_autostart(True)  # persists the "come back at next launch" flag only
background_app.restart()            # respawns
```

This is the production pattern, not a simplification — see `_bootstrap_fused_ai`/`_bootstrap_background_app` in the OpenWhisper tray's `menubar.py` for the shipping version, which wraps it in a try/except so an unreadable `server.json` degrades to `None` rather than crashing the daemon at import time.

A tray "Quit" should call `stop()` here rather than a raw self-`terminate`/`exit` — see "When does it come back?" below. `set_autostart(bool)` is separate and orthogonal: it never starts or stops anything, so a "Start automatically" checkmark calls it directly. Raises `NotUnderEngine` if `FUSED_RENDER_APP_DIR` isn't set (you are not an engine-spawned daemon), and `ServerNotRunning`/`BackgroundAppError` for the same reasons `fused_ai`'s equivalents do.

## Driving it from the page: `fused.daemon`

```js
await fused.daemon.start();                // spawn it now — does NOT touch autostart
const st = await fused.daemon.status();     // {running, autostart, pid, version, engine_id, protocol}
const res = await fused.daemon.call("/count", { hello: "world" }); // POST, proxied to the daemon
const out = await fused.daemon.run({ hello: "world" }); // main = convenience: call("/call", params), envelope unwrapped
await fused.daemon.stop();                  // kill it now — does NOT touch autostart
await fused.daemon.restart();               // respawn — autostart-neutral too
await fused.daemon.setAutostart(true);      // ONLY thing that persists "bring this back at launch"
const unsubscribe = fused.daemon.watch((s) => {
  // s is the same shape status() resolves to. Fires on the initial read and
  // again whenever running/autostart/pid/version actually changes — NOT on
  // every poll tick. This is how you reflect state that changed for a reason
  // outside this page's own control, e.g. the tray's Quit.
});
```

The JS namespace is `daemon` while the HTTP endpoints (`/api/apps/background/*`) and Python modules say "background": "app" already means three other things here (an `fused-app`-tagged folder, the warm worker's `Child.kind`, the `/apps` hub), and `daemon` is the noun the manifest, the file and `engine_host` already use.

Every method except `call`/`run` sends **this page's own path**, never a folder path — the server resolves which app folder the page belongs to, the same `resolve_py` pattern `/api/run` uses, so there is no path-typed API to defend. `call(path, body)` reaches the daemon through `/api/engines/<engine_id>/proxy/<path>`, resolving `engine_id` from a cached `status()` and bringing the daemon up transparently when it isn't known to be running — on a page's first call, or after the idle reaper has retired it — the same way `run()` always has; **`start()` is not required first**. `run(params)` is the `main =` convenience over the same proxy mechanics: POST `/call` with `params` as the body, the `{ok, result, error, stdout, resolved_py}` envelope unwrapped the way `runPython` unwraps `/api/run`'s.

Each method also checks the folder actually declared the protocol it speaks, from `status()`'s `protocol` field: `run()` rejects with a "use `call()` instead" error against a `daemon =` folder (its own routes almost certainly don't serve `/call`), and `call()` rejects with a "use `run()` instead" error against a `main =` folder (it would otherwise "work" but hand back the raw envelope instead of the unwrapped result). A folder with no valid manifest at all reports `protocol: null` and gets neither check — its existing error paths already cover it.

**Run state and autostart are two independent axes** (pinned by `test_api_start_calls_ensure_background_without_touching_autostart` and `test_api_autostart_sets_the_flag_without_starting_or_stopping_anything`):

- `start()`/`stop()`/`restart()` change only whether the daemon is alive right now. None of them read or write the persisted autostart flag.
- `setAutostart(true|false)` changes only the persisted "bring this back at every launch" flag. It never starts or stops the daemon.
- **Autostart is opt-in and defaults to off.** A folder nobody has called `setAutostart(true)` on reports `autostart: false` forever, no matter how many times `start()` is called.

Conflate the two and the daemon either fails to survive a restart the user expected it to survive, or comes back uninvited when they only meant to stop it once.

### When does it come back? Two questions, not one

**Is it running right now?** `status().running`. That changes only through `start()`, `stop()`, `restart()`, or heal-on-proxy (below) — never through `setAutostart()`.

**Will it come back at the next server launch?** `status().autostart`. That changes ONLY through an explicit `setAutostart(bool)` (or server-side `background_apps.set_autostart`), and defaults to `false` for every folder.

Given both facts, a stopped daemon comes back through exactly one of these — there is no other path:

1. **Server start, but ONLY if autostart is on.** `_startup_resurrect_background_apps` walks `autostart_paths()` and brings each one up. A folder that was only ever `start()`ed is not in that list — the opt-in default in action, not an omission.
2. **A page calling `start()` or `restart()`.** The deliberate path — unconditional, but never itself sets autostart.
3. **Heal-on-proxy — the one that surprises people, and it is entirely about run state.** If the process was killed EXTERNALLY (a `kill`, a crash, a tray "Quit" that never talks to the server), the `Child` stays registered in `engine_host._children` — nothing popped it out. The next proxied call finds a `Child` that doesn't answer and heals by respawning it (`engine_forward.py`). **`stop()` does not have this problem**: it pops the child out of `_children` before killing it, so a later proxied call finds nothing registered, returns 409, and does not revive it. A raw external kill skips that pop, which makes it structurally the *weakest* way to end a background app — it leaves intact the one piece of bookkeeping that prevents an accidental revival.
4. **Nothing else.** No heartbeat, no polling, no periodic sweep.

So anything letting a user "quit and stay off" from OUTSIDE a page (a tray icon, a CLI, a native menu item) must call `stop()` through the server's own API, never a raw process kill — otherwise it has built path 3 by accident and the app returns the moment anything pokes it. Whether it also stays off at the next launch depends on autostart, which `stop()` never touches: "stopped now AND never comes back automatically" is two calls for two facts.

## Starting it — and opting into autostart — are the user's decisions

Opening a folder, or a page calling `fused.daemon.status()`, **never starts, stops, or persists anything**. `autostart_paths()` reads `<home_dir()>/background_apps.json` — the *only* thing "autostart" means — and the startup hook only resurrects folders already in that store.

### Lead rule: never call `start()` (or `setAutostart(true)`) on page load

An app going from off to running, or from "one-off" to "survives every server restart", is the user's decision, made by clicking something. And "the page's JS ran" is a much wider event than "the user opened my folder": Home's "Fused Apps" strip and the `/apps` hub render a **live picture** of each app, mounting its own `entry_html` in an iframe (`AppPreviewCard`) once the card nears the viewport, and a card that has a `preview.png` still swaps to the live iframe on hover. That iframe's sandbox is `allow-scripts allow-same-origin` — your script runs, on a card someone merely scrolled past.

**The runtime enforces this, it is not only a convention.** `_preview=1` reaches a card's live iframe reliably — `thumbFrame`/`withPreviewFlag` stamp it onto the `/render?path=...` URL that becomes the iframe's `src`, and `GET /render` serves the app's HTML at exactly that URL with no redirect, so the flag lands in the page's own `location.search`. `start()`, `restart()`, `call()`, `stop()` and `setAutostart()` all check it (this frame's URL, or any same-origin ancestor's) and reject before any POST is sent. `status()` is deliberately ungated: it is read-only.

`setAutostart()` is gated for the same reason as `start()`, only more so — an unwanted `start()` dies with the server, while a persisted "come back forever" flag set by a card scrolling past survives every restart.

```js
// Wrong: fires the instant this script runs, including inside a display-only
// preview iframe that scrolled into view. The user never clicked anything.
fused.daemon.start();
fused.daemon.setAutostart(true);

// Right: page load only reads and renders. Starting the daemon and opting into
// autostart are each something a control does, in response to a click — two
// SEPARATE controls, because they are two separate facts.
async function refreshMenubar() {
  const st = await fused.daemon.status();     // {running, autostart, pid, ...}
  render(st);                                 // see "Render both facts" below
}
refreshMenubar();

toggleBtn.addEventListener("click", async () => {
  const turnOn = !toggleBtn.classList.contains("on");
  turnOn ? await fused.daemon.start() : await fused.daemon.stop();
  await refreshMenubar();
});

autostartCheckbox.addEventListener("change", async () => {
  await fused.daemon.setAutostart(autostartCheckbox.checked);
  await refreshMenubar();
});
```

### What a page may do on load

| On load | Allowed | Why |
|---|---|---|
| `fused.daemon.status()` | Yes | Read-only; spawns, persists and kills nothing. |
| `fused.daemon.watch(cb)` | Yes — **one read and nothing more in a preview thumbnail** | `status()` underneath. In a live/hover preview it does a single read and returns a no-op unsubscribe rather than leaving a poll loop running in a sandbox that mounts and unmounts on every hover. |
| Rendering the result | Yes | Pure display logic. |
| `start()` / `restart()` | No — **refused in a preview thumbnail** | Spawns a daemon; a page render is not a reason to start one, or to bounce one the user may be mid-use of. |
| `stop()` | No — **refused in a preview thumbnail** | Turning something the user has running *off* on a page render is exactly as surprising as turning it on. |
| `setAutostart(bool)` | No — **refused in a preview thumbnail** | Persists a flag that survives a server restart. |
| `call(path, body)` / `run(params)` | Only if it is a genuine read — **refused in a preview thumbnail** | `engine_forward.py`'s heal-on-proxy path respawns a dead-but-registered child on *any* proxied call, so a preview calling `call()`/`run()` against an app started elsewhere can resurrect its daemon exactly like `start()` would. Beyond the preview case, your own daemon may treat a route as "start work" — gate those routes behind an explicit control too. |

### How the guard rejects

A rejection is a plain `Error`, never a silent no-op, and it names the method and the rule so the author sees it in the console instead of wondering later why the daemon they turned off keeps coming back:

```
fused.daemon.start: refused — this page is rendering as a preview thumbnail
(a card peek or hover, not a real open), and a page must never start a
background daemon just by being displayed or hovered. Call
fused.daemon.status() on load to read state, and call start()/restart()
only from an explicit user action, e.g. a button's click handler.
```

The guard is **client-side** (`fused_render/static/runtime.js`), deliberately: the flag reaches the app's own frame reliably, so the check belongs where `start()` itself runs, before any request leaves the page. It protects against a careless app, not an evasive one — a page can always `fetch("/api/apps/background/start")` directly, a bypass that exists independent of preview rendering and is out of scope for this guard.

### Render both facts honestly

`status()` reports **`running`** and **`autostart`** as two independent booleans. Most UIs still want to show three states:

- **Running** — up right now, whatever `autostart` says.
- **Not running, `autostart: true`** — "will come back" at the next launch or on an explicit `start()`. Not broken, not an error; it is the ordinary state right after a `stop()`, or after an external kill heal-on-proxy hasn't repaired yet.
- **Not running, `autostart: false`** — the default for every folder, and often the most common state a background app is in. Do not render it as an error, a missing dependency, or a disabled feature.

`Sina/OpenWhisper/index.html`'s `mbRender`/`refreshMenubar` is a worked example — three labels, `.on`/`.pending` classes, and a separate "Start automatically" checkbox for the autostart fact. Read it for the pattern, not as a finished reference.

### Reflect state that changed outside this page — `fused.daemon.watch()`

`status()` alone tells you what is true at the moment you called it. A page that reads once on load and otherwise refreshes only after its own `start()`/`stop()` has a blind spot: **the daemon's state changes for reasons that have nothing to do with this page.** A tray "Quit" routes through the same `POST /api/apps/background/stop` the page's own button does, so the server knows — but a page reflecting only its own actions leaves its icon "on" until someone reloads.

`watch(callback)` closes that gap: it calls back on the initial read and whenever `{running, autostart, pid, version}` changes, polling only while the tab is visible and refreshing immediately on `visibilitychange`→visible and window `focus` — the case that matters most, since reaching for a tray means this page was *not* focused when the state changed.

```js
const unsubscribe = fused.daemon.watch((s) => {
  mbRunning = s.running;
  mbAutostart = s.autostart;
  refreshMenubar();  // re-render from the two facts, same as after your own start()/stop()
});
```

The convention this establishes: **render from the server's actual state, not from an assumption that your own actions are the only source of change.** Updating local state directly in a click handler is fine for immediate feedback; anything a *different* surface (a tray, another tab, startup resurrection) can also change gets read back from the server.

### Give the user a way to turn the app off from inside the app itself

A background app running native desktop UI needs its own off switch there too — a user who never opens the page still needs a way to quit it. Two things follow from the run-state/autostart split:

- A tray "Quit" only needs `stop()`. It should NOT also turn autostart off: "quit right now" and "never come back automatically" are different intents, and conflating them in the tray re-creates the bug the split exists to fix. Offer autostart as its own checkmark item reflecting `status().autostart` (see `menubar.py`'s `toggleAutostart_`).
- Both controls go through the server's API — `fused.daemon.stop()`/`setAutostart()` from the page, or `background_app.stop()`/`set_autostart()` from inside the daemon — never a raw kill for the "stop it" half, which skips the `_children` bookkeeping and lets the next proxied call heal the daemon back to life.

### The daemon is a guest process: assume nothing about its own continuity

- **It may be killed at any time**, by the server's tree-kill or by anything external. Never assume a clean shutdown path runs — persist anything that must survive under `background_apps.cache_dir_for(engine_id)` (never beside the user's code) with interrupt-safe writes: write-to-temp-then-`os.replace()`, the same pattern as the status-file publish.
- **It is not a singleton across restarts.** Nothing replays state into a fresh child (see "reinit" below) — a respawned daemon starts new, with only what it reads back or rebuilds. Design `main()` for "I might be the fifth process this folder has run today".
- **It must tolerate its own work being interrupted mid-request.** A `call()` can be answered by a daemon killed moments later; don't leave on-disk state half-written or a resource half-acquired across that boundary.

### What the version digest expects from you

`version_for` hashes your `pyproject.toml`'s bytes, your `daemon.py`'s mtime+size, and the interpreter's path+mtime+size into the `--version` string you are spawned with; the reuse check retires and respawns a running child the moment that digest changes.

- **You never bump a version yourself.** Editing `pyproject.toml` or `daemon.py` is enough — the next start or reuse check sees a new digest and spawns a fresh child.
- **Never hardcode your `/ping` response's `version`.** Echo the `--version` you were given. Hardcoding defeats the staleness check silently: the reuse check always "matches", the stale child is kept forever, and respawn-on-edit stops working for your app specifically.

### Conventions the engine does not enforce

Nothing below is checked by any code path; no test or manifest rule will catch it.

- Rendering both `status()` facts honestly, and not presenting `autostart: false` as an error.
- Exposing an in-app off switch at all.
- Routing that off switch through `stop()` instead of a raw kill — heal-on-proxy exists precisely because the engine tolerates a raw kill; it does not forbid one.
- Persisting daemon state safely across an unannounced kill, and not assuming it is the only instance the folder has spawned.
- Echoing the real `--version` from `/ping` — defeat the staleness check and nothing complains; the daemon just quietly stops picking up its own updates.

## Where the daemon actually runs

`start()` (and `restart()`'s ensure-background fallback) never block on an unbuilt venv: when the folder's own project venv isn't built yet, `_resolve()` falls back to `sys.executable` and starts the daemon there anyway — the same fallback `/api/run`'s builtin engine takes. There is no 409 to handle; a daemon started this way just doesn't have the folder's own `[project]` dependencies importable until that venv exists.

Server-startup autostart resurrection is stricter: `resurrect_autostart()` will **not** fall back — a folder opted into autostart whose project venv isn't built yet is logged and skipped entirely ("project environment not built yet, skipping (open it once to install it)"), and stays stopped until something else starts it. Open the folder once (or call `fused.runPython`) to install its venv before relying on autostart.

**The ordering trap specific to background apps:** the daemon runs on **the folder's own declared environment**, resolved from the folder itself — never from an ancestor project. `background_apps.interpreter_for` calls `projectenv.has_project_env(folder)` on the app's own folder only; it does **not** walk upward the way a plain `.py` script's resolution does. A folder declaring no `[project]` deps of its own runs its daemon on `sys.executable` (the app's bundled interpreter) whatever a parent directory declares, and `import fused_render` is **not** available there. Its `[project]` dependency list is also the **complete** list the daemon gets — nothing bundled is unioned in. Declare everything the daemon (or `main =` script) imports, stdlib aside.

## Two things that do not work the way `/api/run` does

**`templates/shared` is not on `sys.path`, so `import fused_ai` fails.** A `.py` under `/api/run` gets the seeding from `_child.py`; a background daemon is not spawned through `_child.py` at all — `engine_host._spawn` runs your script directly, and nothing appends `templates/shared`. Worse, a daemon on a project venv has `PYTHONPATH`/`PYTHONHOME`/`PYTHONEXECUTABLE`/`PYTHONSTARTUP` actively stripped by `_spawn_env` for venv hermeticity, so even an inherited `PYTHONPATH` would not survive. To use `fused_ai` or any other shared helper, bootstrap it off `server.json` (as "Asking the server about yourself" does) or copy the module in — the same "copy this reader" pattern `fused-render-index` uses.

**`reinit()` replay does not apply, so your daemon must survive its own restart.** `engine_host.reinit()` records a request so a *template daemon*'s restart can replay it into the fresh child before any retried request lands — that is what makes a map-tile daemon's death invisible to the page. Nothing in `background_apps.py` or its router ever calls it; `restart()` still calls `_replay(child)` for every kind, but a `bg_*` engine never populated `_reinit`, so it is a no-op empty loop. A respawned daemon starts with whatever its own startup code rebuilds — persist under your cache dir or rebuild in `main()`, and treat dying and restarting as a normal event.

## Cross-platform: native desktop work

A daemon doing native desktop UI will not run unmodified on another platform, and there is no cross-platform API in scope to hand you one. Two facts to work from:

- **fused-render already ships three tray/menu-bar backends.** `fused_render/supervisor/tray.py` is the platform-neutral seam (`TrayAction`, `_State`, `TrayHandle`, the retry-with-backoff `start()`), dispatching by `sys.platform` to `supervisor/_win32/tray.py` (pystray) or `supervisor/_linux/tray.py` (StatusNotifierItem over D-Bus); `fused_render/menubar_pin.py` is the separate macOS AppKit/NSPopover implementation. Guard your own platform branch on `sys.platform` and read the existing backend for your target before reinventing its retry/backoff or lifecycle handling.
- **On macOS, `NSApplication.run()` cannot leave the main thread.** Every AppKit call in `menubar_pin.py` is main-thread only, and cross-thread work hops onto it with `PyObjCTools.AppHelper.callAfter(fn)` — the codebase's established hop. If your HTTP handler thread needs to touch AppKit state, post it through `callAfter` (or `NSOperationQueue.mainQueue()`) rather than calling AppKit from the handler thread.

## Pitfalls checklist

- Publishing the status file AFTER `serve_forever()` → the parent's bootstrap poll never sees it in time, or sees a port not yet accepting connections. Publish right after `bind()`.
- Calling `server.shutdown()` from inside the handler answering `/quit` → deadlocks `ThreadingHTTPServer`; shut down from a separate thread.
- `fused.daemon.start()` on page load → spawns a daemon nobody asked for, and a display-only preview iframe runs that same load path. The runtime now refuses it (and `restart`/`call`/`run`/`stop`/`setAutostart`) inside a preview and throws a named error.
- Assuming `start()` or `stop()` also changes whether the app comes back at the next launch → only `setAutostart(bool)` touches that flag.
- `setAutostart(true)` from page load, or as a side effect of a "start it" click → it persists across restarts; give it its own control.
- Ending the app from a tray control with a raw kill instead of `stop()` → skips the bookkeeping that keeps it from silently reviving on the next proxied call.
- Rendering `autostart: false` (or `autostart: true, running: false`) as an error → both are ordinary, and `autostart: false` is the default.
- Assuming a tray "Quit" should also turn autostart off, or that autostart on means running → independent facts.
- Assuming `import fused_ai` works in a daemon because it works in `/api/run` → it doesn't; bootstrap off `server.json` or copy the module in.
- Expecting a daemon's descriptors to survive a restart the way a template's do → `reinit()`/`_replay` is never invoked for background apps.
- Declaring a dependency somewhere other than the folder's own `[project]` table and expecting the daemon to get it → that table is the complete list.
- Placing `daemon`/`main` outside the folder (`../daemon.py`) or naming a directory (`daemon = "."`) → silently rejected; the manifest reads as absent with no error surfaced.
- Declaring both `daemon` and `main`, or neither → silently rejected; exactly one protocol key is required.
- Relying on autostart before the folder's venv is built → `resurrect_autostart()` skips it and logs a warning; open the page once (or `fused.runPython`) first.
- Reaching for `daemon =` when `main =`'s idle-reap was never the problem → the manifest, start step and HTTP contract are all overhead a plain `main =` folder doesn't need.
- Writing a fourth tray/menu-bar implementation instead of reading `supervisor/tray.py` or `menubar_pin.py` first.
- Touching AppKit state from the daemon's HTTP handler thread on macOS → hop with `PyObjCTools.AppHelper.callAfter`.

## When to switch skills

- The page's own `.html`/`.py` — `fused.runPython`, params, file IO, the venv-build precondition → **`fused-render-authoring`**.
- The warm, zero-config worker, or any work longer than one call → **`fused-render-jobs`**.
- Reading the machine-wide file index from your daemon or page → **`fused-render-index`** (its "copy the reader, don't import fused_render" pattern is the same one this skill points at for `templates/shared`).
- Calling AI models from a page or from Python → **`fused-render-ai`**.
- Registering a preview template for a file extension → **`fused-render-custom-templates`**.
- Just opening or running the app → **`fused-render-usage`**.
