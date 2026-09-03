---
name: fused-render-background-apps
description: Use when app folder needs Python alive after page closes — warm worker, resident daemon, [tool.fused-render.app], fused.daemon, autostart, worker "dying" after idle.
---

# Background apps

Three ways folder's Python runs:

| Path | Lifetime | State survives? | Idle-reaped? |
|---|---|---|---|
| `/api/run` (`fused.runPython`) | fresh subprocess, 60 s kill | no | n/a |
| `main = "x.py"` | shipped warm worker, plain `main(**params)` | yes | after `idle_timeout_s` (default 900 s) |
| `daemon = "x.py"` | own HTTP daemon | yes | no by default; set `idle_timeout_s` > 0 to opt in |

Pick `daemon =` only when idle-reap is the actual problem (websocket/poll server, held device/socket, tray UI). State between calls minutes apart = `main =`, no daemon script.

## Manifest

Folder's `pyproject.toml`: `[tool.fused-render.app]` with EXACTLY ONE of `daemon =` / `main =` (path inside folder, file not dir). Optional `idle_timeout_s` (0 = resident). Bad manifest **silently** rejected (read as "no manifest") — rules in `background_apps.load_manifest`; check `GET /api/apps/background/status?html=<page>` if unsure yours parsed.

## Daemon contract

Plain script run as `[interpreter, daemon.py, --status <path>, --cache <dir>, --version <str>]`. Read `tests/fixtures/background_app/daemon.py` (minimal stdlib reference) before writing one. Race-free sequence: parse args → bind `127.0.0.1:0` → atomically publish `{port, token, pid}` to status file (temp + `os.replace`, BEFORE `serve_forever`) → serve. Require random token on every request (parent proxies via `/api/engines/<id>/proxy/`, but any local process can hit port). `GET /ping` → `{"ok": true, "version": <the --version you were GIVEN — never hardcode, defeats staleness respawn>}`. `GET /quit` → `shutdown()` on separate thread (same-thread deadlocks ThreadingHTTPServer).

Guest-process rules: may die anytime (persist under `--cache` with temp+`os.replace`); respawns start empty — `reinit()` replay does NOT apply here; tolerate mid-request death.

## Environment (differs from /api/run)

- Runs folder's OWN declared env, no ancestor walk, no bundled union — `[project]` list complete. No project deps → `sys.executable`; `import fused_render` never available.
- `templates/shared` NOT on sys.path → `import fused_ai` / `import background_app` fail. Bootstrap: read `<FUSED_RENDER_HOME_DIR or ~/.fused-render>/server.json`, `sys.path.insert(0, info["shared"])`.
- `background_app` module (from shared): `status()`, `stop()`, `restart()`, `set_autostart(bool)` — daemon's own control channel. Needs `FUSED_RENDER_APP_DIR` env (set for managed daemons); raises `NotUnderEngine`, `ServerNotRunning`, `BackgroundAppError` — catch them.
- Server-startup autostart skips folders whose venv isn't built (open page once first); `start()` falls back to `sys.executable`.

## Page side: `fused.daemon`

`start()`, `stop()`, `restart()`, `status()` → `{running, autostart, pid, version, engine_id, protocol}`, `watch(cb)` (fires on real changes — tray quits etc.), `setAutostart(bool)`, `call(path, body)` (daemon protocol, POST proxied), `run(params)` (main protocol, `/call` envelope unwrapped). `call`/`run` auto-bring-up daemon — no `start()` needed first. Wrong-protocol call rejects with "use the other one".

**Two independent axes** — conflating them = THE bug here:

- Run state: `start/stop/restart` only. Never touches autostart.
- Autostart (come back at server launch): `setAutostart` only. Never starts/stops anything. Opt-in, default false — not error state.

Stopped daemon comes back via exactly: (1) server start IF autostart on, (2) explicit `start()`/`restart()`, (3) **heal-on-proxy** — EXTERNAL kill leaves Child registered, next proxied `call()`/`run()` respawns. `stop()` unregisters first, doesn't heal back. So any outside-the-page quit (tray, CLI) must go through `stop()`/`background_app.stop()`, never raw kill. Tray "Quit" = `stop()` only; autostart gets own checkbox.

**Never `start()`/`setAutostart(true)`/`call()` on page load.** App cards render live iframes stamped `_preview=1`; runtime refuses all mutating daemon methods in preview frame with named error (`status()` allowed; `watch()` one read there). Mutations belong in click handlers. Render both facts (`running`, `autostart`) honestly — three states, none errors.

## Native/tray work

Don't write fourth tray backend: `fused_render/supervisor/tray.py` = the seam (win32 pystray / linux D-Bus backends beside it); macOS menubar = `fused_render/menubar_pin.py`. macOS AppKit = main-thread only — hop from handler threads with `PyObjCTools.AppHelper.callAfter`.

## Pitfalls

- Status file published after `serve_forever` → bootstrap poll races. Publish after bind.
- `server.shutdown()` from own handler thread → deadlock.
- Hardcoded `/ping` version → stale daemon kept forever.
- Raw kill from tray → heal-on-proxy revives it.
- Expecting `import fused_ai` / bundled deps / reinit replay like `/api/run` — none work.
- `daemon = "."`, paths outside folder, both/neither protocol keys → silent manifest rejection.

Related: page authoring + preview gating → `fused-render-authoring`; timeout strategies + job rows → `fused-render-jobs`; reading index from daemon → `fused-render-index`.
