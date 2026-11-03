# Claude chat "Breakout" — plan

Worktree: `/Users/iamsdas/Work/fused-render-breakout`, branch `claude-chat-breakout` (from main 08ea4ebdf).
Append a **Decisions log** section at the bottom of this file as you go (what you chose, dead ends, anything this plan got wrong). Do not start `scripts/dev.sh`; the user runs it. Use `bun`, never npm/npx.

## Goal

Let the user fork the current Claude chat into an ephemeral "breakout" side conversation that can research with skills, MCP and read tools but cannot build, then hand a summary back to the main chat's composer.

Approach: reuse the existing per-message fork path (`--fork-session`), add a tip-fork variant, mark the run `breakout` so the server's permission bridge refuses edits and shell (except markdown under the chat folder's `.claude/handoffs/`), hide it from the Tasks list and the folder queue, and host it in a pinned rail-style card beside the chat with a "Send back" that drafts into the parent composer.

## Scope (settled; do not widen)

- Trigger: a Breakout button on every user turn beside Fork, plus a composer Breakout button that forks from the latest message. Works while the main chat is running.
- Card: a pinned, resizable rail-style card; several may be open at once; shell-level so navigation does not unmount it.
- Powers: seeded with parent context up to the chosen message; skills, MCP, Read/Grep/Glob, web allowed. Write/Edit/MultiEdit/NotebookEdit and every Bash call are refused outright with no approval card. Exception: `.md` files under `<chat folder>/.claude/handoffs/` may be written/edited.
- Lifetime: never in the fused-render Tasks list; never takes the folder queue slot. The Claude transcript on disk stays.
- Send back: puts a draft (last assistant reply, plus "Handoff file: <absolute path>" when one was written) into the parent chat's composer, not auto-sent; card closes. Close without Send back → confirm → discard (card only; transcript stays).
- Out of scope: non-Claude backends (copilot/codex → refuse like fork does), read-only shell, auto-send, nesting (no Breakout buttons inside a breakout), non-md or out-of-folder files.

## Tasks

### Task 1 — Backend: breakout start, tip fork, hidden from Tasks and the queue

Test first in `tests/test_claude_fork.py` (fixtures `agent`, `start`; pattern `test_argv_fork_flags`, `test_start_fork_resumes_at_parent`, `test_start_fork_missing_uuid_refused`, `test_start_fork_copilot_refused`), `tests/test_tasks_store.py` (pattern: the deleted.json tests), `tests/test_tasks_api.py` (pattern: whatever test covers `_deleted`/`deleted.json` filtering of `/api/tasks`), `tests/test_run_folder_gate.py` (pattern `test_the_chats_own_run_is_never_refused`:147).

- `fused_render/claude_agent/agent.py`
  - `_claude_argv` (~3770): add a `breakout: bool = False` kwarg. When breakout: (a) the `--allowed-tools` list (~3855-3865) drops `Bash(fused:*)`; (b) `--permission-mode` is forced to `default` regardless of `cli_mode` (~3883), so every Write/Edit/Bash goes through the approve tool; (c) a new fork branch for "fork at tip": `session_id and new_session_id and not resume_at` → `--resume <sid> --fork-session --session-id <new>` (no `--resume-session-at`), placed before the existing `elif session_id:` at ~3895. Keep the existing `resume_at` branch (3885-3894) for per-message breakouts.
  - `_appended_prompt` (~3921): when breakout, append one paragraph telling Claude it is a research-only breakout of the main chat, that edits and shell are refused by policy, that it may write a markdown handoff under the exact absolute `.claude/handoffs/` directory, and that the user will send a summary back to the main chat.
  - `_start` (~4829): add `breakout: bool = False` param. When breakout: require `backend == "claude"` (refuse otherwise, same shape as the copilot fork refusal), require `session_id`; if `fork_at` is empty, fork at tip (skip `_fork_point`, do not raise `fork_target_missing`); mint `new_session_id` as the fork block does. Run meta (~4995-5045) gets `"breakout": True`. Pass `breakout` to `_claude_argv`, and set env `FUSED_BREAKOUT_HANDOFF_DIR=<abs chat folder>/.claude/handoffs` on the MCP server env block in `_write_mcp_config` (~2166-2193) the same way `FUSED_PERM_ALLOW_FILE` is passed (thread it via whatever parameter `_write_mcp_config` already receives from `_start`). After the run record is written, call `tasks_store.mark_breakout(<transcript path>)` (see below) so the Tasks scan can hide it even if meta is not consulted.
  - `main` (~12562): accept `breakout` (string "1"/"" from params, like `fork_at`) and forward to `_start` at ~12591.
- `fused_render/server/routers/claude_agent.py` `agent_dispatch` (~322): read `params.get("breakout")` and forward it to `main` wherever `fork_at` is forwarded. Sibling: if there is a second dispatch path that forwards `fork_at` (grep `fork_at` in this file), change it too.
- `fused_render/claude_agent/gate.py` `_folder_busy` (~221): return `""` immediately when `params.get("breakout") == "1"`; a breakout never claims or waits on the folder slot.
- `fused_render/tasks_store.py`: add `BREAKOUTS_FILE = "breakouts.json"`, `breakout_state()`, `is_breakout(path)` and `mark_breakout(path)` following the `DELETED_FILE` trio at ~930-963 (`load_state`, `_update`). Key by the same transcript identity `deleted.json` uses.
- `fused_render/server/routers/tasks.py` `_collect` (~3091): add `and not _breakout(task, breakouts)` to the final filter at ~3184-3186, loading `breakouts = tasks_store.breakout_state()` next to `deleted`. Write `_breakout` beside `_deleted` (~3288) with the same signature. Also check the run meta `breakout` flag in `_is_task` (~3055) if meta is available there, so a stale `breakouts.json` is not the only guard.

Verify: `.venv/bin/python -m pytest tests/test_claude_fork.py tests/test_tasks_store.py tests/test_tasks_api.py tests/test_run_folder_gate.py -n 0 -q` → all pass, new tests included.
Commit: `Claude chat: breakout start forks the session, hides the run from Tasks and the queue`

### Task 2 — Permission bridge: refuse edits and shell in a breakout, allow handoff markdown

Test first in `tests/test_claude_permission_bridge.py` (fixtures `agent`, `server(tmp_path)`; helpers `_Server(perm_dir, env)`, `_wait_for_request`, `_result_payload`). Cases: Bash denied with no `perm/*.req.json` written; Write to `<handoff>/x.md` allowed without a card; Write to `<handoff>/x.txt` denied; Edit to `<handoff>/../other.md` (path escapes after `os.path.realpath`) denied; Write anywhere else denied; Read/Grep/WebFetch untouched (fall through to `_chat_allows`); with the env var unset nothing changes.

- `fused_render/claude_agent/permission_server.py`
  - Read `FUSED_BREAKOUT_HANDOFF_DIR` once at import next to the other env reads (~100-115).
  - `_handle_approve` (~770): before `_chat_allows`, when the env var is set, apply `_breakout_policy(tool_name, tool_input)`: returns an allow result for Write/Edit/MultiEdit/NotebookEdit whose `file_path` (or `notebook_path`) realpath is inside the handoff dir and ends with `.md`; a deny result with a one-line message ("Breakout chats are research-only; edits and shell are refused. Markdown under <dir> is the one exception.") for those tools elsewhere and for every `Bash` call; `None` for everything else (normal flow). Build results with `_permission_result` (~562). Add the new tools to nothing else; `_NEVER_AUTO_ALLOWED` (~747) stays as is.
  - A denied call must not `_register_inflight`, `_post_card_raised` or write a req card.

Verify: `.venv/bin/python -m pytest tests/test_claude_permission_bridge.py -n 0 -q` → pass.
Commit: `Permission bridge: breakout policy refuses edits and shell, allows handoff markdown`

### Task 3 — Frontend: controller + Breakout triggers in the transcript and composer

Test first in `frontend/src/apps/claude/protocol/run-controller.fork.test.ts` (add breakout cases next to the fork ones) and the existing Turn/Transcript tests if any pin the fork button (grep `turn-fork` under `frontend/src`; memory says tests pin literal source substrings, so keep the existing fork button markup byte-identical).

- `frontend/src/apps/claude/protocol/controller-api.ts`: add `breakout?: boolean` to the controller deps/options (next to where `fork` state lives at ~381) and a `breakoutAt?: string` start option; export a `BreakoutRequest { sessionId: string; file: string; forkAt?: string; forkKey?: string }` type for the card store.
- `frontend/src/apps/claude/protocol/run-controller.ts` start path (~2640-2720): when the controller is in breakout posture, skip the `live_host` probe at ~2645 (a breakout must never `send` into the parent's live run), skip queue admission / `queue_claim`, and add `breakout: "1"` plus `fork_at` (empty string allowed, meaning tip) to the start body. The `fork spent` reset at ~2788 stays. The breakout's own later messages go through the normal `send` to its own run.
- `frontend/src/apps/claude/ui/Turn.tsx` (~96-130 props, button at ~203-213): add `onBreakout?: (turnKey: string) => void`; render a second button `turn-breakout` (`aria-label="Breakout from here"`, `data-hint` same, a lucide icon such as `ExternalLink` or `Split`) immediately after the fork button, only when `onBreakout` is given. Do not alter the fork button line.
- `frontend/src/apps/claude/ui/Transcript.tsx` (~115-116, 175, 964): thread `onBreakout` exactly as `onFork` is threaded.
- `frontend/src/apps/claude/ClaudeChat.tsx`: new prop `breakout?: boolean` (card posture) and `onBreakout?: (req: BreakoutRequest) => void`. Pass `onBreakout` to Transcript (next to `onFork` at ~5420) when `chatBackend === "claude"`, when a session id exists, and when not already in breakout posture; unlike fork, do NOT gate on `!running`. Each click resolves the turn's uuid the same way `beginFork` does in run-controller.ts ~1033 (reuse that lookup, do not copy it) and calls `onBreakout({sessionId, file, forkAt: uuid})`. Add a composer Breakout button (near the existing composer action buttons; find where the model/effort/permission controls render) that calls `onBreakout({sessionId, file})` with no `forkAt`. Hidden when `breakout` posture or no session.
- `frontend/src/apps/claude/ChatMount.tsx` (props 159-246): add `breakout?: boolean` and `onBreakout?` passthrough to `ClaudeChat` (~296-320); breakout posture forces `paramsSource: "memory"` seeding `session_id` via `useHostIds` and passes `breakout` into the controller deps.

Verify: `cd frontend && bun test src/apps/claude/protocol/run-controller.fork.test.ts src/apps/claude/ui` (narrow to the touched test files) and `bunx tsc --noEmit` from `frontend/` → pass.
Commit: `Claude chat: Breakout buttons on turns and the composer; controller starts a breakout run`

### Task 4 — Frontend: shell-level breakout cards with Send back and discard

Test first: a bun test for the store (`frontend/src/shell/breakout/breakouts.test.ts`: open adds a card with a unique id, close removes, sendBack dispatches the composer event with the draft text and clears the card) and one for the draft builder (last assistant text + detected handoff path → expected draft string).

- New `frontend/src/shell/breakout/breakouts.ts`: tiny store (`useSyncExternalStore` pattern used by other shell stores; grep `useSyncExternalStore` under `frontend/src/shell` and follow one) holding `{ id, sessionId, file, forkAt?, parentSessionId }[]`, `openBreakout(req)`, `closeBreakout(id)`, and `buildHandoffDraft(messages, handoffDir)` which takes the breakout transcript's last assistant text and, if any Write/Edit tool_use targeted a path under `handoffDir`, appends a line `Handoff file: <absolute path>`.
- New `frontend/src/shell/breakout/BreakoutCards.tsx`: renders one card per store entry. Card chrome copies the rail peek card: use `peekPlacement`, `dragResize`, `PEEK_WIDTH`, `PEEK_MAX_HEIGHT` from `frontend/src/shell/claude-rail/rail-peek-lib.ts` and the classes in `frontend/src/styles/claude-rail.css` (add a `breakout-card` modifier there, no new stylesheet). Cards stack with a fixed offset per index. Body is `<ChatMount file sessionId breakout chatOnly peek headerInPeek paramsSource="memory" />` with `headerExtra` holding two buttons: **Send back** and **Close**. Mount `<BreakoutCards />` once in `frontend/src/shell/App.tsx` beside the other shell overlays (~1274-1385).
- Send back: read the breakout's transcript via the controller/ChatMount `onReady` handle (expose the messages the way RailPeek or TaskPeek reads them; grep `onReady` in `RailPeek.tsx`), call `buildHandoffDraft`, then dispatch a `window` CustomEvent `fused:composer-draft` `{ sessionId: parentSessionId, file, text }` AND call `saveChatDraft(chatDraftKey(parentSessionId, file), text)` from `frontend/src/platform/lib/drafts.ts` (~205, ~720) as the persisted fallback; then `closeBreakout(id)`.
- `ClaudeChat.tsx`: listen for `fused:composer-draft` whose `sessionId` matches this chat's session (and `file` matches); set the composer text through the same path `draftSyncer(...).setText` uses (~4549) so the textarea and draft stay in sync; append to existing text with a blank line between if the composer is not empty.
- Close: if the breakout has at least one user message, `window.confirm("Discard this breakout? The chat is not sent back.")` before `closeBreakout`; otherwise close directly.
- `ClaudeChat.tsx` wiring: the explorer-level `ClaudeChat`/`ChatMount` instances (grep `ChatMount` and `ClaudeRail` in `frontend/src/apps/explorer/Preview.tsx` ~95-101 and the side-pane chat mount) pass `onBreakout={openBreakout}`. Breakout cards pass no `onBreakout` (no nesting).

Verify: `cd frontend && bun test src/shell/breakout` and `bunx tsc --noEmit` → pass. Also `bun run build` once at the end of this task (the CSS edit; a `*/` in comment prose breaks only the build).
Commit: `Claude chat: breakout cards with Send back into the parent composer`

## Decisions & risks

- Enforcement is server-side in the permission bridge, not in the CLI flag list: `--disallowed-tools` cannot express "md under one dir", and `auto`/`acceptEdits` would bypass the approve tool, hence forced `default` mode.
- Dropping `Bash(fused:*)` and appending a breakout prompt paragraph changes the tool roster, which is a prompt-cache miss on the forked session (headsup.py's comment). Accepted; a breakout is a fresh session anyway.
- A breakout must never attach to the parent's live run: the controller skips the `live_host` probe and queue admission, and the card seeds only `session_id` (never `run`).
- `breakouts.json` mirrors `deleted.json` so the Tasks scan stays meta-blind; the meta flag is a second guard, not the primary one.
- Send back uses an in-page event plus a persisted draft fallback because the parent chat may be inside a different mount (side pane vs. rail) than the card.
- Per-message breakout uses the existing `resume_at` fork path; only the tip fork is new on the CLI side.
- Not done: Breakout for copilot/codex backends, a Breakout entry in the Tasks list, nested breakouts, read-only shell.

## How we'll know it works

1. In a Claude chat with a session, every user turn shows Fork and Breakout; the composer shows a Breakout button while the chat is running.
2. Clicking either opens a card beside the chat whose transcript is the parent's up to that message; asking it a question answers in the card; the main chat is untouched and still runnable.
3. Asking the breakout to run a shell command or edit a file produces an immediate refusal in-chat with no approval card; asking it to write `.claude/handoffs/notes.md` succeeds without a card.
4. `/api/tasks` does not list the breakout; the parent still shows.
5. Send back closes the card and the parent composer contains the summary and the handoff path; nothing is sent.
6. Close on a used breakout asks to confirm, then the card is gone.

## Decisions log

### Task 1
- `agent.py` cannot import `tasks_store` (template outside the package import graph), so the plan's "call `tasks_store.mark_breakout` after the run record is written" moved to the router: `routers/claude_agent.py::_mark_breakout` files the session from the start's answer (normal path and the late-start path). Keyed by SESSION id (what `deleted.json` uses), not transcript path.
- The Tasks scan has no cheap access to run metas, so the plan's second guard (meta `breakout` flag in `_is_task`) was not added; `breakouts.json` is the only guard. The meta flag is still written (diagnosis, future use).
- Added a `breakout="resume"` value beside `"1"` (plan said only "1"/""): a later turn of an existing breakout whose host died restarts through `start` with the breakout's own session id, and a plain `"1"` would fork the breakout again. `"resume"` keeps the policy (default mode, no `Bash(fused:*)`, handoff env, prompt note) but continues the session. The frontend controller must send `"1"` on the first start and `"resume"` for later starts. `gate._folder_busy` and `_file_owner` skip both values (sends carrying `breakout` are also exempt from the queue).
- `session_host.py` needed two edits the plan did not list: it builds argv from the launch request (`breakout` is now in `req`), and `own_sid` treated only `resume_at` as a fork, so a tip fork would have reported the PARENT's id as its own.
- `headsup.py` left alone (it appends its own `--permission-mode dontAsk`).
- Handoff dir = `<_workdir(file)>/.claude/handoffs` (`agent._breakout_handoff_dir`).

### Task 2
- Policy is `permission_server._breakout_policy(tool_name, tool_input, handoff_dir=None)`, run first in `_handle_approve` (before `_chat_allows`, so an "always approve" grant cannot widen a breakout). The allow result is built inline in the same shape as `_permission_result`'s plain allow rather than through it (it needs a decision dict with no scope, and the deny has a fixed message).
- Suite-wide: the file runs ~30 s in whole; the 9 new red tests took 93 s before the fix because each waited the 10 s response timeout.

### Task 3 (frontend controller + triggers)
- Per-message breakout hides turn X and everything after it (same as the existing fork's `--resume-session-at` semantics); implemented by reusing `state.fork` so `fork_at` is read from the parked uuid.
- Controller breakout posture: `deps.breakout`/`deps.breakoutAt`; first start sends `breakout:"1"`, no live_host probe, never adopts the parent's live run; once spent, every start/send carries `breakout:"resume"`. A failed first start leaves it unspent.
- `ClaudeChat` skips queue admission and the fork notice in breakout posture; `onBreakout` is available while a run is active (unlike `onFork`).
- Breakout button hangs one lane left of the fork button (net zero flex width).
