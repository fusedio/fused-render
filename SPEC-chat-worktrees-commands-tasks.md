# SPEC — Claude chat: worktrees, slash commands, subagents / background tasks / monitors

**Status:** Locked for build — 2026-10-13. Owner decisions are final; implementers
build to the shapes below and raise deviations in their report, not in the code.

Three features for the native Claude chat (`frontend/src/apps/claude`,
`fused_render/claude_agent`). Each is independent; they share one contract
document so the field names agree.

The CLI this is written against is Claude Code 2.1.296 (`/opt/claude-code/bin/claude`
on the build box). Facts about it used here were read from its `--help` and its
bundle: the `init` system row carries `slash_commands` (and an optional
`terminal_slash_commands` subset to hide from non-terminal UIs), `agents` and
`skills`; a `commands_changed` system row says the list moved; `task_started` /
`task_progress` / `task_updated` / `task_notification` / `background_tasks_changed`
system rows describe background work, with `task_started` carrying `task_id`,
`description`, optional `subagent_type`, optional `is_backgrounded`, optional
`tool_use_id`; `task_progress` carries `usage {total_tokens, tool_uses,
duration_ms}`; `task_updated.patch.status` is one of `pending running completed
failed killed paused`; a `get_task_output` control request `{task_id}` returns a
shell or Monitor task's output; task types include `local_bash`, `local_agent`,
`monitor_mcp`, `monitor_ws`, `remote_agent`, `in_process_teammate`,
`local_workflow`, `mcp_task`; the Monitor tool is named `Monitor` and streams
each stdout line as a notification; the CLI's own worktrees live at
`<repo>/.claude/worktrees/<name>`.

---

## W — Worktrees

### W-1 Decision: fused-render owns the worktree, not the CLI flag
`claude --worktree` exists, but a chat here is keyed on its working directory
everywhere (session store munge, Tasks page rows, project queue keys, resume by
`_file`). A worktree created by git and opened as an ordinary DIRECTORY target
gets all of that for free; the CLI flag would move cwd under our feet and make
`--resume` ambiguous. So: `git worktree add` by us, then the chat's `_file`
becomes the worktree path. Location is the CLI's own convention,
`<repo-root>/.claude/worktrees/<name>`, so the CLI's `EnterWorktree` tool and
ours agree on where worktrees are. `.claude/worktrees/` is added to
`<repo>/.git/info/exclude` on first create (never to the tracked `.gitignore`).

### W-2 Backend module `fused_render/claude_agent/worktrees.py`
Stdlib + `git` subprocess only. Functions:

- `repo_root(path) -> str | ""` — the main repository root for any path INSIDE a
  repo or one of its linked worktrees (`git rev-parse --git-common-dir` →
  parent). "" when not a git repo or git missing.
- `list_worktrees(path) -> dict` → `{"root": <main root>, "current": <worktree
  path containing `path` or root>, "branch_current": <branch of current>,
  "relative": <path of the target under current, "" at the root>,
  "worktrees": [{"path", "branch", "head", "name", "managed": bool,
  "is_main": bool, "dirty": bool|null, "mapped": {"path", "exists"}}]}` from
  `git worktree list --porcelain`; `mapped` is the target carried into that
  worktree and whether it is on disk there.
  `managed` = under `<root>/.claude/worktrees/`. `name` = basename for managed
  ones, else the last path segment. `dirty` from `git status --porcelain` with a
  2 s timeout; `null` on timeout.
- `create_worktree(path, name, base="") -> dict` → `{"path", "branch"}`. Name
  rules: `^[A-Za-z0-9._-]{1,64}$`, not `.`/`..`. Branch is `name` if free, else
  `name-2`, `-3`…; base defaults to the current HEAD of `path`'s checkout.
  Command: `git worktree add -b <branch> <root>/.claude/worktrees/<name> <base>`.
  Refuses (returns `{"error"}`) when the target dir exists.
- `remove_worktree(path, name, force=False) -> dict` → `{"removed": path}` or
  `{"error"}`. Only MANAGED worktrees may be removed (anything outside
  `.claude/worktrees/` is a refusal). A dirty worktree is refused unless `force`.
  Uses `git worktree remove [--force]`; the branch is left alone.
- Every error is `{"error": "<one line>"}`; nothing raises past the module.

### W-3 Route
`POST /api/claude/worktrees` in `fused_render/server/routers/claude_agent.py`
(same `X-Fused` requirement, same error envelope as the agent route), body
`{action: "list"|"create"|"remove", file, name?, base?, force?}` — `file` is the
chat's `_file` (a path inside the repo). 200 = the function's dict verbatim.
Run the git calls in a thread (`asyncio.to_thread`) with a 20 s budget.

### W-4 Frontend
- `protocol/worktrees.ts`: `listWorktrees(file)`, `createWorktree(file, name,
  base?)`, `removeWorktree(file, name, force?)` over `postJson` with the chat's
  headers (reuse `agent.ts`'s `post` pattern; export a `WorktreeRow` type).
- `ui/WorktreeMenu.tsx` in the Topbar and on the landing card, for ANY
  target inside a repo, file or folder (hidden when `listWorktrees` says not
  a repo). A file chat switches to the SAME file in the other worktree
  (`list` maps the target into every worktree as `mapped: {path, exists}`),
  or to that worktree's root when the file is not on that branch. Trigger shows a branch
  icon + the current worktree name (`main` checkout shows the branch name).
  Menu: one row per worktree (name, branch, dirty dot), the current one marked;
  "New worktree…" opens an inline name field (Enter creates, Esc cancels; the
  default name is `claude-<yymmdd>-<hhmm>`); "Remove this worktree" on a
  managed, non-current-main worktree with a confirm step (dirty → the confirm
  says so and the second click forces).
- Switching = navigating the chat to the worktree path as its new `_file` with
  no `session_id`/`run` (the same navigation the Home lists use to open a folder
  chat; find it, do not invent a second URL builder). The composer's leave
  guard applies.
- Composer local command `/worktree [name]`: no arg opens the menu; `name`
  creates-or-switches.

### W-5 Tests
`tests/test_claude_worktrees.py`: real temp git repos (`git init`, one commit).
Cover: list on main; create → path/branch, `.git/info/exclude` gains the line
once; create twice with the same name → error; branch name collision → `-2`;
remove managed clean; remove dirty refused then forced; remove unmanaged
refused; non-repo path → `{"error"}`; route 403 without the header. Frontend:
bun tests for the menu's row model and the `/worktree` command parse.

---

## C — Slash commands

### C-1 What a slash command is here
A message whose first non-space character is `/` followed by a command token
`[A-Za-z0-9:_-]+`. Three kinds, decided client-side in
`protocol/commands.ts::classifyCommand(text, known)`:

1. **local** — handled by the chat UI, never sent: `/clear` (new conversation:
   drop `session_id`/`run`, same as the "New chat" affordance), `/help` (opens
   the command menu with the list), `/worktree` (W-4), `/model <name>`,
   `/effort <low|medium|high|max>`, `/tasks` (opens the tasks panel, T-5).
2. **cli** — sent verbatim as the user message; the CLI expands it (skills,
   `.claude/commands`, plugin commands, and built-ins that work headless such as
   `/compact`, `/context`, `/cost`, `/review`, `/init`).
3. **unknown** — not in the known list: still sent verbatim (the CLI replies
   that no such command exists; we do not second-guess it), but the menu shows
   no match.

### C-2 Backend: the command list
- `_poll` reads the `init` system row's `slash_commands`,
  `terminal_slash_commands`, `agents`, `skills` and writes them to
  `<run_dir>/capabilities.json` AND to the per-workdir state file
  `_state_file("commands", <munged workdir>)` (so the NEXT session in the same
  folder knows the list before its first turn). A `commands_changed` row
  triggers a re-read on the next poll (store `commands_stale: true`).
- New action `commands` `{file}` → `{"commands": [{"name", "description",
  "source"}], "agents": [...names], "stale": bool, "from": "cli"|"disk"}`.
  Source of truth is the state file when present (`from: "cli"`); otherwise a
  DISK SCAN (`from: "disk"`): `<workdir-repo>/.claude/commands/**/*.md`,
  `~/.claude/commands/**/*.md`, `<repo>/.claude/skills/*/SKILL.md`,
  `~/.claude/skills/*/SKILL.md` (name = file stem or folder; description =
  frontmatter `description:` if present, else the first non-empty line), plus a
  fixed table of headless-safe built-ins with descriptions (`compact`,
  `context`, `cost`, `review`, `init`, `clear`, `help`, `model`, `effort`,
  `worktree`, `tasks`). Names in `terminal_slash_commands` are dropped. The CLI
  list carries no descriptions: merge the disk scan's description by name where
  it has one, else "".
- `poll` payload gains `commands_changed: bool` (true on a window that held an
  `init` or `commands_changed` row) so the page re-fetches once.

### C-3 Frontend
- `protocol/commands.ts`: `classifyCommand`, `LOCAL_COMMANDS` table (name,
  description, argument hint), `filterCommands(list, query)` (prefix first,
  then substring, stable order), `fetchCommands(file)`.
- `ui/CommandMenu.tsx`: shown when the composer text matches `^/[^\s]*$` (only
  the first token typed, caret anywhere in it). Rows: `/name` bold, description
  muted, a `local` tag on local ones. ↑/↓ move, Enter/Tab accept (replaces the
  token with `/name ` — a space — and keeps the caret), Esc closes, click
  accepts. Max 8 rows visible, scrollable. Positioned above the composer. No
  menu while a turn is running? — it IS shown; sending still goes through the
  existing follow-up path.
- Composer send: `classifyCommand` first. `local` → run the handler, clear the
  box, never call `onSend`. Others → the existing send path untouched.
- Transcript: a user turn whose text is a slash command renders the first token
  as a chip (`.chat-cmd-chip`) with the rest as plain text; the assistant's
  `<local-command-stdout>` reply already reaches the page as text — it stays as
  is.
- The composer fetches the list on mount per `_file` and again when a poll
  reports `commands_changed`.

### C-4 Tests
Backend: `tests/test_claude_commands.py` — init row capture into both files;
`commands` action from state file vs disk scan (temp HOME via monkeypatch,
temp repo with `.claude/commands/a.md` with and without frontmatter, a skill
folder); terminal subset dropped; descriptions merged. Frontend bun tests for
`classifyCommand`, `filterCommands`, and the menu's keyboard model.

---

## T — Subagents, background tasks, monitors

### T-1 The task registry (backend)
`<run_dir>/bg_tasks.json` becomes `{"<task_id>": TaskRecord}` where

```
TaskRecord = {
  "id": str, "description": str,
  "kind": "shell" | "agent" | "monitor" | "other",   # from task_type / subagent_type / tool name
  "type": str,              # the raw task_type when the row carries one, else ""
  "subagent_type": str,     # "" unless an agent
  "tool_use_id": str,       # "" unless known — links to the Task/Agent/Monitor/Bash tool segment
  "status": "running" | "completed" | "failed" | "killed" | "paused" | "pending",
  "started_at": float, "ended_at": float | null,     # wall clock, seconds
  "backgrounded": bool,
  "usage": {"total_tokens": int, "tool_uses": int, "duration_ms": int} | null,
  "error": str, "summary": str,                      # summary = last task_notification summary
  "events": int                                      # count of notifications seen (monitor lines)
}
```
Kind rule: `subagent_type` set or type `local_agent`/`remote_agent`/
`in_process_teammate` → `agent`; type `monitor_*`, or the linked tool_use is
`Monitor` → `monitor`; type `local_bash` or linked tool `Bash` → `shell`; else
`other`. Rows: `task_started` creates (status `running`); `task_progress`
updates `usage`; `task_updated.patch` merges `status`/`description`/`error`/
`is_backgrounded`; `task_notification` sets `summary`, bumps `events`, and
ends the task (status `completed` unless it says otherwise) ONLY when the
notification's status is terminal — a monitor's line notifications keep it
running; `background_tasks_changed` marks anything not in its list and still
`running` as `completed`. Ended records stay in the file for the run.

Payload: `activity.tasks` stays `[{id, description}]` for RUNNING tasks (back
compat, `tasks_pending` unchanged) and gains nothing. NEW top-level poll field
`tasks: TaskRecord[]` — every record, running first then by `started_at`.
`_history` returns the same `tasks` for the last run of the session when its
run dir still exists (else `[]`).

### T-2 Subagent transcript nesting (backend)
`_segments_from_rows` currently skips rows with `parent_tool_use_id`. Instead:
group those rows by `parent_tool_use_id`, build each group's segments with the
SAME reader (one level; a subagent's own subagents stay flattened into its
group), and attach them to the parent `tool` segment as `children: Segment[]`.
A parent whose tool_use is not in this window (started before the cursor)
gets a synthetic tool segment `{type: "tool", name: "Agent", id: <parent id>,
detail: "", children, orphan: true}` appended at the end so no subagent output
is lost. Thinking segments inside children are kept. `tool` segments for
`Task`/`Agent`/`Monitor`/backgrounded `Bash` gain `task_id` when the registry
links them (`tool_use_id`).

### T-3 Task output (backend)
New action `task_output` `{run_id, task_id}` → sends the `get_task_output`
control request through the session host (`_write_control_request` +
`_await_control_response`, 10 s) and returns `{"output": str, "status": str,
"truncated": bool}`; `{"error"}` when no live host. Output capped at 64 KiB
(tail), `truncated: true` when cut. Add to the router's `ACTIONS` with a 15 s
budget.

### T-4 Types (frontend `protocol/types.ts`)
`TaskRecord` as above; `PollResponse.tasks?: TaskRecord[]`;
`HistoryResponse.tasks?: TaskRecord[]`; `Segment` tool variant gains
`children?: Segment[]`, `task_id?: string`, `orphan?: boolean`. Add
`"task_output"` and `"commands"` to `Action`/`AgentRequests`/`AgentResponses`.

### T-5 UI
- `ui/TasksPanel.tsx`: a collapsible strip between the transcript and the
  composer (`.chat-tasks`), shown whenever `tasks` is non-empty for the current
  run, defaulting to collapsed with a one-line summary ("2 running · 1 done":
  running counts by kind: "1 agent, 1 shell, 1 monitor"). Expanded: one row per
  task — kind icon, description, status pill, elapsed (live for running),
  usage for agents (tokens · tool uses), `events` count for monitors, last
  `summary`, and an "Output" button for shell/monitor tasks (calls
  `task_output`, shows it in a `CollapsiblePre`). Ended tasks sort below
  running. `/tasks` toggles it open.
- Agent tool chip (`ui/ToolChip.tsx` / `SegmentView.tsx`): a tool segment with
  `children` is expandable; expanded it renders the children with the SAME
  segment renderer indented in a `.chat-subagent` block, and the chip's header
  shows the subagent type, live status (from the linked task record) and usage.
  Orphan parents render as "Subagent (started earlier)".
- WorkingLine: when running tasks exist, the verb names them: "Running a
  subagent", "Running 2 subagents", "Watching a monitor", "Running a background
  command" (agents first, then monitors, then shells); the quiet-wait line
  keeps its wording.
- Monitors: a `Monitor` tool segment shows "watching" while its task runs and
  the events count.

### T-6 Tests
Backend: `tests/test_claude_tasks_registry.py` — the row sequence for each
kind through `_poll` (using the existing fake-rows pattern in
`tests/test_claude_bg_tasks_persist.py`), status transitions, monitor
notifications keep it running, `background_tasks_changed` reconciliation,
persistence across cursor advances; `tests/test_claude_agent_segments.py` gains
nesting cases (children attached, orphan parent synthesised, grandchildren
flattened). `task_output` through the control-request fake in
`tests/test_claude_control_requests.py`'s style. Frontend bun tests for the
summary line, sort order, and the chip's expand model.

---

## X — Cross-cutting
- Docs: a `## D13xx` entry per feature in `DECISIONS.md` (W, C, T) with the
  rejected alternatives above; `frontend/src/apps/claude/README.md` lists the new
  files; the router docstring's action count moves.
- Checks before done: `cd frontend && npm run typecheck && npm run
  check:boundaries && bun test`; `pytest -n0 tests/test_claude_*.py`.
- No new dependencies. No emoji in UI strings (`ui/no-emoji.test.ts` enforces).
- `apps/claude` imports platform only (`scripts/check-boundaries.mjs`).
