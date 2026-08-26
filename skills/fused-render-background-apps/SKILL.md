---
name: fused-render-background-apps
description: How to give a fused-render app folder its own long-running daemon that the server supervises — resident, exempt from idle-retire, killed with the server, resurrected at every server start while the user has explicitly opted it into autostart. Use when a folder needs to keep running after its page closes, hold a persistent connection or in-memory state across calls, poll something on a timer, or run native desktop UI (tray icon, menu-bar item); when the user mentions "background app", "daemon", `[tool.fused-render.app]`, `kind = "background"`, `fused.daemon.status/start/stop/restart/setAutostart/call`, or asks why a warm worker "keeps dying" after 15 minutes idle.
---

# Background apps: a folder's own long-running daemon

A background app is a **third** way a folder's Python runs alongside `/api/run` and the warm `/api/engine` worker — and picking the wrong one is the main mistake here. Read the table before writing anything.

| Path | Lifetime | State survives between calls? | Idle-retired? |
|---|---|---|---|
| `/api/run` (`fused.runPython`) | Fresh subprocess **per call**, killed at a 60 s timeout (`fused-render-authoring`'s "Long-running work") | No — nothing persists | N/A, it's already gone |
| Warm `/api/engine` worker | Persistent interpreter, spawned on first call | Yes — imports, globals, loaded models | **Yes, after 15 min idle** (`engine_host.APP_IDLE_RETIRE_S`) |
| Background app | Resident daemon, spawned by `start()` and (only if opted in) at every server start | Yes — it's a whole separate process with its own lifecycle | **No** — that's the entire point |

Reach for a background app only when the warm worker's idle-retire is the actual problem: a websocket/poll server, something holding a device handle or a long-lived socket, a tray/menu-bar presence, or state that must survive an idle gap longer than 15 minutes. If your state just needs to survive between two calls that happen within a few minutes of each other, the warm worker (already zero-config — no manifest, no start step) is simpler and is what `/api/engine` already gives you for free.

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

There's no error surfaced for a rejected manifest short of the folder just not showing a start option; check `GET /api/apps/background/status?html=<page>` if you're unsure whether yours parsed.

## The daemon contract

Your `daemon.py` is a plain script, run directly as `[interpreter, daemon.py, --status <path>, --cache <dir>, --version <str>]` (`engine_host._spawn`) — not imported, not run through `_child.py`, no framework to subclass. `tests/fixtures/background_app/daemon.py` is the minimal stdlib-only reference; read it in full before writing your own; this section only calls out what it does and *why*, so you don't cargo-cult past the reasoning:

1. **Parse `--status`, `--cache`, `--version`** (argparse, all three `required=True`).
2. **Bind `("127.0.0.1", 0)`** — port 0 asks the OS for a free port; never hardcode one.
3. **Publish `{port, token, pid}` to the status file, atomically, BEFORE serving** — write to a temp file in the same directory, then `os.replace()` over the real path. This ordering is not a style choice: `_spawn` polls the status file in a loop, and binding-then-publishing-then-serving is *the only sequence with no race* — the parent can never observe a port that isn't yet accepting connections, because the file doesn't exist until after `bind()` succeeds. Publish before you call `serve_forever()`.
4. **Generate a random token** (`secrets.token_urlsafe(32)` in the fixture) and require it on every request — `?t=<token>` in the fixture, checked with `secrets.compare_digest`. The parent proxies all traffic through the stable server origin (`/api/engines/<id>/proxy/...`); the browser never sees your port or token directly, but your daemon still has to enforce it, because it's listening on a loopback port any local process can otherwise reach.
5. **Answer `GET /ping`** with `{"ok": true, "version": <the --version you were given>}`. `_spawn`'s bootstrap wait polls this after the status file lands, and `ensure_background`'s reuse check pings it on every subsequent call — a `/ping` that returns the wrong `version` string is treated as a stale child and respawned.
6. **Answer `GET /quit`** by starting `shutdown()` on a separate thread (never call `server.shutdown()` from inside the request handler thread that's serving it — it deadlocks `ThreadingHTTPServer`). `stop()`/a respawn go through `engine_host`'s own SIGTERM→SIGKILL tree-kill (`_kill_tree`), not `/quit` — nothing in the shipped code calls it — but it costs nothing to implement and is a cleaner exit path for your own tooling.

Everything else — what routes you serve, what state you hold, what background threads you run — is yours. The fixture also serves `POST /count` as a worked example of the request shape `fused.daemon.call()` actually sends (a JSON body, POST only — the runtime hardcodes the verb).

## Calling the background-apps API about yourself (D505)

Every `fused.daemon` method sends the caller's own page path as `html`, which
the server turns into an app folder server-side. Your daemon has no page and
no `html` path — so before D505 it had no way to ask the server anything
about itself: not "am I running", not "stop me", not "am I set to autostart".

`engine_host._spawn_env` now exports `FUSED_RENDER_APP_DIR` (the app's own
folder) into a `kind="background"` child's environment, and
`templates/shared/background_app.py` is the stdlib-only client that uses it
— but a bare `import background_app` does NOT work here the way it would
under `/api/run` or the warm `/api/engine` worker. Those get `templates/shared`
appended onto `sys.path` for free (`_child.py:68-69`); a background daemon
is spawned as a plain script with no such seeding (see "Two things verified
against the code" below) — a bare import raises `ModuleNotFoundError`.

Bootstrap it off the same discovery file `server/app.py` writes at bind time
(`write_server_json`, `fused_render/server/app.py:209`): `server.json`'s
`shared` key names the exact `templates/shared` directory this server build
ships, and `FUSED_RENDER_HOME_DIR` (branch-resolved for a dev worktree
server, falling back to `~/.fused-render`) says where to find it:

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

background_app.status()             # {"running", "autostart", "pid", "version", "engine_id"}
background_app.stop()               # kills THIS process's daemon; autostart untouched
background_app.set_autostart(True)  # persists the "come back at next launch" flag only
background_app.restart()            # respawns
```

This is the production pattern, not a simplification of it — see
`_bootstrap_fused_ai`/`_bootstrap_background_app` in the OpenWhisper tray's
`menubar.py` for the shipping version (it also wraps this in a
try/except so a missing or unreadable `server.json` degrades to `None`
rather than crashing the daemon at import time).

Calling `stop()` from inside your own daemon is exactly what a tray "Quit"
should do instead of a raw self-`terminate`/`exit` — see "When does it come
back?" below: an external kill leaves the `Child` registered and gets healed
back to life by the next proxied call, while going through `stop()` pops it
out first. `set_autostart(bool)` is separate and orthogonal (D511) — it
never starts or stops the daemon, so a tray's "Start automatically" checkmark
should call it directly rather than routing through `stop()`/`restart()`.
Raises `background_app.NotUnderEngine` if `FUSED_RENDER_APP_DIR` isn't set
(not running as an engine-spawned background daemon) and
`background_app.ServerNotRunning`/`background_app.BackgroundAppError` for
the same reasons `fused_ai`'s equivalents raise.

## Driving it from the page: `fused.daemon`

The browser namespace is `fused.daemon` (D506) — the HTTP endpoints underneath
it (`/api/apps/background/*`) and the Python modules (`background_apps.py`,
`background_app.py`) still say "background". That split is deliberate, not a
half-finished rename: "app" already means three other things here (an
`fused-app`-tagged folder, `ensure_app`'s warm worker `Child.kind`, the
`/apps` hub), so `fused.app.start()` read as "start this app" when it meant
"install a resident daemon". `daemon` is the noun already used everywhere
else — the manifest's `daemon = "daemon.py"` key, the file itself, and
`engine_host`'s own vocabulary — so only the author-facing JS name changed.

```js
await fused.daemon.start();                // spawn it now — does NOT touch autostart
const st = await fused.daemon.status();     // {running, autostart, pid, version, engine_id}
const res = await fused.daemon.call("/count", { hello: "world" }); // POST, proxied to the daemon
await fused.daemon.stop();                  // kill it now — does NOT touch autostart
await fused.daemon.restart();               // respawn — autostart-neutral too
await fused.daemon.setAutostart(true);      // ONLY thing that persists "bring this back at launch"
const unsubscribe = fused.daemon.watch((s) => {
  // s is the same shape status() resolves to. Fires on the initial read and
  // again whenever running/autostart/pid/version actually changes — NOT on
  // every poll tick. Call this to reflect state that changed for a reason
  // outside this page's own control, e.g. the tray's Quit.
});
```

Every method except `call` sends **this page's own path**, never a folder path — the server resolves which app folder the page belongs to server-side, the same `resolve_py` pattern `/api/run`/`/api/engine` already use, so there's no path-typed API to defend. `call(path, body)` reaches the daemon directly through `/api/engines/<engine_id>/proxy/<path>`, resolving `engine_id` from a cached `status()` call (fetching one first if none is cached yet) and rejecting client-side if the app isn't known to be running — call `start()` first.

**Run state (`start`/`stop`/`restart`) and autostart (`setAutostart`) are two completely independent axes now (D511) — there is a test for exactly this shape (`test_api_start_calls_ensure_background_without_touching_autostart`, `test_api_autostart_sets_the_flag_without_starting_or_stopping_anything`, `tests/test_background_apps.py`):**

- `start()`/`stop()`/`restart()` change only whether the daemon is alive right now. None of them ever read or write the persisted autostart flag.
- `setAutostart(true|false)` changes only the persisted "bring this back at every launch" flag. It never starts or stops the daemon.
- **Autostart is opt-in and defaults to off.** A folder nobody has ever called `setAutostart(true)` on reports `autostart: false` from `status()` forever, no matter how many times `start()` is called on it.

If your page conflates the two (e.g. assumes `start()` also makes the app come back at next launch, or that `stop()` also turns autostart off), the daemon either fails to survive a server restart the user expected it to survive, or comes back uninvited when the user only meant to stop it once.

### When does it come back? Two questions, not one (D505, D511)

There used to be one question here ("is it enabled?"); now there are two, and
conflating them is the exact bug this decision fixes. Ask them separately:

**Is the daemon running right now?** Call `fused.daemon.status()` and read
`running`. That fact changes only through `start()`, `stop()`, `restart()`,
or heal-on-proxy (below) — never through `setAutostart()`.

**Will it come back at the next server launch?** Call `status()` and read
`autostart`. That fact changes ONLY through an explicit `setAutostart(bool)`
call (or the server-side `background_apps.set_autostart`) — never as a side
effect of `start()`, `stop()`, or `restart()`. It defaults to `false` for
every folder until something explicitly turns it on.

Given both facts, resurrection after the daemon stops running happens
through exactly one of these — there is no other path:

1. **Server start, but ONLY if autostart is on.** `_startup_resurrect_background_apps`
   (`server/app.py`) walks `autostart_paths()` and brings each one up. A
   folder that was only ever `start()`ed, with `setAutostart` never called,
   is NOT in that list and does not come back here — this is the opt-in
   default in action, not an omission.
2. **A page calling `start()` or `restart()`.** The documented, deliberate
   path — unconditional, but never itself sets autostart.
3. **Heal-on-proxy — the one that surprises people, and is entirely about run
   state, not autostart.** If the process was killed EXTERNALLY (a `kill`, a
   crash, a tray "Quit" that never talks to the server), the `Child` stays
   registered in `engine_host._children` — nothing ever popped it out. The
   next proxied call (`fused.daemon.call(...)`, `/api/engines/<id>/proxy/...`)
   finds a `Child` that doesn't answer and heals by respawning it
   (`engine_forward.py`). **`stop()` deliberately does not have this
   problem**: it pops the child out of `_children` (`engine_host.stop`)
   before killing it, so a proxied call after `stop()` finds nothing
   registered, returns 409, and does NOT revive it. A raw external kill
   skips that pop entirely — it is structurally the WEAKEST way to end a
   background app's process, weaker than `stop()`, because it leaves the one
   piece of bookkeeping that prevents an accidental revival intact. Anything
   that quits a background app from outside the server's own API (a tray
   menu doing `terminate:` directly, a shell `kill`, a crash) lands here, not
   on `stop()`'s clean path — see the OpenWhisper tray for the concrete case
   this bit.
4. **Nothing else.** No heartbeat, no polling, no periodic sweep — resurrection
   only ever happens as a *reaction* to one of the three triggers above, and
   trigger 1 only fires at all if autostart is on.

The practical consequence for anything that wants to let a user "quit and
stay off" from OUTSIDE a page (a tray icon, a CLI, a native menu item): call
`stop()` through the server's own API, never a raw process kill — otherwise
you've built path 3 by accident, and the app comes back the moment anything
pokes it. Whether it ALSO stays off at the next server launch depends
entirely on autostart, which `stop()` never touches — if the user wants
"stopped now AND never comes back automatically," that's `stop()` plus
confirming (or setting) autostart off, two separate calls for two separate
facts. `templates/shared/background_app.py` (above) is what a daemon uses to
do the run-state half of that about itself.

## Starting it — and opting into autostart — are the user's decisions, not yours

Opening a folder, or even the page loading and calling `fused.daemon.status()`, **never starts, stops, or persists anything**. `autostart_paths()` reads `<home_dir()>/background_apps.json` — the *only* thing "autostart" means — and the startup resurrection hook only resurrects folders already in that store. Your daemon comes up when something explicitly calls `POST /api/apps/background/start` (i.e. `fused.daemon.start()`), typically from a button the user clicks, never from page load. Do not call `start()` in an `onload`/init path — that would spawn a daemon on someone's machine the moment they open your folder, without them asking for it — and separately, do not call `setAutostart(true)` from page load either: that would make the daemon start surviving every future server launch, an even more surprising and more persistent version of the same mistake.

## Authoring conventions: being one of many apps on a user's machine

Everything above is mechanics — what the engine does. This section is what an app author has to get right so their app behaves as one of potentially many background apps sharing a user's machine, most of it a straight consequence of the mechanics already documented. None of this is enforced by a schema or a lint; the list at the end of this section says explicitly which parts the engine will not catch for you.

### Lead rule: never call `start()` (or `setAutostart(true)`) on page load

An app going from off to running, or from "one-off" to "survives every server restart," is the user's decision, made by clicking something. A page whose script calls `fused.daemon.start()` unconditionally takes that decision away from them the moment its JS runs — which is not only "the user opened my folder."

**This was a live hazard, not a theoretical one.** The home page's "Fused Apps" strip and the `/apps` hub render a *live picture* of each app, not a static screenshot, whenever the app has no authored `preview.png`: `AppPreviewCard` mounts the app's own `entry_html` in an iframe (`frontend/src/platform/ui/AppPreviewCard.tsx`, the `liveSrc`/`wantsLive` logic) once the card is near the viewport, and even a card that *does* have a `preview.png` swaps to the same live iframe on hover. That iframe's sandbox is `"allow-scripts allow-same-origin"` (`frontend/src/platform/lib/frame-focus.ts:180-181`, `THUMB_SEAL`) — scripts run. So an app that called `enable()` (the predecessor of `start()` + autostart, before D511) on load used to get it called every time its card scrolled into view or was hovered in a listing — someone merely *browsing* apps, never opening one, silently installed a resident daemon that survived the page, outlived the session, and came back at every server start.

This was a real bug: `Sina/OpenWhisper/index.html`'s menu-bar control used to call `fused.daemon.enable()` unconditionally on load. The daemon came back every time the page rendered — including in a listing preview — which made the tray's "Turn Off" item effectively unreachable: the user turned it off, the page (or a preview of it) rendered again, and it was back. D511 splits WHY that happened into two separate, individually-fixable facts (run state and autostart, previously fused into one "enabled" concept) — but the underlying rule this section states is unchanged: neither fact may ever change as a side effect of rendering.

**This is now enforced, not just documented (D507, SPEC.md §46).** The `_preview=1` flag reaches a card's live iframe reliably: `thumbFrame`/`withPreviewFlag` (`frontend/src/platform/lib/thumb-frame.ts`, `router.ts`) stamp it onto the `/render?path=...` URL that becomes the iframe's own `src`, and `fused_render/server/routers/render.py`'s `GET /render` serves the app's HTML at exactly that URL with no redirect — so the flag lands in the rendered page's own `location.search`, the same fact `runtime.js` already computed for the focus contract (`IS_THUMBNAIL`). `fused.daemon.start()`, `restart()`, `call()`, `stop()`, and `setAutostart()` now check it before doing anything: inside a preview thumbnail (this frame's own URL, or any same-origin ancestor's, carrying `_preview=1`), each rejects immediately with an `Error` naming the method and the rule — no POST is ever sent. See "How the guard rejects" below for the exact message. `status()` is the one method deliberately left ungated: it is read-only. `stop()` and `setAutostart()` are gated exactly like `start()`/`restart()`/`call()` (D508, 2026-08-26 code review) — leaving them open would let a preview do the one thing worse than starting a daemon: a card thumbnail mounts an app's own `entry_html` live with `allow-scripts`, so an app whose init path calls `setAutostart(true)` would persist a "come back forever" flag just because its card scrolled past, and unlike an unwanted `start()`, that survives a server restart.

```js
// Wrong: fires the instant this script runs, including inside a display-only
// preview iframe that scrolled into view. The user never clicked anything.
fused.daemon.start();
fused.daemon.setAutostart(true);

// Right: page load only reads and renders the current state. Starting the
// daemon, and opting into autostart, are each something a control does, in
// response to a click — two SEPARATE controls, since D511 made them two
// separate facts.
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

### What a page may do on load, and what it may not

| On load | Allowed | Why |
|---|---|---|
| `fused.daemon.status()` | Yes | Read-only; does not spawn, persist, or kill anything. |
| `fused.daemon.watch(callback)` | Yes — **but does one read and nothing more in a preview thumbnail** | `status()` underneath; never spawns, persists, or kills anything. In a live/hover preview iframe it does a single read and returns a no-op unsubscribe rather than leaving a poll loop or listeners running in a sandbox that gets mounted and unmounted on every hover. |
| Rendering the result | Yes | Pure display logic. |
| `fused.daemon.start()` | No — **and refused in a preview thumbnail** | Spawns a daemon — see above. Called inside a card's live/hover iframe, it now rejects instead of spawning. |
| `fused.daemon.restart()` | No — **and refused in a preview thumbnail** | Same spawn as `start()`; a page render is not a reason to bounce a daemon the user may be mid-use of. Same rejection as `start()` inside a preview. |
| `fused.daemon.stop()` | No — **and refused in a preview thumbnail** | Turning something the user has running *off* on a page render is exactly as surprising as turning it on — a load is not a "turn it off" click either. |
| `fused.daemon.setAutostart(bool)` | No — **and refused in a preview thumbnail** | Gated on the preview flag too (D508): a card thumbnail mounts an app's own `entry_html` live with `allow-scripts`, so an unguarded `setAutostart(true)` could persist a "come back forever" flag just because a card scrolled past — worse than an unwanted `start()`, since it survives a server restart. |
| `fused.daemon.call(path, body)` | Only if it's a genuine read — **and refused in a preview thumbnail** | `call()` proxies straight to the daemon's own HTTP surface (`engine_forward.py`); the runtime itself won't spawn on your behalf for it (client-side rejects if the app isn't known to be running — see `fused.daemon` above), but `engine_forward.py`'s heal-on-proxy path (`_forward`, lines ~216-222) *will* respawn a dead-but-running child on any proxied call, so a preview render that calls `call()` against an app started elsewhere can resurrect its daemon exactly like `start()` would — the guard covers it for that reason. Beyond that, your daemon might still treat a given route as "start work" server-side, and nothing stops a page from calling that route on load outside a preview. Treat routes that kick off work the same as `start()`/`restart()`: gate them behind an explicit control, not a load path.

### How the guard rejects

`start()`, `restart()`, `stop()`, `setAutostart()`, and `call()` check the preview flag *before* making any request — a rejection is a plain `Error`, never a silent no-op, and it names the method and the rule so the author sees it immediately in the console rather than wondering later why the daemon they turned off keeps coming back:

```
fused.daemon.start: refused — this page is rendering as a preview thumbnail
(a card peek or hover, not a real open), and a page must never start a
background daemon just by being displayed or hovered. Call
fused.daemon.status() on load to read state, and call start()/restart()
only from an explicit user action, e.g. a button's click handler.
```

This is a **client-side** guard (`fused_render/static/runtime.js`), not a server-side one, and that choice is deliberate: the flag reaches the app's own frame reliably (verified above — it's stamped directly onto the iframe's `src`, not merely inferred), so the check belongs where `start()`/`restart()`/`call()` themselves run, before any network request leaves the page. It protects against a *careless* app — the actual, verified hazard (OpenWhisper) — not a deliberately evasive one; nothing stops a page's own script from calling the underlying `fetch("/api/apps/background/start", ...)` directly and skipping `runtime.js` entirely, preview or not — that bypass exists independent of this guard, is not specific to preview rendering, and is out of scope for it (the iframe sandbox already grants arbitrary same-origin script execution; no guard on one function stops a page willing to route around it).

### Render both facts honestly — running AND autostart are separate (D511)

`fused.daemon.status()` reports **`running`** (a live child right now) and **`autostart`** (will the server bring it back at the next launch) as two independent booleans — not one collapsed three-state enum any more, though most UIs still want to SHOW three states because the interesting cases are the same as before:

- **Running** — the daemon is up right now, regardless of what `autostart` says.
- **Not running, but `autostart: true`** — "will come back" (at the next server launch, or on an explicit `start()`/`restart()`). Don't render this as broken or as an error; it's the ordinary state right after a `stop()` on an app that has autostart on, or after any external kill the heal-on-proxy path hasn't repaired yet.
- **Not running, `autostart: false`** — the ordinary default for anyone who hasn't opted the app into autostart, whether or not they've ever `start()`ed it. Don't render this as an error, a missing dependency, or a disabled/unavailable feature — it's a valid, common, and often the *most* common state a background app is in (it's the default!). `Sina/OpenWhisper/index.html`'s `mbRender`/`refreshMenubar` (its "Menu bar active…" / "…stopped (starts automatically at next launch)" / "…off — click to turn on" labels, `.on`/`.pending` CSS classes, and a SEPARATE "Start automatically" checkbox in Settings for the autostart fact) is a worked example of rendering both facts — read it for the pattern, not as a finished reference.

### Reflect state that changed for a reason outside this page — `fused.daemon.watch()`

`status()` alone only tells you what's true right now, at the moment you called it. A page that calls `status()` once on load and otherwise only refreshes after its own `start()`/`stop()`/`restart()` calls has a silent blind spot: **the daemon's state can change for reasons that have nothing to do with this page** — the OpenWhisper tray's "Quit" (a native menu item, not a page action) routes through the exact same `POST /api/apps/background/stop` the page's own `stop()` button does, so the server knows the daemon died, but a page that only reflects its own actions never finds out. Its mic icon stayed "on" until the user manually reloaded — a real bug, not a hypothetical one.

`fused.daemon.watch(callback)` closes that gap: it calls `callback(status)` on the initial read and again whenever `{running, autostart, pid, version}` changes, polling `status()` only while the tab is visible and refreshing immediately on `visibilitychange`→visible and window `focus` (the case that matters most — reaching for a tray or another window means this page was *not* focused when the state changed). Use it instead of hand-rolling your own poll loop, and use it as your app's ONE source of truth for `running`/`autostart` rather than tracking a local boolean your own button clicks flip:

```js
const unsubscribe = fused.daemon.watch((s) => {
  mbRunning = s.running;
  mbAutostart = s.autostart;
  refreshMenubar();  // re-render from the two facts, same as after your own start()/stop()
});
```

The general convention this establishes: **an app should render from the server's actual state, not from an assumption that its own actions are the only source of change.** A button's own click handler updating local state directly (rather than waiting for the next `watch()` tick or re-deriving from a fresh `status()`) is fine for the calling page's own immediate feedback — but anything a *different* surface (a tray, another tab, the server's own startup resurrection) can also change should be read back from the server, not assumed static between polls.

### Give the user a way to turn the app off from inside the app itself

A background app that runs native desktop UI (a tray icon, a menu-bar item) needs its own off switch there too, not only on the page — a user who never has the page open still needs a way to quit it. Two things follow directly from the run-state/autostart split above:

- A tray "Quit" item only needs to call `stop()` — it should NOT also try to turn autostart off, because "quit right now" and "never come back automatically" are different user intents (D511's whole point) and conflating them back together in the tray just re-creates the bug this decision fixed. Offer autostart as its own separate control (a checkmark item reflecting `status().autostart`, toggling it via `set_autostart()`) if you want the user to control it from the tray at all — see `menubar.py`'s `toggleAutostart_` for the shipped pattern.
- Both controls must go through the server's own API — `fused.daemon.stop()`/`setAutostart()` from the page, or `background_app.stop()`/`set_autostart()` (`fused_render/templates/shared/background_app.py`) from inside the daemon itself — never a raw process kill (`terminate()`, `sys.exit()`, a bare `kill`) for the "stop it" half. A raw kill is the *weakest* way to end a background app: it skips the `_children` bookkeeping `stop()` pops, so the next proxied call heals the daemon back to life (resurrection path 3 in "When does it come back?" above). This is the exact mechanism behind the OpenWhisper tray bug this skill section exists for.

### The daemon is a guest process: assume nothing about its own continuity

Three things follow from facts already established elsewhere in this skill, restated here as author obligations:

- **It may be killed at any time**, by the server's own SIGTERM→SIGKILL tree-kill (`stop()`, a respawn) or by anything external (a crash, a manual `kill`, an OS reboot). Never assume a clean shutdown path runs — persist anything that must survive a restart (`background_apps.cache_dir_for(engine_id)`, never beside the user's code) with writes that are safe to interrupt mid-flight (write-to-temp-then-`os.replace()`, the same pattern the daemon's own status-file publish uses — see "The daemon contract" above).
- **It is not a singleton across restarts.** Nothing replays state into a fresh child for you (see "Does `reinit()` replay apply to a background app? No" above) — a respawned daemon starts as a brand-new process with only what it reads back from its own persisted state or rebuilds from scratch. Design `main()` for "I might be the fifth process this folder has run today" rather than "I am the one true instance."
- **It must tolerate its own work being interrupted mid-request.** A client-facing `call()` can be answered by a daemon that gets killed moments later; don't leave on-disk state half-written or a resource half-acquired across that boundary. This is the same interruption tolerance the venv-precondition and heal-on-proxy paths already assume of every background app, just stated as a requirement rather than a fact about the engine.

### What the version-digest machinery expects from you

`version_for` (`fused_render/background_apps.py:116-163`) hashes your `pyproject.toml`'s bytes, your `daemon.py`'s mtime+size, and the interpreter's own path+mtime+size into the `--version` string your daemon is spawned with — and `ensure_background`'s reuse check retires and respawns a running child the moment that digest changes. Two consequences:

- **You never bump a version yourself.** Editing `pyproject.toml` or `daemon.py` during development is enough — the next `start()`/`restart()`/reuse check sees a new digest and starts a fresh child; there's no manual step and no version field to remember to touch.
- **Never hardcode your `/ping` response's `version` to a fixed string.** Echo back exactly the `--version` value you were spawned with (per "The daemon contract," step 5, above). Hardcoding it defeats the staleness check silently: `ensure_background` would see a `/ping` that always "matches," reuse the stale child forever, and the respawn-on-edit behavior the rest of this section relies on would stop working for your app specifically.

### Conventions the engine does not enforce

Nothing below is checked by any code path — a careless app can still do the wrong thing, and no test or manifest rule will catch it. This is a deliberate list, not an oversight: enforcing any of these is a separate decision for the user to make, not something this documentation pass adds code for. (`start()`/`restart()`/`call()`/`stop()`/`setAutostart()` on page load used to be partly on this list — it's enforced now; see "How the guard rejects" above.)

- Rendering both `status()` facts (`running`, `autostart`) honestly, and specifically not presenting `autostart: false` as an error or a missing feature — purely a UI choice.
- Exposing an in-app off switch (tray/menu-bar or otherwise) at all — nothing requires a background app with native UI to offer one.
- Routing that off switch through `stop()` (or `background_app.py`'s equivalent) instead of a raw process kill — the heal-on-proxy resurrection path exists precisely because the engine tolerates a raw kill; it does not forbid one.
- The daemon persisting its own state safely across an unannounced kill, and not assuming it is the only instance a folder has ever spawned — both are properties of how the author writes `main()`, invisible to `background_apps.py`.
- Echoing the real `--version` value from `/ping` rather than a hardcoded string — an author can defeat the staleness check and nothing will complain; the daemon just quietly stops picking up its own updates.

## The venv precondition, its 409, and where the daemon actually runs

`start()` (and `restart()`'s ensure-background fallback) return **409** when the folder's own project venv isn't built yet:

```json
{"error": "<folder> needs its project environment built before its background app can start; open it once (or call fused.runPython) to install it, then retry.", "status": 409}
```

Building a venv can take minutes, and this endpoint won't do it inside a POST — open the page once, or call `fused.runPython`, to trigger the build, then retry `start()`. This is the identical stance `/api/engine`'s warm-worker dispatch already takes; see `fused-render-authoring` for the general venv-precondition rule and what "open it once" builds.

**The ordering trap specific to background apps (D503):** the daemon runs on **the folder's own declared environment**, resolved from the folder itself — never from an ancestor project. `background_apps.interpreter_for` calls `projectenv.has_project_env(folder)` on the app's own folder only; it does **not** walk upward the way a plain `.py` script's environment resolution does. If your folder declares no `[project]` deps of its own, your daemon runs on `sys.executable` (this app's own bundled interpreter) regardless of what any parent directory declares — `import fused_render` is **not** available there. And your folder's `[project]` dependency list (in its own `pyproject.toml`) is the **complete** list your daemon gets; nothing from this app's own bundled dependency set is unioned in. Declare everything your `daemon.py` imports, stdlib aside.

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
- Expecting `fused.daemon.start()` from page load to be harmless → it spawns a daemon the user never asked for, and a display-only preview iframe (Home's app strip, the `/apps` hub) runs that same load path. Gate it behind an explicit user action; the runtime now refuses `start()`/`restart()`/`call()`/`stop()`/`setAutostart()` inside a preview thumbnail and throws a named error if you don't — see "How the guard rejects" above.
- Assuming `start()` (or `stop()`) also changes whether the app comes back at the next server launch → it doesn't, ever (D511). Only `setAutostart(bool)` touches that flag; a test (`test_api_start_calls_ensure_background_without_touching_autostart`) fails if `start()` regresses this.
- Calling `setAutostart(true)` from page load, or as a side effect of a "start it" click → it persists a flag surviving a server restart; gate it behind its OWN explicit control, separate from the run-state switch.
- Ending a background app from a tray/menu-bar control with a raw process kill instead of `stop()` → the weakest way to quit it; skips the bookkeeping that keeps it from silently reviving on the next proxied call.
- Rendering `autostart: true, running: false` or `autostart: false` as an error/broken state instead of an ordinary one → both are expected, common states, not failures — `autostart: false` is in fact the default for every folder.
- Assuming a tray "Quit" should also turn autostart off (or that autostart on means "running") → they're independent facts now; conflating them back together re-creates the exact bug D511 exists to fix.
- Assuming `import fused_ai` (or any `templates/shared` helper) works in a daemon because it works in `/api/run` → it doesn't; `_child.py`'s `sys.path` seeding never runs for a background daemon spawn. Copy the module in.
- Expecting a daemon's descriptors to survive a restart the way a template's do → `reinit()`/`_replay` is never invoked for background apps; your daemon must rebuild or persist its own state.
- Declaring a dependency only in this app's own environment and expecting it to reach the daemon → the folder's own `[project]` table is the complete dependency list; nothing bundled is unioned in.
- Placing `daemon` outside the folder (`../daemon.py`) or naming a directory (`daemon = "."`) → both are silently rejected by `load_manifest`; the manifest reads as absent with no error surfaced to the page.
- Calling `start()` before the folder's venv is built → 409; open the page once (or `fused.runPython`) first, per the same stance `/api/engine` already takes.
- Reaching for a background app when the warm `/api/engine` worker's 15-minute idle-retire was never actually the problem → the manifest, start step, and daemon HTTP contract are all overhead a zero-config warm worker doesn't need.
- Writing a fourth tray/menu-bar implementation instead of reading `supervisor/tray.py` (+ its `_win32`/`_linux` backends) or `menubar_pin.py` first.
- Touching AppKit state from your daemon's HTTP handler thread on macOS → `NSApplication.run()` requires the main thread; hop with `PyObjCTools.AppHelper.callAfter`.

## When to switch skills

- Writing or debugging the page's own `.html`/`.py` — `fused.runPython`, params, file IO, the venv-build precondition in general → **`fused-render-authoring`**.
- The warm, zero-config `/api/engine` worker (persistent interpreter, 15-minute idle-retire, no manifest) instead of a resident daemon → **`fused-render-authoring`**'s "Long-running work" section.
- Reading or querying the machine-wide file index from your daemon or page → **`fused-render-index`** (its "copy the reader, don't import fused_render" pattern is the same one this skill points you at for `templates/shared`).
- Calling AI models, local or hosted, from a page or from Python → **`fused-render-ai`**.
- Registering a preview template for a file extension (a different, page-rendering concept from a background daemon) → **`fused-render-custom-templates`**.
- Just opening/running the app or a view → **`fused-render-usage`**.
