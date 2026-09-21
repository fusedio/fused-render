# Status-bar terminal

## Goal

Add a real shell terminal to the app, reached from a status-bar chip, with VS Code's
shell-resolution semantics (profiles: path + args + env, `$SHELL` on unix, login shell
on macOS).

Approach: the server owns a pty session registry keyed by session id; a WebSocket
streams bytes both ways and replays scrollback on reattach, so a page reload rejoins the
same shell. The frontend is xterm.js inside a **resizable drawer that reserves height in
`#main`**, not a `.dl-panel` — the status-bar chip only toggles it. macOS + Linux only
this round.

## Tasks

### 1. Shell profile resolution (server, pure)

**Create `fused_render/terminal_profiles.py`.** One function returning the resolved
profile — executable path, argv, env overlay, cwd — for the current platform. Port the
rules already proven in `fused_render/claude_health.py`: `$SHELL` or `/bin/bash`
(:281), `executable()` checks, and **scrub `PYTHONHOME`/`PYTHONPATH`** from the child
env (same reason as :283 — the bundled interpreter exports both and a non-Python child
dies with "No module named 'encodings'"). Add `-l` on darwin only (macOS GUI apps
inherit launchd's env, not the profile's — this is exactly what VS Code does and what
`claude_health._shell_rc` at :547 already documents for bash/zsh). Set `TERM=xterm-256color`
and `COLORTERM=truecolor`; drop `FUSED_RENDER_ORIGIN` passthrough decisions to the
default (inherit). No settings file, no profile *list* — one resolved profile. Return
`None` on `os.name == "nt"`.

**Create `tests/test_terminal_profiles.py`.** Test-first. Monkeypatch `SHELL`,
`sys.platform` and `os.name`: unset `SHELL` → `/bin/bash`; darwin → argv carries `-l`,
linux → it does not; `PYTHONHOME`/`PYTHONPATH` present in `os.environ` → absent from the
returned env; `nt` → `None`. Follow the parametrised style in
`tests/test_claude_agent_windows.py`.

Verify: `pytest tests/test_terminal_profiles.py -q` — all pass.
Commit: `Terminal: resolve the user's shell into a spawn profile`

### 2. Pty session registry + fork-safe exec helper (server)

**Create `fused_render/_pty_exec_helper.py`.** A bare-python script, no
`fused_render` imports, run as the Popen target: it inherits the pty slave on fd 0/1/2,
calls `os.setsid()`, `fcntl.ioctl(0, termios.TIOCSCTTY, 0)`, `os.chdir(cwd)`, then
`os.execv(shell, argv)`. Sibling of `fused_render/_env_install_worker.py` — same "helper
script run in a clean interpreter" shape.

**Create `fused_render/pty_session.py`.** The registry. Per session: `os.openpty()` **in
the server process** (openpty does not fork, so it is safe here), then
`subprocess.Popen([app_interpreter, helper, ...])` with the slave fd as
stdin/stdout/stderr and `**SUBPROCESS_KWARGS`-equivalent kwargs — `close_fds=False`, **no
`cwd=`, no `start_new_session=True`, and an absolute interpreter path**. That combination
is what keeps this Popen on the `posix_spawn` path; the fork path runs PROJ's SQLite
atfork handler and the server dies with SIGSEGV. `fused_render/claude_spawn.py:129-134`
states this rule verbatim for the identical situation — read that docstring before
writing the Popen, and mirror its comment here. cwd and setsid move into the helper
precisely because `cwd=`/`start_new_session=True` would force the fork.

Also here: a bounded scrollback ring (cap ~256KB of raw bytes, enough to repaint a
screen on reattach), `write()`, `resize(rows, cols)` via `TIOCSWINSZ` on the **master**
fd (an ioctl, no fork), `kill()` (SIGHUP to the process group then SIGKILL after a
grace), a reaper for exited children, and a hard cap of 8 live sessions. A reader thread
per session drains the master fd into the ring and fans out to subscribers — the
one-ticker-many-subscribers shape `_WATCH_REGISTRY` uses in
`fused_render/server/routers/fs_read.py` (see its header comment).

**Create `tests/test_pty_session.py`.** Test-first, `skipif` on Windows. Spawn
`/bin/sh -c 'echo hi'`-shaped sessions: output reaches the ring; `write("exit\n")` ends
the session and the reaper marks it dead; scrollback is capped at the byte limit and
keeps the *tail*; `resize` is reflected by a session running `stty size`; the cap refuses
a 9th session. Assert the Popen kwargs explicitly (`close_fds is False`, `cwd` absent,
`start_new_session` absent) — that assertion is the regression guard for the SIGSEGV,
and a runtime crash would not otherwise be attributable.

Verify: `pytest tests/test_pty_session.py -q` — all pass.
Commit: `Terminal: pty session registry with a fork-safe exec helper`

### 3. WebSocket + REST routes (server)

**Create `fused_render/server/routers/terminal.py`.** `POST /api/terminal` creates a
session (body: optional `cwd`) and returns its id; `GET /api/terminal` lists live
sessions; `DELETE /api/terminal/{sid}` kills one; `WS /api/terminal/{sid}/stream`
attaches. On attach: send the scrollback, then stream. Client→server frames are JSON
control messages (`{"resize": [rows, cols]}`) and binary frames for keystrokes;
server→client is binary for output plus a JSON `{"exit": code}` on death. Follow
`fs_read.py:655` for the accept/pump/drain-to-detect-disconnect shape and its 15s
keepalive. On Windows every route returns 501 with a plain reason (see Decisions).

**Register it** in `fused_render/server/app.py` beside the other routers (the
`include_router` block, :701-840), following `capture_router` at :748.

**Add the LAN decision explicitly**: do NOT add the route to `LanApp`'s forward
allowlist in `fused_render/lan.py:630-647`. The WS branch there allowlists exactly
`/api/fs/events`; leaving terminal out means a paired LAN peer gets a 1008 close, which
is the behaviour we want. Add a one-line comment there saying so, so the next person
adding a socket does not assume the omission was an oversight.

**Create `tests/test_terminal_routes.py`.** Test-first, following
`tests/test_server_fs_events.py` for `TestClient.websocket_connect`. Cover: create →
attach → type `echo hi` → the bytes come back; detach and re-attach → scrollback
replays; resize control frame reaches the session; delete kills it; a second attach to
the same id gets the same shell; unknown id → close with 1008.

Verify: `pytest tests/test_terminal_routes.py -q` — all pass.
Commit: `Terminal: /api/terminal session routes and byte stream`

### 4. Terminal session client + view (frontend platform)

**Create `frontend/src/platform/lib/terminalSession.ts`.** Everything testable without a
DOM: build the WS URL, frame encode/decode (binary vs JSON control), the create/attach
handshake, and reconnect-with-backoff. `frontend/src/apps/explorer/listing/useDirListing.ts:104-170`
is the WS pattern to follow, including its note at :164 that WebSockets do not
auto-reconnect the way EventSource did — this one must, since a dev-server restart
otherwise leaves a dead terminal.

**Create `frontend/src/platform/lib/terminalSession.test.ts`.** Test-first against a
fake WebSocket: URL shape, resize encoding, output decode, reconnect backoff sequence,
and that an `{"exit": code}` frame stops reconnection.

**Add `@xterm/xterm` + `@xterm/addon-fit` to `frontend/package.json` dependencies**
(`bun add`). npm deps, not vendored: the vendor precedent
(`fused_render/templates/vendor/codemirror.bundle.js`) exists because *user templates*
load it from a served path with no bundler; the shell has vite and every other shell dep
is an npm package.

**Create `frontend/src/platform/ui/TerminalView.tsx`.** Thin: owns one xterm instance +
FitAddon, pipes it to a `terminalSession`, calls `fit()` on a ResizeObserver, disposes on
unmount. Deliberately has no unit test — a headless renderer cannot measure a canvas, and
a test over this file would assert only that we called the library (see Decisions).

Verify: `cd frontend && bun test src/platform/lib/terminalSession.test.ts` — all pass;
`bun run typecheck` clean.
Commit: `Terminal: xterm client and session protocol`

### 5. Status-bar chip + resizable drawer (frontend shell)

**Create `frontend/src/shell/TerminalDrawer.tsx`.** A `<div class="term-drawer">` with a
drag handle on its top edge, height persisted to localStorage, holding `TerminalView`.
It renders as a sibling of `.status-bar` inside `#main`, so it *reserves* height rather
than floating — `#main` is already `flex-direction: column` (see the reasoning in
`frontend/src/platform/ui/StatusBar.tsx`'s header, the "nothing may overlap" paragraph).

**Create `frontend/src/shell/TerminalDock.tsx`.** The chip. Uses `StatusChip`
(`frontend/src/platform/ui/StatusChip.tsx`) directly with local open state — **not**
`useStatusChip` and **not** `useExclusiveSection`: hover-to-preview is wrong for a
surface you type into, and a drawer that reserves its own height does not overlap the
other three panels, so it has nothing to arbitrate with. Label: "Terminal" idle, the
running foreground command or shell name when open, count from 2 sessions. Do not add a
key to `SECTION_ORDER` in `frontend/src/platform/lib/exclusiveSection.ts:35`.

**Modify `frontend/src/platform/ui/StatusBar.tsx:110-127`** — add a `terminalDock?:
ReactNode` prop, rendered leftmost (persistent status, the same lifetime argument its
header makes for Models).

**Modify `frontend/src/shell/App.tsx:1097-1118`** — pass `terminalDock={<TerminalDock/>}`
and render `<TerminalDrawer/>` between `{main}` and `<StatusBar/>`. The drawer's cwd
comes from `fsPathFromLocation` (already imported at :23). Name the prop `terminalDock`,
not `terminal`: `RepoUpdatesDock` in this very JSX block already takes a `terminal` prop
meaning *jobs in a terminal state* (:1112).

**Modify `frontend/src/styles/notifications.css`** — `.term-drawer` rules next to
`.status-bar` (:126). It is not a `.dl-panel`; the panel's ~340px max-width (see the
comment at :174) is unusable for a terminal, which is the whole reason the drawer is a
sibling instead.

**Create `frontend/src/shell/TerminalDock.test.tsx`.** Test-first, following
`frontend/src/shell/ModelsDock.test.tsx`: chip label per state, click toggles, hovering
the chip does *not* open the drawer, and the chip does not close Models' panel.
Drawer geometry gets no test (see Decisions).

Verify: `cd frontend && bun test src/shell/TerminalDock.test.tsx src/platform/ui/StatusBar.test.tsx`
— all pass; `bun run build` clean.
Commit: `Terminal: status-bar chip and resizable drawer`

### 6. Lifetime and exit behaviour

**Modify `fused_render/pty_session.py`** — kill every live session on server shutdown,
wired to the app's existing shutdown handler in `fused_render/server/app.py` (find it
near the lifespan/startup handlers the `LanApp` docstring at `fused_render/lan.py:610`
refers to). **Modify `frontend/src/shell/TerminalDrawer.tsx`** — on an `{"exit": code}`
frame, print a dim "Process exited (N) — press Enter to start a new shell" line and
restart on Enter, rather than leaving a dead black box.

**Extend `tests/test_pty_session.py`** with a shutdown test: two live sessions, run the
shutdown hook, both children are reaped and no orphan remains.

Verify: `pytest tests/test_pty_session.py tests/test_terminal_routes.py -q` — all pass.
Commit: `Terminal: reap sessions on shutdown, restart on exit`

## Decisions & risks

- **The drawer is a sibling of `.status-bar`, not a `.dl-panel`.** Structural call. The
  panels max out around 340px and float over content; the status bar exists *because* a
  floating card kept landing on the Claude composer. A terminal is the largest surface in
  the bar and the one most likely to be open for minutes, so it reserves height.
- **The chip opts out of hover-preview and exclusivity.** A surface you type into that
  closes on pointer-leave is wrong, and Escape-to-close collides with vim and readline.
  Nothing overlaps, so nothing needs arbitrating.
- **Fork safety is the one way this crashes the whole server.** `pty.fork()` and any
  Popen with `cwd=`/`start_new_session=True`/`preexec_fn` take the fork path, which runs
  PROJ's SQLite atfork handler and kills the server process with SIGSEGV. Hence
  `os.openpty()` in the server, posix_spawn to a bare helper, and setsid/chdir/TIOCSCTTY
  inside that helper. Task 2's Popen-kwargs assertion exists to catch a regression that a
  runtime crash would not attribute to this code.
- **No profile *settings*, no profile list.** VS Code needs `terminal.integrated.profiles`
  because it serves every shell on Windows plus WSL. One resolved `$SHELL` profile covers
  this app; a settings surface for a value that is right by default is scope we would be
  guessing at. Revisit if someone asks for fish-vs-zsh switching.
- **Windows deferred.** Python's `pty` is unix-only; ConPTY means `pywinpty`, a C
  extension added to a build that already ships its own CPython per platform. Routes 501
  and the chip is hidden, rather than a chip that fails when clicked.
- **Not on the LAN allowlist, stated as a decision rather than left implicit** — a paired
  LAN peer gets a socket close. Note this is a UX/scope call, not a security barrier:
  `/api/run` already executes arbitrary Python for any client that reaches the loopback
  server, so a pty adds no new class of exposure.
- **`TerminalView` is deliberately untested.** Headless renderers cannot measure a canvas
  or a layout; a test here would assert we called xterm. The protocol logic that *can* be
  wrong sits in `terminalSession.ts`, which is tested. Drawer resize needs a real
  interaction pass — it goes on the to-verify list, not into a test that would pass while
  the handle is undraggable.
- **Naming collision risk**: "terminal" already means *terminal job state* in
  `App.tsx`/`ActivityDock`/`RepoUpdatesDock`. New code uses `TerminalDock`/`TerminalDrawer`/
  `terminalDock` and never a bare `terminal` identifier in shell scope.
- **Risk**: the reader thread per session is a thread per live shell, capped at 8. Fine at
  that cap; if the cap ever rises this wants the shared-ticker treatment `_WATCH_REGISTRY`
  already gives stats.

## How we'll know it works

Open the app, click Terminal in the status bar: a shell opens in the folder currently
shown in the explorer, with the user's own prompt, aliases and PATH (nvm/volta/asdf
shims included, because it is a login shell). Run `vim`, resize the drawer — the editor
reflows. Navigate to another folder in the app: the shell keeps running, its scrollback
intact. Reload the page: the same shell is still there with its history. Type `exit`: the
drawer says the process exited and Enter starts a new one.

To verify by hand (cannot be asserted in a test): dragging the drawer handle resizes it
and the size survives a reload; `vim`/`htop` render and take arrow keys; the drawer never
overlaps the Claude composer.

## Build notes (appended as work landed)

- **Tasks 1-3 done and committed** (`Terminal: resolve the user's shell into a spawn
  profile`, `Terminal: pty session registry with a fork-safe exec helper`,
  `Terminal: /api/terminal session routes and byte stream`).
- **Task 6's shutdown wiring was front-loaded into Task 3's commit**: app.py's
  `include_router(terminal_router)` block already registers `@on_shutdown
  _shutdown_terminal_sessions()` calling `pty_session.REGISTRY.shutdown_all()` via
  `asyncio.to_thread`. Task 6 still owes: the extra `test_pty_session.py` shutdown-specific
  test (two sessions, run the hook, both reaped) and the frontend "Process exited (N) —
  press Enter" restart behavior in `TerminalDrawer.tsx`.
- **Two real bugs found and fixed by the route tests (not flakiness — both would leak
  in production)**:
  1. `pump_output()` in `routers/terminal.py` originally did a bare
     `await asyncio.to_thread(out_queue.get)` (no timeout). Cancelling that task on
     disconnect does NOT stop the already-running thread-pool worker (a blocking
     `Future.cancel()` on a running item is a no-op), so a disconnect from a still-alive
     session leaked a thread parked on `queue.get()` forever. In a test, this hung
     asyncio's executor shutdown (`loop.shutdown_default_executor`) forever — the whole
     test process would never exit. Fixed with `out_queue.get(timeout=0.2)` + `except
     Empty: continue`, so a cancelled task's current call returns well within one tick.
  2. `PtySession.kill()` assumed `os.killpg(self.proc.pid, ...)` is always correct because
     the helper's `os.setsid()` makes the child its own process-group leader. There is a
     real race between `Popen()` returning (as soon as the posix_spawn exec syscall
     completes) and the child actually reaching that `setsid()` call — during that window
     the child is still a member of the SERVER's own process group. `killpg` there is a
     no-op (a stale/empty group from the OS's point of view) rather than actually killing
     anything, and worse, would be catastrophic to fire against the server's own group if
     that identity check weren't there. Fixed with `os.getpgid(pid)` checked against `pid`
     before calling `killpg`; falls back to a plain `os.kill(pid, sig)` (never `killpg`)
     until the child's own session is confirmed.
- **Environment note for the next person**: on this dev machine, `pytest -n auto` (the
  project default) sometimes takes 60-100+ seconds just for xdist worker bring-up under
  load from other running dev.sh/claude processes, with ZERO output printed until it's
  done (no partial "bringing up nodes..." line makes it to the log if you're tailing a
  redirected file — block buffering). Don't conclude "hung" from that alone; either wait
  it out with a generous timeout, or pass `-n 0` while iterating (same test semantics,
  starts in under a second, and is what surfaced the two bugs above — a real deadlock
  looks identical to "still bringing up nodes" if you only give it 15-20s).
- **pytest fixtures close every pty fd / reap every child.** Every `PtySessionRegistry`
  test fixture calls `reg.shutdown_all()` at teardown (kills + joins every reader
  thread, which is what actually closes the master fd). No test leaves a session, a
  thread, or an fd running past itself — checked by running the full trio of terminal
  test files together and confirming a clean, prompt process exit under both `-n 0` and
  the default `-n auto`.
- **Task 4 done and committed** (`Terminal: xterm client and session protocol`):
  `platform/lib/terminalSession.ts` (framing, backoff, exit-stops-reconnect, no DOM
  dependency — everything DOM-touching lives in the untested `TerminalView.tsx`) plus
  10 passing tests in `terminalSession.test.ts`, `@xterm/xterm`/`@xterm/addon-fit`
  added via `bun add`. Three TypeScript traps hit and fixed, worth flagging for anyone
  writing a similar test: (1) a static import of a module that transitively reaches
  `router.ts`'s module-scope `location` read needs `installDomShim()` + a *dynamic*
  `await import(...)`, exactly like `api.test.ts`'s own documented workaround; (2) a
  value destructured from that dynamic import loses its type binding, so a
  `let x: ThatClass` annotation needs a separate `import type { ThatClass as
  ThatClassType }`; (3) a bare `let requested: string | null = null` mutated only
  inside a nested callback gets narrowed back to literal `null` at a later read by
  TS's control-flow analysis — a boxed `{ url: string | null }` has no such narrowing.
  Also caught and fixed before pushing: the first commit was missing the
  `Co-Authored-By` trailer, amended immediately (own just-made, unpushed, nothing
  stacked on top — not a "never amend" violation).
- **Task 5 done and committed** (`Terminal: status-bar chip and resizable drawer`):
  `shell/terminalDockStore.ts` (a bespoke `useSyncExternalStore` boolean, deliberately
  not `useStatusChip`/`useExclusiveSection` — see its own header), `shell/TerminalDock.tsx`
  (`TerminalDockView`/`TerminalDock` split, mirroring `ModelsDock.tsx`), `shell/TerminalDrawer.tsx`
  (owns the session id's lifecycle and the height drag, stays mounted while closed so a
  live session survives a close/reopen cycle within one page load), `.term-drawer`/
  `.term-drawer-handle`/`.term-view` CSS next to `.status-bar`, and the `StatusBar.tsx`/
  `App.tsx` wiring (`terminalDock` prop, never a bare `terminal` — that name is already
  taken by terminal-*job-state* props on `RepoUpdatesDock`/`ActivityDock` in the same
  JSX block).
  - **Deviation from the plan's chip-label text**: "Terminal" in both states, not "the
    running foreground command or shell name when open, count from 2 sessions" — this
    round only ever keeps one live drawer session, so there is no multi-session list to
    count or a foreground command to read off yet. Noted in `TerminalDock.tsx`'s own
    header for whoever adds multi-session support later.
  - **Extra defensive behavior beyond the plan's literal text**: before reusing a
    cached session id from localStorage, `TerminalDrawer` checks it against `GET
    /api/terminal`'s live list rather than trusting it blindly. Reasoning: the WS
    stream route rejects an unknown id by closing before `accept()` — no `{"exit":...}`
    frame is ever sent, so `TerminalSession` cannot tell "this id is gone" from "the
    network hiccuped" and would otherwise reconnect-loop forever against a dead id
    after a server restart (a real gap in the `TerminalSession`/route contract as
    written in Tasks 3-4, out of scope to fix at the protocol level this round).
  - **`bun run build` warns about two dynamic/static import splits** (`router.ts`,
    `api.ts`) — pre-existing (the same modules were already both statically and
    dynamically imported elsewhere before this feature; `terminalSession.ts`'s own
    dynamic-import-avoidance only applies inside its *test* file, not runtime code) and
    not a new warning introduced by this feature; the build still succeeds.
  - Two `useSyncExternalStore`-in-`react-test-renderer` `act()` fixes were needed in
    `TerminalDock.test.tsx` (documented in that file's own comments): the initial
    `create()` call itself has to be wrapped in `act()`, not just the click, because the
    subscription is registered in a passive effect that only flushes inside `act()`.
- **Task 6 done and committed** (`Terminal: reap sessions on shutdown, restart on
  exit`). The server-side shutdown wiring itself was already front-loaded into Task 3's
  commit (see the note above); this task added the one thing that was still missing —
  `tests/test_pty_session.py::test_shutdown_all_reaps_every_live_session`, two
  long-lived `/bin/sh` sessions (no self-terminating command, so the hook has to be
  what kills them) run through `registry.shutdown_all()`, asserting both `not alive`,
  both have an `exit_code`, and — the actual "no orphan" check — `os.kill(pid, 0)`
  raises `ProcessLookupError` for both pids afterward (proving the child was `wait()`ed,
  not merely signalled; the reader thread's `_read_loop` calls `self.proc.wait()`
  before exiting, and `shutdown_all` joins that thread).
  - `TerminalDrawer.tsx` now tracks an `exitCode: number | null | undefined` state
    (`undefined` = alive), wired to `TerminalView`'s `onExit` prop. On exit it renders
    a `.term-drawer-exit` status line ("Process exited (N) — press Enter to start a new
    shell") as a flex sibling BELOW `.term-view`, not an overlay on top of it — the
    dead terminal's last frame stays fully visible rather than getting dimmed or
    covered. A `document`-level `keydown` listener, installed only while `exitCode !==
    undefined`, intercepts Enter to call `createTerminalSession` again and reset state;
    it is never registered while a session is alive, so ordinary typing inside a live
    shell (including a literal Enter keystroke sent to the pty) is never touched by it.
  - No new frontend unit test for this behavior: `TerminalDrawer.tsx` was already
    outside this plan's frontend test scope (Task 5's Decisions note drawer geometry is
    a "to verify by hand" item, and this is the same component), so this reuses that
    same call rather than inventing a react-test-renderer harness for a component whose
    whole job is coordinating two other untested/hard-to-test pieces (`TerminalView`'s
    canvas, `document`-level key handling). Verified by the typecheck + boundary-check +
    targeted `bun test` + `bun run build` combination that already covers this file's
    compile correctness; the actual "Enter restarts the shell" behavior needs a live
    browser pass — added to the to-verify list below.
  - **To verify by hand** (added to the plan's existing list): typing `exit` in the
    drawer shows the exit line without covering the last frame; pressing Enter starts a
    fresh shell in the same drawer; the drag handle actually resizes and the height
    survives a reload.
