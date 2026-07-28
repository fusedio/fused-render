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
├── template.html          # chat UI; adapted from the internal chat sandbox POC
├── agent.py               # runPython backend: start/poll/decide/sessions/history/cancel; stdlib only
├── permission_server.py   # one-tool stdio MCP server: the approval bridge (stdlib only)
└── icon.svg               # monochrome asterisk for the mode switcher
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
- **Permissions — the chat window IS the prompt** (see the next section).
  No `--permission-mode`: the default (ask) is answerable now.

## Approvals: the permission bridge (2026-07-28)

Headless `claude -p` has no terminal to prompt on. The POC's answer was
`--permission-mode acceptEdits`, which bought silent edits anywhere on disk
*and still left everything else broken*: a Bash/WebFetch/Write call the
session's rules didn't already allow came back
`tool requires user interaction; no prompt available in headless mode` — an
invisible refusal. The user saw Claude decline, with no prompt and no reason.

`--permission-prompt-tool` names an MCP tool the CLI calls **instead of**
prompting, and `permission_server.py` is that tool:

```
claude (headless)                    agent.py / browser
   │  needs to run Edit
   ├─► mcp__fused_approvals__approve
   │      └─ writes  perm/<id>.req.json ──────► poll → card with Allow/Deny
   │         …blocks…                             │
   │         reads   perm/<id>.res.json ◄─────────┘ decide
   └─◄ {"behavior": "allow", "updatedInput": …}
```

- **The card is the prompt.** While one is unanswered the subprocess is
  genuinely blocked, `poll` reports `phase: "awaiting"`, and the status line
  reads "Waiting for your approval…".
- **How many cards you get is a picker** (`permission` URL param, next to
  model/effort), mapping onto the CLI's own `--permission-mode`:

  | label | mode | measured against CLI 2.1.220 |
  |---|---|---|
  | ask every time *(default)* | *(none)* | `Edit` **and** `Write` both carded |
  | auto-accept edits | `acceptEdits` | edit applied with no card; `WebFetch` still carded |
  | Claude decides | `auto` | its classifier vouched for the edit + write; escalates what it won't |

  The bridge stays wired in **all three** — the mode only decides how much is
  auto-approved *before* the prompt tool is consulted, and whatever is left has
  to stay answerable or it is a silent refusal again. `bypassPermissions` is
  deliberately not on the menu, and an unknown value falls back to the
  strictest: more auto-approval is opted into, never handed over by a mangled
  param. "Claude decides" is a *broader* opt-in, not a blanket one.
- **Wire shape** (CLI 2.1.220): in `{tool_name, input, tool_use_id}`, out a
  *single* text block whose text is JSON — `{"behavior": "allow",
  "updatedInput": …}` or `{"behavior": "deny", "message": …}` (the message is
  required). Anything else and the CLI raises "Permission prompt tool returned
  an invalid result". Pinned by `tests/test_claude_permission_bridge.py`,
  which drives the server over its stdio JSON-RPC without invoking claude.
- **Request ids are ours, not the CLI's `tool_use_id`** — the id is joined into
  a path, and a name we minted cannot escape the perm dir.
- **The card shows the whole input, and that is a security property.** An Allow
  returns `updatedInput` **unchanged**, so anything the card elided would still
  run. The input is model-authored, so a prompt-injected model that knows where
  a cut falls can put something benign in front of it and the real payload
  behind it — the user clicks Allow on the part they can read. Two rules
  therefore hold: nothing is truncated (the `<pre>` is `max-height` +
  `overflow: auto`, which makes length a *scrolling* problem, not a disclosure
  one), and every `input` key the curated summary has no case for is rendered
  verbatim underneath it rather than assumed unimportant. Both are pinned by a
  node probe that runs the card's own `summarizePermission` over a table of
  tool inputs — including a 5 KB command whose last line is the destructive
  one, and a `Grep` whose `pattern` used to vanish whenever a `path` was set.
- **Decisions are a one-way latch.** `O_EXCL`, first writer wins: a
  double-click, or a cancel landing on a card that was just allowed, must not
  overwrite a verdict the tool may already have acted on. Anything that is not
  the exact string `allow` fails closed.
- **A lost race is waited out, never guessed.** `O_EXCL` makes the file's
  *existence* the latch while its content lands a moment later, so for a few
  microseconds it is there and unparseable. Both readers that lose the create
  treat that as a write in flight (`DECISION_WRITE_WINDOW`, 2 s) rather than as
  "nobody answered" — reading it the second way is how the loser of a
  double-click reported *its own* verdict, and how the server's timeout could
  hand claude a deny for a tool the user had just allowed. `poll` is the one
  reader that never waits: it runs every 400 ms, so it reports the request as
  still pending and the next tick corrects it. A writer that dies after the
  create unlinks the file it claimed, since an empty one holds the latch
  forever while never parsing. The UI follows the same rule — a card re-renders
  when the polled verdict differs from the one the click rendered
  optimistically, so the file is always what the label ends up showing.
- **"Allow all X in this reply"** returns `updatedPermissions: [{type:
  "addRules", rules: [{toolName}], behavior: "allow", destination: "session"}]`
  — the CLI's own rule engine does the matching. The rule is the **bare tool
  name**: the wire hands us no permission *suggestions*, and inventing our own
  `Bash(rm -rf *)`-style patterns would be a hand-rolled matcher in the one
  place that must not have one.
- **…and it is only offered where a whole-tool grant is proportionate**
  (`WHOLE_TOOL_GRANTABLE`: `Edit`, `Write`, `Read`, `Glob`, `Grep`,
  `NotebookEdit`). Those are the repeat-heavy file tools this template exists
  to drive, where the grant is the difference between one click and eight.
  Bash, the web tools, and everything unrecognised (MCP tools included) get
  **Allow/Deny only** — each such call is its own action with its own blast
  radius, so one `gh pr diff` is no reason to hand over every command for the
  rest of the turn, and a blanket Bash grant is close to switching approvals
  off. The middle ground the CLI's own prompt offers — a rule narrowed to
  *that* command — is unavailable to us for the reason above, so the honest
  choice is all-or-nothing per tool, defaulting to nothing.
- **…and it is enforced in `agent.py`, not only on the card.** The page is a
  view, and a view is the wrong place for the only copy of a security-relevant
  rule — any other caller of `decide` would otherwise get the session-wide Bash
  grant the UI never offers. `WHOLE_TOOL_GRANTABLE` exists on both sides and a
  test asserts the two lists are identical (D146: a duplicated rule needs a
  test, not a comment). A session scope asked for on an ungrantable tool
  **narrows to allow-once** rather than erroring, and the effective scope is
  reported back.
- **It is a rule, not a mode.** The update is `addRules` for one `toolName`;
  the CLI's separate `setMode` update is a different button (below). Verified
  end to end: after allow-all on an `Edit`, a second `Edit` went through
  untouched and a `Write` in the same turn still parked its own card.
- **"Allow, and let Claude decide from here"** is that other button — the same
  `updatedPermissions` channel carrying `setMode` instead of `addRules`, which
  re-points the **running** session rather than adding one rule to it.
  Measured: a turn that carded `Edit`/`Write`/`Write` in the strictest mode
  carded only the `Edit` once the first card switched. It is offered only while
  a stricter mode is in force (pointless once you are already in `auto`), only
  alongside an *allow* (a deny that loosened the mode would be incoherent), and
  only for `SWITCHABLE_MODES` — `bypassPermissions` is unreachable from a card
  by any route, re-validated in `permission_server` because that is the side
  that hands the CLI its payload. The click also writes the `permission` param,
  because a `setMode` dies with the process exactly like a session rule and the
  next turn would otherwise go back to asking.
- **Nobody home:** an unanswered request denies itself after
  `FUSED_RENDER_PERMISSION_TIMEOUT` (default 1 h, read in `agent.py` *and*
  `permission_server.py` — the former stamps the resolved value into
  `mcp.json`, so a constant there would silently overwrite whatever the user
  set) and writes that verdict down, so a re-attaching frame doesn't render
  buttons that lead nowhere. The per-server `timeout` in the generated
  `mcp.json` is set *above* that, so our sentence wins over the CLI's
  MCP-timeout error. `cancel` releases every parked request before killing the
  process group.
- **A run that ends latches its leftovers.** `poll` marking an unanswered
  request `expired` **writes** that to the latch rather than only labelling the
  payload, and `decide` records an expiry instead of the click once the run is
  no longer alive. Both orderings otherwise ended with a click landing on disk
  after the run died and the card reading "✓ Allowed" for a tool claude never
  ran (Bugbot, PR #308).
- **Side effect handled:** naming a permission-prompt tool also un-gates
  `AskUserQuestion` and `ExitPlanMode`, which the CLI otherwise disables in
  headless mode. This chat renders neither, so `--disallowed-tools` keeps them
  off — the change is about tool approvals and nothing else.

## Deliberate simplifications / tradeoffs (revisit later)

1. **"This reply", not "this session".** Each turn is a fresh
   `claude -p --resume`, and a `destination: "session"` rule lives in *that
   process* — verified: turn 2 asks again. So the second button is honestly
   labelled "Allow all X in this reply". Making it stick across turns means a
   grant store of our own (keyed by session id, replayed as `--allowedTools` on
   the next `--resume`) — deliberately not built here, because a durable,
   invisible, un-revokable grant is a bigger design call than this fix.
   Approvals are also not narrowed (allow-all-Bash, never `Bash(npm test:*)`),
   for the reason above.
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
6. **Only text turns — and approval cards — render.** A tool call that needs
   permission now shows up as a card (tool name + a per-tool summary: the Bash
   command, the Edit's `-`/`+` lines, the Write's content). An *allowed* one
   still streams past invisibly behind the "Working…" spinner, and cards are
   not in the transcript, so they vanish on a reload of a finished session.
   Showing all tool activity inline is still the obvious next feature.
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
11. **Tests stop at the CLI boundary.** `agent.py` shells out to a
    user-installed binary, so nothing here runs `claude`:
    `test_claude_agent_sidecar.py` covers the sidecar,
    `test_claude_permission_bridge.py` drives `permission_server.py` over its
    own stdio JSON-RPC and asserts the spawn line, and the registry test pins
    resolution (`.html` → `_render, code, claude`). What no test can catch is
    the CLI changing its side of the wire — the flag names, the result schema,
    or which tools a prompt tool un-gates.

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
