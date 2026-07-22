# Desktop supervisor architecture

The **supervisor** is the windowless process that owns a FusedRender desktop
install: it elects a single instance, launches and supervises the Python
server as a child, shows the tray, forwards "open this file" requests from
secondary launches, and tears everything down cleanly (including on
upgrade). It lives in `fused_render/supervisor/`.

This replaces the experiment framing of
[`PYTHON_SUPERVISOR_SPEC.md`](PYTHON_SUPERVISOR_SPEC.md) (kept for history): the
pure-Python supervisor shipped, and the code is now organized as a
platform-neutral core plus a per-OS backend, so a new operating system is a new
*backend*, not a fourth reimplementation of the run loop.

## Module layout

```
fused_render/
  desktop_probe.py            # token-verified readiness probe (all platforms)
  _view_url_codec.py          # fs-path -> /view URL codec (all platforms + frontend)
  paths.py                    # server-side desktop_instance() (id/token echo)
  app.py                      # macOS menu-bar app (its own in-process design; see below)
  supervisor/
    __main__.py               # entry: env-before-import, arg parse, fatal reporting
    core.py                   # the run loop / event loop / teardown (platform-neutral)
    protocol.py               # IPC wire frames (byte-stable)
    tray.py                   # pystray tray loop (portable)
    paths.py                  # DesktopPaths + child/self environment contract
    _backend.py               # sys.platform dispatch -> the one live backend
    _win32/                   # Windows backend
      job.py                  # Job Object process-tree kill
      instance.py             # mutex + named-pipe single instance & IPC
      startup.py              # HKCU Run-key autostart toggle
      ui.py                   # MessageBox / file dialog / os.startfile shell opens
```

`core.py` and `__main__.py` reach every OS-specific capability **only** through
`_backend`. `_backend` dispatches on `sys.platform`, imports the matching
backend package, and re-exports its namespace; a platform with no backend
raises a clear `RuntimeError` at import. Because exactly one backend is ever
live per process, this is a module namespace, not an ABC — no runtime
polymorphism, no interface class standing in front of a single implementation.

## The backend seam contract

A backend must provide all of these names (see `_backend.__all__`):

| Name | What it must guarantee |
| --- | --- |
| `Job` | A supervised process group. `Job().spawn(python, args, environment=, output=)` starts the server as a child; `.close()` **kills the whole tree** — no orphaned render subprocesses survive the supervisor, even on hard exit. |
| `instance` | Single-instance election + IPC. `InstanceNames.current_user()`, `acquire(names)` → `PrimaryInstance` or `SecondaryInstance`; a secondary can `send(command, timeout)` a `protocol.Command` to the primary and `wait_for_exit(...)`; a primary can `serve(requests, log)` and `stop_serving()`. Names must stay stable across builds (see invariants). |
| `startup` | Autostart toggle: `enabled() -> bool`, `set_enabled(bool)`. Failure is non-fatal (logged; the tray checkbox simply reflects the real state). |
| `ui` | Native dialogs + shell opens: `alert(message)`, `confirm_exit() -> bool`, `report_open_rejected(path)`, `pick_file() -> str | None`, `open_path(path)`, `open_uri(uri)`, `open_url(url)`. `alert` must be usable for fatal reporting even when the rest of the backend failed to import (on Windows this is why `ui`'s heavy imports are lazy). |
| `SPAWN_ERRORS` | Tuple of extra exception types `Job.spawn` may raise beyond the stdlib `OSError`/`RuntimeError`/`TimeoutError` the run loop already handles (Windows: `pywintypes.error`; a stdlib-only backend: `()`). |

Readiness and shutdown are **not** in the backend — they are cross-platform and
live in `desktop_probe.py` + the server's `/api/config` and
`/api/desktop/shutdown`. Each launch publishes a per-launch instance id + 256-bit
token into the child/in-process server's environment (`paths.child_environment`
on Windows, `app.configure_desktop_instance` on macOS); the server echoes them
from `/api/config` and gates `/api/desktop/shutdown` on the token. The probe
polls until the echoed id + token match, so a decoy server holding the port
cannot satisfy startup.

## Platform matrix

| Capability | Windows (`_win32`) | macOS (`app.py`) | Linux (planned `_linux`) |
| --- | --- | --- | --- |
| Process model | out-of-process supervisor + Job-owned child server | in-process server thread under the rumps app | out-of-process supervisor + child server |
| Single instance | named mutex `FusedRender.Supervisor.v1.<sid>` | pidfile + `/` probe (`find_running_server`) | TBD (e.g. abstract-socket / lock file) |
| IPC (forwarded opens) | named pipe (same base name), `protocol` frames | AppKit `openFiles:` / `openURLs:` delegates | TBD |
| No-orphans tree-kill | Job Object | *not guaranteed* (deferred; see below) | cgroup / process group |
| Tray | `pystray` (`supervisor/tray.py`) | `rumps` status item + pinned-view popover | `pystray` (shared) |
| Autostart | HKCU `Run` key | not wired | XDG autostart `.desktop` |
| Paths root | `%LOCALAPPDATA%\FusedRender\Desktop` | `~/Library/Application Support/fused-render` | XDG base dirs |
| `/view` URL codec | shared `_view_url_codec` | shared `_view_url_codec` | shared |
| Token readiness / shutdown | shared `desktop_probe` + `/api/*` | shared `desktop_probe` + `/api/*` | shared |

## Invariants (upgrade compatibility)

The only hard backward-compatibility constraint is narrow: **a newly-installed
supervisor must be able to shut down an already-installed older one during an
upgrade.** That requires two things to stay stable:

- the **pipe/mutex base name** `FusedRender.Supervisor.v1.<sid>`, and
- the **`protocol` wire frames** a secondary sends (magic/version/opcode
  layout) must keep decoding.

Everything else — internal function shapes, module boundaries, the run-loop
structure — is free to change. The `__main__` **env-before-import** ordering is
also load-bearing (the desktop env, including the branch opt-out, must be
applied before anything under `fused_render` imports and caches a branch ref);
`tests/test_supervisor_env_ordering.py` exists precisely to guard it.

## Deliberately unshared: the macOS app architecture

macOS keeps its own in-process design in `app.py` (the rumps app runs the
uvicorn server on a daemon thread inside the same process) rather than adopting
the out-of-process supervisor. The one-line reason: **rumps needs to own the
AppKit main thread**, so the menu-bar app cannot also be a headless child of a
separate supervisor process without a larger rework. macOS has converged on the
genuinely shared pieces — the `_view_url_codec` and the `desktop_probe`
token-verified readiness/shutdown contract — but full supervisor convergence
(which would also give macOS the Job-Object-style no-orphans guarantee it
currently lacks) is **deferred until the Linux backend proves the seam** with a
second out-of-process implementation.
