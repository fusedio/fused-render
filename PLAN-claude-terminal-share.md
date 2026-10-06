# PLAN: share terminals between the user and Claude

Builder handoff. Append decisions, dead ends and spec corrections to the
**Build notes** section at the bottom as you go — the next builder resumes from
this file plus `git log`.

## Goal

1. Claude (the native chat, `fused_render/claude_agent/`) can see the user's
   status-bar terminals and knows which one the user means.
2. The user can watch, live, the output of every shell command Claude runs
   (foreground and `run_in_background`) in a read-only "Claude" tab in the
   terminal drawer.

Platform: POSIX only. Every terminal route already 501s on Windows
(`fused_render/server/routers/terminal.py`); on Windows the new MCP tools must be
ABSENT from `tools/list` (same rule as `app_state`: a tool that can never
answer is worse than none — see the docstring of
`fused_render/claude_agent/permission_server.py`).

## Existing pieces (read these first)

- `fused_render/pty_session.py` — `PtySession` (scrollback, `attach`,
  `subscribe`, `shell_is_foreground`, `write`), `PtySessionRegistry`, `REGISTRY`.
- `fused_render/terminal_profiles.py` — how the user's shell is resolved and spawned.
- `fused_render/_pty_exec_helper.py` — fork-safe exec helper (memory: posix_spawn
  needs close_fds=False + absolute exe path + no cwd=; do not regress).
- `fused_render/server/routers/terminal.py` — `/api/terminal*` routes + WS stream.
- `fused_render/claude_agent/permission_server.py` — per-run stdio MCP server,
  stdlib only, NO `fused_render` import; already talks to the server over
  `urllib` (see around line 213–280). `app_state` is the model for a pre-allowed tool.
- `fused_render/claude_agent/agent.py` — `_write_mcp_config` (~1432), the claude
  argv build (~2396–2445, `--allowedTools` list incl. pre-allowed app_state), spawn env.
- Frontend: `frontend/src/shell/TerminalDrawer.tsx`, `TerminalTabStrip.tsx`,
  `terminalTabs.ts`, `frontend/src/platform/lib/terminalDockStore.ts`,
  `terminalSession.ts`, `frontend/src/platform/ui/TerminalView.tsx`, and the chat
  composer/send path of the native chat (find where the page posts `message` to
  `/api/claude/agent` start).

## Part 1 — Claude reads the user's terminals

### 1a. Server: screen text
- `GET /api/terminal/{sid}/text?lines=N` (default 200, clamp 1..2000) →
  `{"id", "text", "cwd", "foreground", "alive", "exitCode", "lastCommand",
  "lastExit", "lastActivity"}`.
- `text` must be what a human sees, not raw bytes with ANSI stripped: feed the
  session's output through a VT emulator so `\r` progress bars, cursor moves and
  full-screen TUIs render correctly. Use `pyte` (add to the base deps in
  pyproject.toml — check it is pure-python and small; record in Build notes) —
  keep a `pyte.HistoryScreen` per session fed from the reader thread, or replay
  scrollback on demand; pick one and justify it in Build notes (memory cost vs
  CPU). Result = history lines + current screen, trailing blank lines trimmed,
  last N lines.
- `foreground`: name of the foreground process group leader (`tcgetpgrp` on the
  pty master → process name via `ps -o comm= -p` or `/proc`; macOS has no /proc).
  `null` when the shell itself is foreground.
- Same `_require_fused` guard as the other routes? Reads are GET like
  `api_terminal_list`, which is unguarded — follow `api_terminal_list`.
- Extend `GET /api/terminal` list entries with `foreground`, `lastCommand`,
  `lastExit`, `lastActivity` (additive).

### 1b. Shell integration (feeds lastCommand / lastExit / cwd)
- Inject OSC 133 (prompt/command start/end + exit code) and OSC 7 (cwd) when we
  spawn the user's shell, VS Code style, WITHOUT editing the user's rc files:
  zsh via a `ZDOTDIR` shim dir that sources the user's real `$ZDOTDIR`/`~` files
  then adds precmd/preexec hooks; bash via `--rcfile` shim that sources
  `~/.bashrc` (login shell semantics: check what terminal_profiles currently
  does and preserve it). Other shells (fish, etc.): no injection, fields stay null.
- Command text: emit it via OSC 633;E-like private sequence or record from
  preexec `$1`; your choice, document it.
- Parse these sequences in `PtySession`'s reader path into per-session state
  (`last_command`, `last_exit`, `cwd` live-updated, `last_activity`). They must
  still reach xterm (xterm ignores unknown OSC) — do not strip them from the stream.
- Must not break: the `/input` busy guard, the "no stray %" fix (#1432), Nerd
  font, existing terminal tests.

### 1c. MCP tools (permission_server.py)
- `terminal_list` → the list above, plus `focused: bool` (see 1d).
- `terminal_read(id?: str, lines?: int)` → the `/text` payload; no id = the
  focused terminal; error text if none.
- Both pre-allowed exactly like `app_state` (add to the `--allowedTools` set in
  agent.py). Present only on POSIX and only when the server is reachable.
- `terminal_send(id, text)` → POSTs to `/api/terminal/{sid}/input`; NOT
  pre-allowed, so it goes through the normal permission card. Keep the 409
  busy refusal and surface it as a tool error.
- The MCP server needs the server's base URL + X-Fused header value; reuse
  however the existing urllib call there gets them.

### 1d. Which terminal the user means
- The drawer's focused tab id must be knowable server-side at message time.
  Simplest: when the chat page sends a message, it includes a short
  `terminal_hint` (focused tab id, title, cwd, lastCommand, lastExit, age) read
  from `terminalDockStore` + `GET /api/terminal`; agent.py appends it to the
  turn as a one-line system note only when the drawer has at least one live
  terminal. Metadata only — never terminal contents. Also persist "focused id"
  so `terminal_read()` with no id resolves (e.g. the page PUTs it, or the hint
  carries it into the run dir — your call, note it).
- Drawer tab: an "Ask Claude" action on each terminal tab that opens/focuses the
  chat and attaches that terminal (inserts a reference chip / text like
  `[terminal t3: zsh — ~/x]` that makes Claude call terminal_read on it).
- Optional if cheap: "Send selection to Claude" from xterm selection.

## Part 2 — watch Claude's commands live

### 2a. Probe first (gate)
`claude` (2.1.29x, `~/.local/share/claude/versions/`) contains
`CLAUDE_CODE_SHELL_PREFIX`. Before building on it, verify with a throwaway
headless run (`claude -p` with a prompt that runs a Bash command, foreground and
`run_in_background`) and a prefix script that logs its argv:
- exact invocation shape (does it get the command as one arg? `-c`? the shell?);
- whether background commands go through it;
- whether exit codes / stdin / cwd persistence survive.
Record findings verbatim in Build notes. If the prefix is unusable, implement
the FALLBACK below instead and say so in your report.

### 2b. Wrapper + log
- A stdlib/POSIX-sh wrapper (shipped in the package, absolute path) that agent.py
  sets as `CLAUDE_CODE_SHELL_PREFIX` in the claude spawn env. It runs the command
  unchanged, preserving exit code, tees stdout+stderr into a per-chat log (in the
  run dir or keyed by claude session id so it survives turns), writing a header
  line per command (command text, pid/pgid, start time) and a footer (exit code).
- Must not change what Claude sees (output, exit code, timing beyond noise).

### 2c. "Claude" tab in the drawer
- A read-only session kind in the registry (or a parallel small registry) backed
  by that log file: same WS stream protocol (binary out, `{"exit"}` text), input
  frames ignored. Listed by `GET /api/terminal` with `kind: "claude"` and the
  chat it belongs to. Shown as a distinct tab ("Claude", visually marked),
  created when a chat's first command runs; one per chat session.
- Stop button: kills the pgid of the currently running/background command(s)
  recorded by the wrapper.
- Reconnect/scrollback replay works like real sessions.

### FALLBACK (only if 2a fails)
Replay Bash `tool_use` / `tool_result` from the run's stream-json log into the
same read-only tab after each command completes (not live). Note in report.

## Testing
- TDD, scoped: `.venv/bin/python -m pytest tests/test_terminal_routes.py -k ...`,
  the relevant pty/agent/permission_server test files, `cd frontend && bun test
  <file>`. NEVER the full pytest or full bun suite (full bun run wedges the
  machine — memory). Orchestrator runs the full suite at the end.
- Real-pty tests for shell integration (spawn zsh and bash if present, run `false`,
  assert lastExit=1, lastCommand="false", cwd follows `cd`).
- permission_server tests: tools present/absent per platform and per reachability;
  terminal_send is not pre-allowed.
- Wrapper: exit code preserved, stderr captured, background command logged.
- Tests pin literal frontend source text in places (memory) — if a bun/pytest test
  greps a TS line you change, update it deliberately.
- `bun run build` once after frontend changes (a `*/` in a CSS comment breaks only the build).

## Constraints
- Branch: `worktree-claude-terminal-share`. Do NOT call EnterWorktree; your cwd is
  already correct. Never touch main.
- Env: follow the in-repo `setting-up-dev-env` skill
  (`.claude/skills/setting-up-dev-env/SKILL.md`) first; confirm
  `.venv/bin/python -c "import fused_render; print(fused_render.__file__)"`
  prints this worktree.
- Do NOT start a dev server (dev.sh is user-run only).
- Subprocess spawns from the server process: follow the existing fork-safety
  patterns (close_fds etc.) — read `_pty_exec_helper.py` comments.
- DECISIONS.md is an append-only project log — add one entry for this feature,
  using the next free D-number on this branch.
- Commit per logical unit (1a, 1b, 1c, 1d, 2a notes, 2b, 2c). Use
  `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` trailers.

## Build notes
### Builder 1 (partial: Parts 1a, 1b, 1c, focus endpoint done; 1d frontend and Part 2 NOT done)

- **Part 2a probe: BLOCKED.** The auto-mode classifier denied launching a headless `claude` run twice. No retry or workaround. Nothing is known about `CLAUDE_CODE_SHELL_PREFIX`; Part 2b/2c (or the FALLBACK) need a human to run the probe or pick the fallback.
- **pyte**: pure python, ~212K, one dep (wcwidth). Base dependency, imported lazily. Rendering is on demand by replaying the 256 KiB ring (~0.3 s per read) rather than a live per-byte emulator, so idle terminals cost nothing.
- **Shell integration** lives in `fused_render/shell_integration.py`, applied by `PtySession registry.create` via `integrate(profile)`, NOT in `resolve_profile` (existing profile tests assert argv `["/bin/zsh","-l"]`). zsh uses a ZDOTDIR shim restoring ZDOTDIR at the end of .zshrc. bash uses `--init-file` and drops `-l`; FUSED_SHELL_LOGIN makes the shim replay login semantics (so `shopt login_shell` reads off). bash 3.2 `history 1` prefixes a number, which the shim strips.
- **MCP tools** are keyed on a dedicated `FUSED_RENDER_TERMINAL_ORIGIN` env stamped into mcp.json by agent.py (POSIX only), not the ambient FUSED_RENDER_ORIGIN, so existing "tools == [approve, app_state]" tests stay deterministic. `terminal_list` and `terminal_read` are pre-allowed; `terminal_send` keeps its permission card.
- **Focus**: `PUT /api/terminal/focus {"id"}` stores `REGISTRY.focused_id`; list entries gain `focused`. The MCP read with no id uses the focused session, else the only live one.
- **Not done**: the `terminal_hint` note in agent.py, the drawer posting its focused id, and the "Ask Claude" tab action (Part 1d frontend); Part 2b/2c.

### Builder 2 (Part 1d done; Part 2 still blocked on the human-run probe)

- **terminal_hint transport.** The page sends `terminal_hint` (a JSON string of metadata only) as a param of `start`/`send`, NOT inside the message text. `agent.py::_with_terminal_hint` turns it into one `<terminal-hint>...</terminal-hint>` line inserted AFTER any leading machinery blocks (the app-state block must stay first: `_pane_file` anchors on it) and before the user's words; only the inbox row carries it, `meta.json`'s message stays clean. `terminal-hint` was added to `_MACHINERY_STRIP` in BOTH `agent.py` and `tasks_store.py` (parity test pins them) so restored transcripts and the Tasks list never show it. Fields are single-line, length-capped, `<`/`>` stripped so a command cannot close the tag.
- **Frontend.** `platform/lib/terminalFocus.ts`: `reportFocusedTerminal(id,label)` (PUT /api/terminal/focus, deduped, failure retried next change), `terminalHint()` (GET /api/terminal; focused tab, else server `focused` flag, else the lone live terminal, else no hint; "" with no live terminal / embed / Windows), `askClaudeTerminalPrompt`. `run-controller` calls the optional `deps.terminalHint` once per send beside `appStateBlock` and spreads it into all four start/send calls; `ClaudeChat` wires it.
- **Drawer.** Reports its active tab when open and on every tab change (nothing while the list is still unverified; null only when the list is emptied; the last tab stays reported after the drawer closes). Tab strip gets an optional `onAskClaude` button (`.term-tab-ask`) that reuses the existing `explainWithAi` seam: stages the prompt `[terminal t3: zsh - cwd]` + "use terminal_read with id ..." and navigates to the tab's cwd (default folder when none) so the folder chat opens seeded with it.
- **Not done.** "Send selection to Claude" (xterm selection lives inside TerminalView; not cheap). The queued-send road (`admitQueueSend`) carries no hint: only the live `start`/`send` calls do.
- Tests: `tests/test_claude_terminal_hint.py`, `terminalFocus.test.ts`, additions to `TerminalDrawer.test.tsx` (fake server now answers PUT) and `run-controller.test.ts`.

### Fix round (review findings)

- `pyte` added to `_IMPORT_TO_DIST` (suite failure). zsh shim now unsets ZDOTDIR when FUSED_USER_ZDOTDIR is empty too (was left empty, so .zlogin was looked up in `/`). bash `__fr_debug` records `$HISTCMD` in `__fr_post` and only trusts `history 1` when it advanced; otherwise it reports `$BASH_COMMAND` (the executing simple command, not the whole line, for unsaved lines).

### Builder 3 (Part 2: probe results, wrapper, Claude tab)

- **Probe results (human-run, claude 2.1.291; supersede 2a).** The prefix is invoked as `<prefix> <ONE argument>`, argc == 1, a complete shell command string, which the prefix must run through a shell itself. It wraps EVERYTHING claude spawns: hooks, statusline, MCP stdio servers (very likely permission_server.py), not only the Bash tool. Bash-tool commands look like `source .../shell-snapshots/snapshot-zsh-<id>.sh 2>/dev/null || true && { shopt -u extglob || setopt NO_EXTENDED_GLOB ...; } ... && eval '<USER COMMAND>' < /dev/null && pwd -P >| /tmp/claude-XXXX-cwd`; run_in_background goes through it too. cwd persistence is claude reading that `-cwd` file, so the string runs unmodified.
- **Wrapper**: `fused_render/claude_shell_prefix.sh` (tests: `tests/test_claude_shell_prefix.py`, real sh/zsh/bash). Stderr/stdout stay on separate fds via two fifos + tee, exit status preserved (false -> 1, exit 7 -> 7), a cd still writes the -cwd file, stdin passes through, a TERM to the wrapper is forwarded to the command. NOT verified against a live claude: exit/stderr propagation as claude consumes it, and the prefix on hooks/MCP.
- **Deviation**: Stop kills the wrapper process TREE (ps walk), not the recorded pgid, because the pgid may be the claude CLI own group. `/bin/sh` on macOS makes `source <missing file>` fatal (POSIX mode), so the tests ship a real empty snapshot file; zsh/bash are exercised too.
- **Log key**: session id (`session_id or new_session_id`, passed from session_host to `_spawn_env`). If the CLI ever mints a new id on `--resume`, the resumed chat gets a second Claude tab.
- **Server**: `claude_cmd_log.py`, routes in `routers/terminal.py`, tests `tests/test_terminal_claude_kind.py` (includes a real-wrapper Stop test). `terminal_read` works on `claude:<chat>` (the /text route handles it) but is not separately tested through the MCP tool.
- **Frontend**: `terminalTabs.ts` (`syncClaudeTabs`, claude-aware reconcile/cachedTabs), `TerminalTabStrip` (is-claude, Stop), `TerminalDrawer` 2 s poll, `stopClaudeCommands`, CSS in `notifications.css`. Tests added to `TerminalDrawer.test.tsx`. D1325 in DECISIONS.md.

### Fix with AI / Ask Claude on selection (Builder 4)

- Unit 1: `claude_cmd_log._descendants/_is_wrapper` now spawn `/bin/ps` (or `/usr/bin/ps`) with close_fds=False and no cwd; test pins it for both. No other subprocess call was added on this branch (checked the diff).
- Unit 2: `terminalAi.ts` (+tests), prompt builders in `terminalFocus.ts`, TerminalView pills and OSC handlers, drawer `askSelection`/`fixFailure`, CSS next to the terminal rules. D1326.
- Deviation: pill top gets an 8px offset (`.term-view` padding) because it is positioned against `.term-view`, not the surface. No TerminalDrawer test added (no cheap seam for the TerminalView props beyond `view()`).
- All visual and click behaviour unverified.
