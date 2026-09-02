---
name: fused-render-background-apps
description: Use when an app folder needs Python that keeps running after its page closes — a warm worker or resident daemon, [tool.fused-render.app], fused.daemon.*, autostart, or a worker "dying" after 15 min idle.
---

# Background apps

Three ways a folder's Python runs:

| Path | Lifetime | State survives? | Idle-reaped? |
|---|---|---|---|
| `/api/run` (`fused.runPython`) | fresh subprocess, 60 s kill | no | n/a |
| `main = "x.py"` | shipped warm worker, plain `main(**params)` | yes | after `idle_timeout_s` (default 900 s) |
| `daemon = "x.py"` | your own HTTP daemon | yes | no (default) |

Pick `daemon =` only when idle-reap is the actual problem (websocket/poll server, held device/socket, tray UI). State between calls minutes apart = `main =`, no daemon script needed.

## Manifest

In the folder's `pyproject.toml`: `[tool.fused-render.app]` with EXACTLY ONE of `daemon =` / `main =` (path inside the folder, a file, not a dir). Optional `idle_timeout_s` (0 = resident). Bad manifests are **silently** rejected (read as "no manifest") — `background_apps.load_manifest` lists the rules; check `GET /api/apps/background/status?html=<page>` if unsure yours parsed.

## Daemon contract

Plain script run as `[interpreter, daemon.py, --status <path>, --cache <dir>, --version <str>]`. Read `tests/fixtures/background_app/daemon.py` (minimal stdlib reference) before writing one. The sequence that has no race: parse args → bind `127.0.0.1:0` → atomically publish `{port, token, pid}` to the status file (temp + `os.replace`, BEFORE `serve_forever`) → serve. Require a random token on every request (parent proxies via `/api/engines/<id>/proxy/`, but any local process can hit your port). Answer `GET /ping` with `{"ok": true, "version": <the --version you were GIVEN — never hardcode, it defeats the staleness respawn>}` and `GET /quit` by `shutdown()` on a separate thread (same-thread deadlocks ThreadingHTTPServer).

Guest-process rules: may be killed anytime (persist under `--cache` dir with temp+`os.replace`); respawns start empty — `reinit()` replay does NOT apply to background apps; tolerate mid-request death.

## Environment (differs from /api/run)

- Runs on the folder's OWN declared env, no ancestor walk, no bundled union — the `[project]` list is complete. No project deps → `sys.executable`; `import fused_render` never available.
- `templates/shared` is NOT on sys.path → `import fused_ai` / `import background_app` fail. Bootstrap: read `<FUSED_RENDER_HOME_DIR or ~/.fused-render>/server.json`, `sys.path.insert(0, info["shared"])`. Shipping example: `_bootstrap_background_app` in the OpenWhisper tray's `menubar.py`.
- `background_app` module (from shared): `status()`, `stop()`, `restart()`, `set_autostart(bool)` — the daemon's own way to control itself via the server.
- Server-startup autostart skips folders whose venv isn't built (open the page once first); `start()` falls back to `sys.executable` instead.

## Page side: `fused.daemon`

`start()`, `stop()`, `restart()`, `status()` → `{running, autostart, pid, version, engine_id, protocol}`, `watch(cb)` (fires on real changes — reflects tray quits etc.), `setAutostart(bool)`, `call(path, body)` (daemon protocol, POST proxied), `run(params)` (main protocol, `/call` envelope unwrapped). `call`/`run` auto-bring-up the daemon — `start()` not required first. Wrong-protocol calls reject with "use the other one".

**Two independent axes** — conflating them is THE bug here:

- Run state: `start/stop/restart` only. Never touches autostart.
- Autostart (come back at server launch): `setAutostart` only. Never starts/stops anything. Opt-in, default false — not an error state.

A stopped daemon comes back via exactly: (1) server start IF autostart on, (2) explicit `start()`/`restart()`, (3) **heal-on-proxy** — an EXTERNAL kill leaves the Child registered, so the next proxied `call()`/`run()` respawns it. `stop()` unregisters first, so it doesn't heal back. Therefore any outside-the-page quit (tray, CLI) must go through `stop()`/`background_app.stop()`, never a raw kill. Tray "Quit" = `stop()` only; autostart gets its own checkbox.

**Never `start()`/`setAutostart(true)`/`call()` on page load.** App cards render live iframes stamped `_preview=1`; the runtime refuses all mutating daemon methods in a preview frame with a named error (`status()` allowed; `watch()` does one read there). Mutations belong in click handlers. Render both facts (`running`, `autostart`) honestly — three states, none of them errors.

## Native/tray work

Don't write a fourth tray backend: `fused_render/supervisor/tray.py` is the seam (win32 pystray / linux D-Bus backends beside it); macOS menubar = `fused_render/menubar_pin.py`. On macOS all AppKit calls are main-thread only — hop from handler threads with `PyObjCTools.AppHelper.callAfter`.

## Pitfalls

- Status file published after `serve_forever` → bootstrap poll races. Publish after bind.
- `server.shutdown()` from its own handler thread → deadlock.
- Hardcoded `/ping` version → stale daemon kept forever.
- Raw kill from a tray → heal-on-proxy revives it.
- Expecting `import fused_ai` / bundled deps / reinit replay to work like `/api/run` — none do.
- `daemon = "."`, paths outside the folder, both/neither protocol keys → silent manifest rejection.

Related: page authoring + preview gating → `fused-render-authoring`; timeout strategies + job rows → `fused-render-jobs`; reading the index from a daemon → `fused-render-index`.
