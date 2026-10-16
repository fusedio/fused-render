# Copilot backend — build contract

GitHub Copilot CLI as a second agent for **Tasks and Sessions** (not Bots).
Design settled 2026-10-16 with the owner (decisions D1–D18 below). This file is
the contract every workstream codes against; the design page it condenses is
the owner's artifact and is not in the repo.

Verified live against Copilot CLI 1.0.98 on 2026-10-16: everything in §Verified.
Anything not listed there is an assumption and is marked.

## 0. Verdict and seam

Seam = **the host process and the row shape it writes**, one level below the
~20 private `agent.py` functions (`_start/_send/_poll/_decide/_cancel/_live_*/
_permissions/_alive/_history`) that `routers/tasks`, `schedule`, `queue_manager`,
`project_queue`, `tasks_watch`, `sysmon` already treat as the contract — together
with the run-dir files they read directly (`meta.json`, `session`, `pid`,
`host.json`, `out.jsonl`, `perm/*.req.json|.res.json`, `inbox/`).

A new `claude_agent/copilot_host.py` speaks ACP to `copilot --acp` and writes
**the same run-dir files and the same stream-json rows** `session_host.py`
produces for Claude. `_poll`, `_segments_from_rows`, `_turn_state`, `_history`,
the tasks scan, the queue and the watchers are not rewritten.

Outside the host, what changes: a `backend` field wherever task identity is
stored; a root LIST where readers hard-code `~/.claude`; two Preferences
toggles; an Agent control + the agent named on every surface; a Copilot
health/install/login twin of the Claude one; a catalog endpoint.

## 1. Code already in the tree (the API everyone codes against)

### `fused_render/templates/shared/agent_roots.py` (stdlib, loaded by path)

```
BACKENDS = ("claude", "copilot"); DEFAULT_BACKEND = "claude"
claude_dir() -> ~/.claude (CLAUDE_CONFIG_DIR wins)
copilot_dir() -> <FUSED_RENDER_HOME|~/.fused-render>/copilot      # UNNESTED home, never branch-resolved
projects_dir(b), sessions_dir(b), base_dir(b)
transcript_roots() -> [("claude", .../projects), ("copilot", .../projects)]   # ALWAYS both
registry_roots()   -> [("claude", .../sessions), ("copilot", .../sessions)]
munge(path)        -> Claude's bucket rule (re.sub [^A-Za-z0-9] -> "-")
backend_for_transcript(path) -> "claude" | "copilot"   (by root; claude when under neither)
find_transcript(sid, workdir=None) -> (backend, path) | None   (exact bucket first, then glob all roots)
backend_for_session(sid, workdir=None, hint=None) -> hint if known, else by root, else "claude"
```

`agent.py`, both hosts, `permission_server.py` import it with the existing
`_SHARED` sys.path idiom (`from agent_roots import ...`). App modules import it
as `from fused_render.templates.shared import agent_roots` is NOT allowed (shared
files are not a package); app modules load it the way `claude_spawn.load_agent`
loads agent.py, or — simpler and what WS-A does — through
`fused_render/agent_backends/roots.py`, a thin app-side re-export (WS-A writes
it: `importlib.util.spec_from_file_location` on the shared file, exposing the
same names).

### `fused_render/agent_backends/` (app-level; agent.py never imports it)

```
base.py      Backend(id, label, models, default_model, efforts, modes, caps: Caps,
                     account_error_markers, limit_error_markers, terminal_resume(cwd, sid) -> str,
                     health() -> dict | None, routes_prefix, pref_key)
             Caps(plan_mode, live_model_switch, live_mode_switch, snapshots, artifacts,
                  worktrees, bg_tasks, ask_user, terminal_mirror, mid_turn_messages)
claude.py    CLAUDE  (all caps True; models fable/opus/sonnet/haiku; modes plan/prompt/acceptEdits/auto)
copilot.py   COPILOT (caps: live_mode_switch only; models auto/gpt-5.4/...; modes prompt/acceptEdits/auto)
registry.py  get(id) (unknown/empty -> CLAUDE), is_known, enabled_ids() (never empty), is_enabled,
             default_id(), disabled_error(id) -> 400 body, catalog(with_health=True) -> payload below
```

### `fused_render/shell/prefs.py` (read side done; WS-A does the PUT/GET)

```
AGENT_BACKEND_KEYS = {"claude": "claude_enabled", "copilot": "copilot_enabled"}
backend_enabled(b): claude -> stored is not False; copilot -> stored is True
claude_enabled(), copilot_enabled(), default_backend() -> "claude" unless stored valid
```

## 2. Decisions (owner-approved)

- **D1** seam as above. Rejected: neutral row schema; adapter behind the 20 functions.
- **D2** `backend` explicit field, per task, chosen at first turn, **immutable**.
  Lives in: run `meta.json` + `host.json`; `session_settings.json` record
  `{sid: {model, effort, backend}}`; scheduler entry (beside `model`/`effort`);
  task row on the wire; `start`/`send`/`/api/tasks/create`/`/api/schedule` bodies;
  the task row derives it from the transcript root at scan time. **Session id:**
  Claude's is fused-minted (`--session-id uuid4`). Copilot's is **Copilot's own**,
  from the `session/new` reply: `--acp` and `--session-id` are mutually exclusive
  (verified 2026-10-16: the CLI refuses to start). So a fresh Copilot `_start`
  returns `session_id: ""`, and the id is learned from the host's init row by
  `_poll`, the same path Claude used before `--session-id`. The settings record is
  written when that id is first recorded. There is **no task table**.
- **D3** Copilot transcript is **written by the host**, Claude transcript shape,
  under `copilot_dir()/projects/<munge(cwd)>/<sid>.jsonl`; live registry under
  `copilot_dir()/sessions/<pid>.json` (`{sessionId, cwd, status: busy|idle,
  updatedAt, pid}`). Readers iterate `transcript_roots()` / `registry_roots()`.
  Rejected: converter over Copilot's `events.jsonl` (finals only; readers are
  seek/offset based). Turns taken in a terminal are backfilled from the
  `session/load` replay on the next fused attach (host diffs replayed user turns
  against its transcript and appends the missing ones).
- **D4** Permissions: host handles `session/request_permission` through the
  existing `perm/<id>.req.json` / `.res.json` files (exact shapes: read
  `permission_server.py` `_handle_approve`, `_await_decision`, `_read_decision`
  and `agent.py` `_permissions`/`_decide`). allow once → `allow_once`; scope
  `session` → `allow_always`; deny/expired/cancelled/timeout → `reject_once`.
  `permission_server.py` stays for pane tools only (`app_state`, `terminal_*`),
  launched with `--additional-mcp-config @run_dir/mcp.json` and a new
  `--no-approve` argv flag that drops the `approve` tool. **No hooks.**
- **D5** Modes: `prompt` → ACP mode `agent` + `allow_all=off`; `acceptEdits` →
  agent + spawn `--allow-tool write`; `auto` → agent + `allow_all=on`; `plan` →
  not offered for Copilot. Live: `allow_all` via `session/set_config_option
  {configId:"allow_all", value:"on"|"off"}`; acceptEdits needs respawn. A card
  allow carrying mode `auto` → `set_config_option allow_all=on` after the RPC
  reply. A card allow carrying `acceptEdits` → `allow_always` on that card only,
  never `allow_all`; "accept edits from here" must not also let shell run unasked.
- **D6** Host **serializes prompts**: a second `session/prompt` cancels the
  first (verified). User rows arriving mid-turn are HELD (left in `inbox/`, so
  the UI shows them undrained) and sent after the turn's `result`. `interrupt`
  control requests are never held → `session/cancel` immediately.
- **D7** `--model`, `--reasoning-effort` at spawn only. `_send` returns
  `{respawn: True}` when model differs and `caps.live_model_switch` is False (same
  path effort already takes); new host resumes with `session/load`.
- **D8** System prompt via generated `run_dir/plugin/{plugin.json,
  agents/fused.agent.md}` + `--agent fused:fused`; `skills/` symlinked from the
  fused skill plugin (and workbench plugin for canvases). Slash commands from
  `available_commands_update` go into the `system/init` row.
- **D9** Shots + `extra_read_dirs` → `--add-dir`; fused CLI → `--allow-tool
  'shell(fused:*)'` + PATH prefix. Builtin GitHub MCP stays on. No shell-prefix
  terminal mirror (`claude_cmd_log`) for Copilot.
- **D10** `copilot_health.py` + `copilot_login.py` + `routers/copilot_health.py`
  under `/api/copilot/{health, health/refresh, install, login, login/cancel}`,
  same snapshot keys as `claude_health.summary()`. Signed-in probe = spawn
  `copilot --acp --disable-builtin-mcps`, `initialize`, `session/new`; an
  JSON-RPC error `-32000 "Authentication required"` (verified) → signed out.
  **gh CLI as a fallback login (Q5, owner: yes), two-step:** the health probe
  first runs WITHOUT an injected token (stored `copilot login`, or a token the
  user exported). Only if that reads signed out, and `gh auth token` yields a
  token the user did not already export, a second probe runs with it as
  `COPILOT_GITHUB_TOKEN`. `account.method` records which worked: `copilot` |
  `token` | `gh`. The chat host injects the gh token **only** when the cached
  snapshot says `gh`, and never overrides a user-set `COPILOT_GITHUB_TOKEN`, so
  the snapshot is the authority and a working `copilot login` is never
  overridden by a gh account. A probe that times out keeps the previous
  snapshot's `account` and reports `signed_in: null`, never "Sign in".
- **D11** `GET /api/agent-backends` → `registry.catalog()`:
  `{"backends":[<enabled, in order>], "default": id, "all":[<every backend>]}`,
  each `{id,label,enabled,models[{id,label}],default_model,efforts,modes,caps{...},health}`.
  Frontend model/effort/mode vocab reads THIS, replacing the hand-synced copies.
- **D12** no-equivalent table (see §caps): AskUserQuestion → none (prose);
  bg tasks → none; snapshots/artifacts/worktrees → hidden; recap → `copilot -p -s
  --model auto`; terminal continue → `copilot --resume=<sid>`; plan → hidden;
  commit subject `"Copilot: <msg>"`; `entrypoint` always `sdk-cli`.
- **D13** errors per backend: JSON-RPC error on prompt / credits exhausted →
  `assistant` row `isApiErrorMessage:true` + `result is_error`. Markers in
  `Backend.account_error_markers` / `limit_error_markers`.
- **D14** tool names: ACP `kind` → `execute`=Bash{command,description},
  `edit` new file=Write{file_path,content} / existing=Edit{file_path,diff},
  `read`=Read{file_path}, `fetch`=WebFetch{url}, `search`=Grep{pattern,path},
  MCP `fused_approvals-app_state` → `mcp__fused_approvals__app_state`, else the
  ACP `title`. Every input also carries `_acp: {kind, title}`.
- **D15** erase removes the fused transcript + `~/.copilot/session-state/<sid>/`
  (Q6 owner: yes); relocate moves the fused transcript bucket only.
- **D16** ACP only for sessions; `-p` only for the recap one-shot.
- **D17** two Preferences toggles `claude_enabled` (default on), `copilot_enabled`
  (default off). PUT refuses clearing the last enabled (400 "turn on the other
  agent first"); `default_backend` re-points in the same write. Disabled agent:
  omitted from `catalog().backends`; `backend` naming it → **400** on
  `start`/`send`/`/api/tasks/create`/`/api/tasks/{key}/send`/`/api/schedule`;
  scheduler tick marks such an entry `error` reason "<agent> disabled"; existing
  sessions stay listed, open read-only, composer shows one line + Preferences
  link. Claude-only surfaces following `claude_enabled`: explorer "open in
  terminal", recap for Claude sessions, `claude_health.warm_in_background`.
  NOT gated: onboarding, Claude Config page, `fused.ai` claude provider, Bots
  (Q7 owner: leave bots, say so in the toggle copy).
- **D18** agent always named: **Agent pill before the model pill** (hidden when
  only one backend enabled, but the agent is still named everywhere); fixed label
  once a session has a turn; top bar title/mark; placeholders "Ask Copilot…";
  working line "Copilot is thinking"; cards "Copilot wants to use…"; first-turn
  notice row "Handled by GitHub Copilot · model <id>" (host emits a
  `system/task_notification` row); agent mark on task rows/board/peek/cards
  wall/recents; New Task modal agent picker first + summary "Copilot will run
  this task in <folder>"; notifications keep the task title and say "Copilot finished" on the detail line; terminal
  item "Continue with Copilot in terminal". Copilot mark = fused-drawn glyph +
  word, **never the GitHub Copilot logo**.

## 3. Copilot host: spawn

```
copilot --acp --no-auto-update
        # never --session-id: --acp refuses it; Copilot mints the id in session/new; a resume does session/load
        --model <id> --reasoning-effort <effort>
        --plugin-dir <run_dir>/plugin --agent fused:fused
        --additional-mcp-config @<run_dir>/mcp.json    # only when a pane (app_state) or terminal origin exists
        --add-dir <shots> [--add-dir <extra>]...
        --allow-tool 'shell(fused:*)' [--allow-tool write]   # write only for acceptEdits
cwd = _workdir(file); env = _spawn_env() + COPILOT_GITHUB_TOKEN (D10) ; stdin/stdout = JSON-RPC pipes; stderr -> err.log
```

ACP sequence (all verified): `initialize {protocolVersion:1, clientCapabilities:{}}` →
`session/new {cwd, mcpServers:[]}` (or `session/load {sessionId, cwd, mcpServers:[]}`)
→ per turn `session/prompt {sessionId, prompt:[{type:"text", text}]}` → reply
`{stopReason: end_turn|cancelled|..., usage{inputTokens,outputTokens,cachedReadTokens,...}}`.
Notifications `session/update {sessionId, update:{sessionUpdate: ...}}` kinds seen:
`available_commands_update`, `session_info_update{title}`, `config_option_update`,
`current_mode_update`, `usage_update{used,size}`, `agent_thought_chunk{content{text}}`,
`agent_message_chunk{content{text}}`, `user_message_chunk` (replay only),
`tool_call{toolCallId,title,kind,status,rawInput,locations}`,
`tool_call_update{toolCallId,status:completed|failed,content[{type:diff|text,...}],rawOutput}`.
Client-side requests to answer: `session/request_permission` (→ `{"outcome":{"outcome":"selected","optionId":...}}`);
anything else (`fs/*`, `terminal/*`) → JSON-RPC error -32601.
Mid-session: `session/set_config_option {sessionId, configId:"allow_all", value:"on"|"off"}`,
`session/set_mode {sessionId, modeId:"https://agentclientprotocol.com/protocol/session-modes#agent"}`,
`session/cancel {sessionId}` (notification, no id).

## 4. Row contract (what the host writes)

Two serializers over one internal event. `out.jsonl` = stream shape `_poll`
parses; transcript = durable shape the tasks readers parse. Every row carries
ISO `timestamp` and `session_id` (stream) / `sessionId` (transcript). Read
`agent.py` §3 of the map (`_poll`, `_read_current_turn`, `_starts_new_turn`,
`_segments_from_rows`, `_turn_state`, `_session_from_out`, `_context_usage`,
`_capture_commands`) and `tasks_store._parse_head`/`routers/tasks._absorb`/
`session_liveness.transcript_turn_open` before writing either.

| ACP event | → out.jsonl | → transcript |
|---|---|---|
| after session/new or load | `{type:system, subtype:init, session_id, cwd, model, slash_commands[], skills[], tools[]}` **first row** | on a NEW session: `{type:system, subtype:task_notification, summary:"Handled by GitHub Copilot · model <id>", ...}` |
| host sends session/prompt | `{type:user, message:{role:user, content:[{type:text,text}]}}` **written at send time** (this is the echo `pending_echo`/`_starts_new_turn` wait for) | same + `uuid, parentUuid, cwd, entrypoint:"sdk-cli", isMeta:false, isSidechain:false` |
| agent_message_chunk | `{type:stream_event, event:{type:content_block_delta, delta:{type:text_delta,text}}}`; first chunk of a message preceded by `{type:stream_event, event:{type:message_start, message:{usage:{input_tokens, cache_read_input_tokens, output_tokens:0}, model}}}` | — |
| agent_thought_chunk | same with `thinking_delta{thinking}` | — |
| tool_call | `{type:stream_event, event:{type:content_block_start, content_block:{type:tool_use,id,name,input}}}` then `{type:assistant, message:{role:assistant, model, content:[{type:tool_use,id,name,input}], usage}}` | `{type:assistant, uuid, parentUuid, message:{...same}}` |
| tool_call_update completed/failed | `{type:user, message:{role:user, content:[{type:tool_result, tool_use_id, content:<text>, is_error}]}}` (NOT text-only, so it never starts a turn) | same |
| text message ends (next tool_call, or prompt reply) | `message_delta{usage:{output_tokens}}`, `message_stop`, then `{type:assistant, message:{content:[{type:text,text}], usage, model}}` | `{type:assistant, ...}` |
| session/prompt reply | `{type:result, subtype:success\|error_during_execution, is_error, result:<final text>, session_id, usage, stop_reason, num_turns}` — on EVERY stopReason incl. cancelled | `{type:system, subtype:turn_duration, durationMs}` |
| usage_update{used,size} | fold into next message_start.usage (input_tokens=used); `size` also written as `context_window` on the init row (agent.py may read it for the meter — WS-C) | — |
| session_info_update{title} | — | `{type:"ai-title", aiTitle:<title>}` |
| JSON-RPC error / credits | `{type:assistant, isApiErrorMessage:true, message:{content:[{type:text,text:<error>}]}}` then `result is_error:true` | same assistant row |
| session/cancel acked (prompt reply cancelled) | `{type:control_response, response:{request_id, subtype:success, response:{}}}` | — |
| set_model / get_task_output / stop_task | `control_response` `subtype:error`, `error:"unsupported by copilot"` | — |

Registry file: `copilot_dir()/sessions/<pid>.json` = `{"sessionId", "cwd", "status": "busy"|"idle", "updatedAt": ISO, "pid"}`, rewritten on every turn edge, removed in `finally`.

## 5. Host lifecycle (every map, who clears it)

| state | set | cleared | stale ⇒ |
|---|---|---|---|
| `pending_perm {rpc_id → perm_id}` | request_permission | `.res.json` read; `session/cancel` (answer reject_once); `finally` (answer all reject_once) | Copilot blocked on RPC, card orphaned |
| `held_inbox []` | user row while turn open | sent after `result`; `_discard_inbox` on cancel (files still in inbox/) | follow-up sent after an unwanted cancel |
| `turn_open` | session/prompt sent | reply received (any stopReason) → `result` written | `_turn_state` open forever, no reap |
| `replaying` | session/load sent | load reply received | replay duplicated into out.jsonl |
| registry file | after spawn, each turn edge | `finally` | row shows running until pid check fails |
| `host.json`, `pid` | after spawn | `finally` (host.json removed; pid left for `_alive`) | as Claude today |

Idle reap: 30 s with no turn open (`FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS`), then
`session/cancel` nothing, close stdin, wait, post `exited`. Same `turn_ended`/
`exited`/`card_raised`/`card_cleared` posts to `/api/tasks/queue/event` as
`session_host.py` and `permission_server.py` make today (copy their code).

## 6. Invariants (each has a test)

1. First row of out.jsonl carries `session_id`.
2. Every session/prompt ends in exactly one `result` row, whatever happens (try/finally).
3. User echo row is written before the prompt is sent.
4. No second session/prompt while one is open.
5. Replay never reaches out.jsonl.
6. Every permission RPC answered exactly once.
7. Roots only through `agent_roots` (grep-test: no `CLAUDE_CONFIG_DIR`/`~/.claude` literals outside `agent_roots.py`, `claude_config/`, `claude_health.py`).
8. A session's backend never changes (`_start`/`_send` with a session_id use `backend_for_session`).
9. Two backends in one cwd concurrently: distinct roots, uuid keys, per-run dirs.
10. A disabled agent is never spawned (one check in `_start` + request-level 400s).
11. At least one agent enabled (prefs PUT rule; `default_backend` re-pointed).
12. A Copilot default never reaches `fused.ai` (`server/ai.py` falls back to haiku unless backend is claude).

## 7. Caps → behaviour

| cap false | server | frontend |
|---|---|---|
| plan_mode | `_start`/`_send` reject mode plan with 400 | mode picker omits plan |
| live_model_switch | `_send` → `{respawn:true}` on model change | — |
| live_mode_switch | respawn on mode change | — |
| snapshots | `snapshots` → `{versions:[]}`; plan/revert → `{ok:false,error:"unsupported"}` | Snapshots UI hidden |
| artifacts | `/api/claude/artifacts` → `[]` | ArtStrip hidden |
| worktrees | — | WorktreeMenu hidden |
| bg_tasks | `task_output`/`task_stop` → `{error:"unsupported"}` | TasksPanel hidden (already hides when empty) |
| ask_user | — | no QuestionCard ever arrives |
| terminal_mirror | no `claude:<chat>` terminal session | Kebab "view commands" hidden |
| mid_turn_messages | host holds inbox | composer shows queued row (existing `drained:false`) |

## 8. Wire changes

- `POST /api/claude/agent` `start`: + `backend` ("claude" default). `send`: + `backend` (must equal the session's; ignored otherwise). `poll`: + `backend`. `defaults`: + `backend`. `history`: + `backend` ("" when there is no transcript on disk and no record: no evidence, never a guess). `terminal_command`: uses the session's backend.
- `/api/tasks` row: + `backend`. `/api/tasks/pulse` row: + `backend`. `/api/tasks/create`: + `backend`. `/api/tasks/{key}/send`: + `backend` (optional, validated). `/api/tasks/settings` GET/POST: + `backend`.
- `/api/schedule` POST: + `backend`; entry field `backend`.
- `GET /api/prefs`: + `agents: {claude: bool, copilot: bool, default: id}`. PUT: `claude_enabled`, `copilot_enabled`, `default_backend`.
- `GET /api/agent-backends` (new).
- `/api/copilot/health`, `/api/copilot/health/refresh`, `/api/copilot/install` (GET/POST), `/api/copilot/login` (GET/POST), `/api/copilot/login/cancel` (new; same bodies as the Claude ones).

## 9. Workstreams and file ownership (disjoint)

- **WS-A server plumbing**: `agent_backends/roots.py` (app-side loader of the shared file); root-list refactor in `tasks_store.py`, `session_liveness.py`, `tasks_watch.py`, `claude_artifacts.py`, `claude_session_move.py`, `server/routers/claude_sessions.py`, `server/routers/tasks.py` (incl. erase D15, row `backend`, create/send/settings), `templates/shared/file_history.py` (enrich glob), `schedule.py` (entry field + disabled gate), `claude_spawn.py` (pass backend), `shell/prefs.py` PUT/GET, `server/routers/agent_backends.py` (new, `/api/agent-backends`), `server/app.py` (include routers), `server/ai.py` (invariant 12), `diagnostics.py` (bundle plan mentions roots). Tests for roots refactor + prefs rule + gates.
- **WS-B host**: `claude_agent/copilot_host.py` (new), `claude_agent/permission_server.py` (`--no-approve`), `tests/test_copilot_host.py` with a fake ACP peer (stdlib). Nothing else.
- **WS-C agent.py**: `claude_agent/agent.py` only (+ its tests): host pick, `_copilot_argv`, `_copilot_plugin_dir`, `_send` respawn rule, caps gating on actions, `backend` in meta/host/settings/poll/defaults/history, `_terminal_command`, `_commit_turn` subject, `_account_error` per backend, `_session_transcript_path`/`_cli_sessions`/`_history` via roots, `_start` gate reading prefs.json (invariant 10) through a tiny stdlib read of `<home>/prefs.json` (`claude_enabled`/`copilot_enabled` keys; same defaults as prefs.py), `_history` first-turn notice passthrough.
- **WS-D health**: `copilot_health.py`, `copilot_login.py`, `copilot_install.py`, `server/routers/copilot_health.py` (new), tests. Nothing else.
- **WS-E frontend**: everything under `frontend/src` per D11/D17/D18 + caps table. `tsc --noEmit` must pass.

Rules for every workstream: stdlib-only + no `fused_render` import inside
`claude_agent/*` and `templates/shared/*`; never run `vite build` or
`scripts/dev.sh`; do not commit (the TL commits per workstream); match the
surrounding comment density and idiom; every new behaviour in §6 has a test.

## 10. Verified vs assumed

Verified 2026-10-16 (Copilot 1.0.98): ACP handshake/prompt/cancel/load/usage;
request_permission shape + options; `--plugin-dir`+`--agent plugin:name`,
`--additional-mcp-config @file` (tool reached as `<server>-<tool>`), `--model`
under `--acp`; `set_config_option allow_all`, `set_mode`; second prompt cancels
first; MCP via session/new never attaches; no `ask_user`; `copilot --resume=<sid>`
resumes an ACP session; events.jsonl finals only; `preToolUse` hook deny shape;
`--acp` refuses `--session-id` (CLI exits); signed-out `session/new` →
`-32000 "Authentication required"`; `--available-tools=` with an EMPTY value
keeps every tool (a never-matching name removes them); `session/load` replays
each prompt text byte-identical, machinery blocks included, so the backfill's
text match never duplicates a fused turn (checked with a live-app-state +
terminal-hint prompt and two resumes: 3 user rows). End-to-end through
`agent.py` (start, live send, permission card, Stop, resume, history): 22/22.

Assumed (design absorbs each): signed-out ACP error code (fallback: `-p "ping"`
20 s probe); `COPILOT_GITHUB_TOKEN` as login substitute; Copilot error texts;
install command (`npm i -g @github/copilot` vs standalone installer — check
docs at build time); `--allow-tool write` as acceptEdits.
