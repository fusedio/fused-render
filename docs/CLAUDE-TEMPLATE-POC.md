# Claude template — POC notes

**Status:** POC, 2026-07-08. Not a locked design; every choice below was made
for implementation simplicity and is expected to be revisited.
Origin: adapted from an internal chat sandbox POC (detached `claude -p`
subprocess + stream-json log + poll loop).

## What it is

A new built-in template `fused_render/templates/claude/` bound to `.html` /
`.htm` as a third mode (`["_render", "code", "claude"]`). Opening an HTML
file and switching to the `claude` mode gives a chat UI (claude.ai-style
landing + terminal-style chat) that talks to the local Claude Code CLI
**about that file**.

```
fused_render/templates/claude/
├── template.html   # chat UI; adapted from the internal chat sandbox POC
├── agent.py        # runPython backend: start/poll/sessions/history/cancel; stdlib only
└── icon.svg        # monochrome asterisk for the mode switcher
```

## How it works

- **Target file** arrives as the standard read-only `_file` param. Every
  `agent.py` action that needs it receives it explicitly (`file` param) —
  the subprocess model has no other channel.
- **Working directory:** `claude` is spawned with
  `cwd = dirname(target file)`. Note this is unrelated to the executor's own
  cwd (`_child.py` chdirs to `agent.py`'s folder — the template dir); the
  Popen `cwd=` argument is what scopes Claude. Resume also always runs from
  that same directory, so Claude Code's per-project session storage
  (`~/.claude/projects/<munged-cwd>/`) stays consistent per file-folder —
  the sandbox POC's "find the session's original cwd by globbing
  transcripts" hack is dropped.
- **Scoping system prompt:** `--append-system-prompt` (keeps Claude Code's
  default system prompt, appends ours) tells Claude the user is viewing
  `<file>`, to treat it as the subject, and to stay scoped to it *unless the
  user explicitly asks for more* — soft instruction, not enforcement,
  exactly as requested.
- **Streaming:** `start` detaches `claude -p <msg> --output-format
  stream-json --verbose --include-partial-messages` with stdout redirected
  to `$TMPDIR/fused_render_claude/runs/<run_id>/out.jsonl`; the page polls
  (`action=poll`) every 400 ms and re-parses the file. Fresh-process-per-call
  executor makes any push channel impossible anyway (30 s timeout), so
  poll-a-file is the natural fit.
- **Sessions sidecar (the "ai data" file):** `<file>.json` next to the
  target — `my-folder/sample.html` → `my-folder/sample.html.json`:

  ```json
  {
    "claudeSessions": [
      {"id": "<uuid>", "preview": "first user message…", "created_at": 1751…, "last_used": 1751…}
    ]
  }
  ```

  The first `poll` that sees the session id (from the `system` init row)
  writes the entry (atomic temp+`os.replace`; a `recorded` marker in the run
  dir keeps it one-shot). The landing page lists **only** these sessions —
  never the user's global `~/.claude` history.
- **Resume chains:** plain `--resume` keeps the session id (verified
  empirically 2026-07-08; earlier claude versions and `--fork-session` mint
  a new one). `start` remembers `resumed_from`; the sidecar update replaces
  the old id in place (keeping `created_at`/`preview`) either way, so one
  conversation stays one row.
- **Session portability (copy-on-resume):** claude stores transcripts under
  `~/.claude/projects/<munged-cwd>/<id>.jsonl` (munging: non-alphanumeric →
  `-`) and `--resume` only looks in the *current* cwd's project dir — moving
  `sample.html` + `sample.html.json` to another folder would otherwise break
  resume ("No conversation found"). Each sidecar entry therefore records its
  `cwd`; on resume, if the transcript is missing from the new directory's
  project dir, `_migrate_session` copies it over from the recorded old cwd
  (never overwriting an existing destination — that's where new turns
  append, so it is always the newer copy) and updates the entry's `cwd`.
  Verified live: full conversation context survives the move. The munging
  rule is claude-internal, not API — if it changes, the failure mode is just
  claude's own "session not found". No glob fallback (owner call): the
  sidecar's `cwd` is the single source for where a transcript lives.
- **History is file-scoped too:** the `history` action requires `file` and
  reads only `<munged(dirname(file))>/<id>.jsonl`. With *copied* files the
  same session id exists in several project dirs with divergent content —
  a glob could render some other copy's conversation while resume continues
  this one's. History also runs `_migrate_session` first (like `start`), so
  opening a moved file's saved session shows its turns immediately instead
  of appearing empty until the first new message triggers migration.
- **Permissions:** spawned with `--permission-mode acceptEdits` so headless
  Claude can actually edit the file (non-interactive runs can't answer
  permission prompts; the default would stall/deny every Edit).

## Deliberate simplifications / tradeoffs (revisit later)

1. **`acceptEdits` without confirmation UI.** Claude edits files (anywhere,
   if the user insists) with no approval step in the browser. Right POC
   call, wrong product call — a real version wants a permission bridge
   (e.g. `--permission-prompt-tool` via MCP, or the Agent SDK's canUseTool)
   surfacing approvals in the chat UI.
2. **Polling over push.** 400 ms `runPython` polls = one fresh Python
   subprocess per poll. Wasteful but fits the executor contract with zero
   server changes. A real version wants a server-side run manager +
   WebSocket (see D74 precedent). The detached-run design does buy one
   thing for free: the in-flight run id rides the URL as the `run` param,
   so a frame that dies mid-stream (mode switch, reload) re-attaches on the
   next boot — poll replays the whole turn's text (partial history rows for
   that turn are trimmed first; `poll` returns the run's `message` so the
   user turn renders even when the transcript had no rows yet). A stale
   `run` param (bookmark, pruned tmp) is cleared silently.
3. **Sidecar is claimed, not reserved.** `<file>.json` may already exist as
   a user's own data file — agent.py tolerates non-conforming JSON (treats
   as empty) but a *save would clobber it*. No namespacing (`.fused-ai.json`
   or similar) yet; naming follows the requested shape literally
   (memory: URL/file shapes are literal).
4. **Sidecar races.** Atomic replace prevents torn writes, but two
   concurrent chats on the same file can lose one entry (last writer wins on
   read-modify-write). Accepted for POC.
5. **Session transcripts belong to Claude Code, not us.** The sidecar stores
   ids + cwd; history is rebuilt from the file's own project dir. If the
   user deletes/cleans Claude Code data, the sidecar rows go stale (resume
   fails with claude's error; history shows empty). No pruning yet.
   Copy-on-resume also leaves the *original* transcript behind: a second
   copy of file+sidecar resumes from the state at copy time and diverges —
   a silent fork, same session id in two project dirs (owner call: copy,
   not move; fork semantics deemed reasonable for copied files).
   Cross-*machine* transfer would need the transcript embedded in the
   sidecar — out of scope.
6. **Only text turns render.** Tool calls/diffs stream past invisibly (a
   "Working…" spinner phase is the only signal). Showing tool activity
   (edits made to the file!) inline is the obvious next feature.
7. **`claude` binary discovery:** `FUSED_RENDER_CLAUDE_BIN` (explicit
   override, mirroring `FUSED_RENDER_RCLONE_BIN`), then `shutil.which`, then
   the platform's install locations — `~/.local/bin`, `/opt/homebrew/bin`,
   `/usr/local/bin` on POSIX; see **Windows** below for that list. The server
   env's PATH (Finder-launched .app! GUI-launched .exe!) may lack it; the
   error message names every place we looked.
8. **`run_id`/tmp hygiene.** Run dirs under `$TMPDIR/fused_render_claude/`
   are never pruned (OS tmp cleanup handles it eventually). Cancel action
   exists in agent.py but has no button in the UI yet.
9. **Bound only to `.html`/`.htm`** (as requested — "html to begin with").
   Template itself is file-type-agnostic; binding other extensions is a
   registry edit away. Generalizing (e.g. a `"*": [..., "claude"]` splice or
   a per-mode "chat about this file" affordance) is a product decision.
10. **`model`/`effort`/`session_id` ride ordinary URL params** — so a
    bookmark or pane layout restores the exact conversation (nice), but
    switching modes keeps them on the shell URL (documented registry quirk;
    `session_id` is meaningless to other templates but harmless).
11. **No tests for agent.py.** It shells out to a user-installed CLI;
    meaningful tests need a fake `claude` binary. The registry/test pin
    covers resolution (`.html` → `_render, code, claude`).

## Windows

The template was written against POSIX and broke on Windows in four separate
places — each a POSIX idiom that Windows either ignores or reinterprets.

- **`claude` is usually not on our PATH.** The PowerShell installer puts
  `claude.exe` in `%USERPROFILE%\.local\bin` and appends that to the *user*
  PATH in the registry; a process that was already running (or was started by
  Explorer before the install) keeps the PATH it inherited at login, so
  `shutil.which("claude")` finds nothing while `claude` works fine in a fresh
  terminal. `_claude_bin` therefore also looks in the known install locations,
  `%USERPROFILE%\.local\bin\claude.exe` (native installer) first, then winget's
  shim dir and npm's global prefix. `.exe` is preferred over a `.cmd` shim:
  a shim hands our argv back to cmd.exe for a second round of parsing, and that
  argv carries arbitrary user text (`-p`) plus the target path.
- **`start_new_session=True` does nothing on Windows** — subprocess accepts and
  ignores it. So `claude.exe` was spawned with no creationflags at all, and
  because the executor deliberately gives its worker no console
  (`CREATE_NO_WINDOW`, so a windowless server doesn't flash a console per run),
  Windows had to allocate a **fresh console window** for the console-subsystem
  child: a terminal window popped up for every chat turn. `_DETACH` now passes
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` there — the same detach idiom
  as templates/docs, latex and usd — which both detaches the run and leaves it
  console-less (stdout/stderr already go to the run dir).
- **`os.kill(pid, 0)` is not a liveness probe on Windows.** Signal 0 *is*
  `CTRL_C_EVENT`, so Python routes it to `GenerateConsoleCtrlEvent` instead of
  a no-op check: it either delivers a real Ctrl+C or fails outright, and the
  failure made `_alive` report a perfectly healthy run as dead. Since the page
  polls 400 ms after `start`, the first poll killed or condemned the turn —
  surfacing as *"claude exited unexpectedly"* with an empty `err.log`. `_alive`
  now goes through `../shared/procutil.pid_alive` (OpenProcess +
  GetExitCodeProcess), which is what the rest of the repo already uses.
- **`os.killpg` doesn't exist on Windows**, so `cancel` raised AttributeError
  instead of stopping the run. There is no process group to signal either —
  `CTRL_BREAK` only reaches a shared console and a detached run has none — so
  Windows cancels with `taskkill /PID <pid> /T /F`, which also collects the
  children claude spawned for its own tools. It runs with `CREATE_NO_WINDOW`:
  taskkill is itself a console program, so it would otherwise flash the very
  window the detach fix removes (Bugbot, PR #307). The server's global
  no-window policy does not reach here — it patches `Popen` in `cli.py`'s
  process, and the executor worker is a bare `python _child.py`.

Two related fixes came out of the same pass:

- **`CLAUDE_CONFIG_DIR` is honoured** when locating transcripts. The supervisor
  sets it for every packaged build (`supervisor/paths.py child_environment`),
  so claude writes the transcripts for *our* runs under the app's state dir —
  reading a hardcoded `~/.claude/projects` lost history and copy-on-resume in
  the packaged app on every platform, not just Windows.
- **The id guards reject `\` and a drive prefix.** `run_id` and `session_id`
  arrive as URL params and are joined onto a directory we own; on Windows
  `..\..\x` traverses exactly like `../../x`, and `os.path.join(runs, "d:x")`
  drops `runs` entirely. Both now go through `_bad_id`.

Not covered here: the `claude-cli://` deep link (`Open in Claude`, PR #286) is
a different path — the OS hands the URL to Claude Code's own scheme handler,
and nothing in this template is involved.

## Synergy worth noting

Claude edits `sample.html` → M4 auto-reload already live-refreshes the
`_render` view. A panel layout with `_render` on the left and `claude` mode
on the right (`/view/_panel?_layout=(…sample.html,…sample.html?_mode=claude)`)
is a working "live preview + AI pair-editing" surface with zero new code.
The chat frame itself calls `fused.autoReload(false)` (LR-5): the runtime
watches `_file`, so without the opt-out Claude's own edit reloaded the chat
mid-stream — freezing the reply mid-sentence and orphaning the run.
