# Claude template — POC notes

**Status:** POC, 2026-07-08. Not a locked design; every choice below was made
for implementation simplicity and is expected to be revisited.
Origin: adapted from Sina's `ClaudeChat` sandbox POC (detached `claude -p`
subprocess + stream-json log + poll loop).

## What it is

A new built-in template `fused_render/templates/claude/` bound to `.html` /
`.htm` as a third mode (`["_render", "code", "claude"]`). Opening an HTML
file and switching to the `claude` mode gives a chat UI (claude.ai-style
landing + terminal-style chat) that talks to the local Claude Code CLI
**about that file**.

```
fused_render/templates/claude/
├── template.html   # chat UI; adapted from the ClaudeChat sandbox POC
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
    "sessions": [
      {"id": "<uuid>", "preview": "first user message…", "created_at": 1751…, "last_used": 1751…}
    ]
  }
  ```

  The first `poll` that sees the session id (from the `system` init row)
  writes the entry (atomic temp+`os.replace`; a `recorded` marker in the run
  dir keeps it one-shot). The landing page lists **only** these sessions —
  never the user's global `~/.claude` history.
- **Resume chains:** Claude Code mints a *new* session id on every
  `--resume`. `start` remembers `resumed_from`; the sidecar update replaces
  the old id in place (keeping `created_at`/`preview`), so one conversation
  stays one row instead of one row per turn.
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
   WebSocket (see D74 precedent).
3. **Sidecar is claimed, not reserved.** `<file>.json` may already exist as
   a user's own data file — agent.py tolerates non-conforming JSON (treats
   as empty) but a *save would clobber it*. No namespacing (`.fused-ai.json`
   or similar) yet; naming follows the requested shape literally
   (memory: URL/file shapes are literal).
4. **Sidecar races.** Atomic replace prevents torn writes, but two
   concurrent chats on the same file can lose one entry (last writer wins on
   read-modify-write). Accepted for POC.
5. **Session transcripts belong to Claude Code, not us.** The sidecar stores
   only ids; history is rebuilt from `~/.claude/projects/*/<id>.jsonl`. If
   the user deletes/cleans Claude Code data, the sidecar rows go stale
   (resume fails with claude's error; history shows empty). No pruning yet.
6. **Only text turns render.** Tool calls/diffs stream past invisibly (a
   "Working…" spinner phase is the only signal). Showing tool activity
   (edits made to the file!) inline is the obvious next feature.
7. **`claude` binary discovery:** `shutil.which` + three well-known
   fallbacks (`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`). The
   server env's PATH (Finder-launched .app!) may lack it; error message says
   what to do.
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

## Synergy worth noting

Claude edits `sample.html` → M4 auto-reload already live-refreshes the
`_render` view. A panel layout with `_render` on the left and `claude` mode
on the right (`/view/_panel?_layout=(…sample.html,…sample.html?_mode=claude)`)
is a working "live preview + AI pair-editing" surface with zero new code.
