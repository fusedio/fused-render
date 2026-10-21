# Codex backend — build contract

OpenAI Codex CLI as a third agent for **Tasks and Sessions** (not Bots), beside
Claude Code and GitHub Copilot CLI. Design settled 2026-10-21. This file is
the contract every workstream codes against; it condenses, and where Codex
differs overrides, `docs/COPILOT_BACKEND.md`, which stays the reference for
everything a non-Claude backend shares (the seam, the row contract's readers,
the invariants, the caps table, the lifecycle maps).

Verified live against Codex CLI 0.163.0 (`@openai/codex`, npm) on 2026-10-21,
signed in with a ChatGPT account (plan `go`): everything in §10 Verified.
Anything not listed there is an assumption and is marked.

## 0. Verdict and seam

Same seam as Copilot (COPILOT_BACKEND.md §0): **the host process and the row
shape it writes**. A new `claude_agent/codex_host.py` drives `codex
app-server` (JSON-RPC 2.0, newline-delimited, over stdio — Codex's own
"app-server protocol v2", the one its VS Code extension speaks) and writes
**the same run-dir files and the same stream-json rows** `session_host.py`
and `copilot_host.py` produce. `_poll`, `_segments_from_rows`, `_turn_state`,
`_history`, the tasks scan, the queue and the watchers are not rewritten.

Codex is NOT ACP. The wire is different in every method name but the same in
shape (a request/reply/notification JSON-RPC peer, approvals as server→client
requests), so `copilot_host.py` is the model to copy from, not to share code
with: the two hosts stay separate files, stdlib-only, loaded by path.

Where Codex is SIMPLER than Copilot, the design takes the simpler road:

* the system prompt is a request field (`developerInstructions`), so there is
  no generated plugin dir;
* model, effort, approval policy and sandbox are **per turn** (`turn/start`
  carries all four), so a model or mode change never respawns;
* the session id is Codex's own (`thread/start` mints it), exactly as
  Copilot's is — the same "self-minted id" path, generalised.

## 1. Code in the tree the Codex work extends

`agent_roots.py` grows a third root; `agent_backends/` a third row; `prefs`
a third switch; `agent.py` a third host file and argv; the frontend a third
fallback row, glyph, toggle and health strip. Exact lists in §9.

```
agent_roots:   BACKENDS = ("claude", "copilot", "codex")
               codex_dir() -> <FUSED_RENDER_HOME|~/.fused-render>/codex   # unnested home, like copilot_dir()
               base_dir("codex") -> codex_dir(); projects/sessions/transcript_roots/registry_roots follow
registry:      BACKENDS = {claude, copilot, codex}; ORDER = (claude, copilot, codex)
prefs:         AGENT_BACKEND_KEYS["codex"] = "codex_enabled" (off unless stored True)
               VALID_DEFAULT_BACKENDS = ("claude", "copilot", "codex")
agent.py:      _HOST_FILES["codex"] = codex_host.py
```

## 2. Decisions

- **D1** Seam as above. One host file per backend; no shared host module.
- **D2** `backend: "codex"` explicit, per task, immutable (COPILOT D2 verbatim).
  **Session id = Codex's thread id** (`thread/start` reply `thread.id`, a
  uuid7-shaped string, e.g. `01a15f4b-da04-7b11-8b21-41c96595aa15`; passes
  `agent._bad_id`). A fresh `_start` returns `session_id: ""` and `_poll`
  learns it from the host's init row — the Copilot path. `agent.py`'s
  `_record_copilot_session` becomes `_record_minted_session`, keyed on a set
  `_SELF_MINTED_ID_BACKENDS = ("copilot", "codex")`, and every
  `backend == "copilot"` check that really means "this backend mints its own
  id" reads that set. Codex's own record of the thread is its rollout file
  `<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<ts>-<sid>.jsonl`; fused never
  reads it (D3).
- **D3** Transcript **written by the host**, Claude shape, under
  `codex_dir()/projects/<munge(cwd)>/<sid>.jsonl`; live registry
  `codex_dir()/sessions/<pid>.json` (same `{sessionId, cwd, status, updatedAt,
  pid}`). Turns taken in a terminal (`codex resume <sid>`) are backfilled from
  the `thread/resume` reply's `thread.turns[]` (oldest first; each turn's
  `items[]` holds `userMessage` and `agentMessage` items — verified) by the
  same text-match diff `copilot_host.backfill_replay` does.
- **D4** Permissions through `perm/<id>.req.json` / `.res.json` (COPILOT D4).
  Codex asks the client with two server→client REQUESTS (verified):
  `item/commandExecution/requestApproval {itemId, command, cwd, commandActions,
  availableDecisions}` and `item/fileChange/requestApproval {itemId, reason,
  grantRoot}` (the change itself was announced by the `item/started` for that
  `itemId`). Answers (verified enums):
  * command: allow once → `{"decision":"accept"}`; allow scope `session` →
    `acceptForSession`; deny / expired / timeout → `decline`; host closing
    or turn cancelling → `cancel` (which also interrupts the turn — the
    behaviour a Stop wants).
  * file change: `accept` / `acceptForSession` / `decline` / `cancel`, same
    rules. (NOT `approved`/`denied`: those are the v1 `applyPatchApproval`
    enum; answering `approved` to the v2 request reads as a decline —
    verified the hard way.)
  * `item/permissions/requestApproval`, `item/tool/requestUserInput`,
    `mcpServer/elicitation/request`, `item/tool/call`, `account/chatgptAuthTokens/refresh`,
    the v1 `applyPatchApproval`/`execCommandApproval` and anything else →
    JSON-RPC error `-32601` (assumed harmless: Codex treats a refused
    request as declined; `ask_user` stays False).
  * The verified command request offered `availableDecisions: ["accept",
    {acceptWithExecpolicyAmendment}, "cancel"]` — neither `decline` nor
    `acceptForSession`. The host sends the contract's words regardless (the
    schema allows them, and a deny must never become the `cancel` that
    kills the turn) and logs to stderr when the answer was not on the menu.
    Whether Codex honours an unoffered `decline` is untested live.
  * "Allow, and accept edits" on an edit card → `acceptForSession` on that
    card (Codex stops asking for that file), never a session-wide switch;
    on a command card → plain `accept`. "Allow, and stop asking" (mode
    `auto`) → `acceptForSession` on the card AND the host's stored mode
    becomes `auto`, which takes effect on the NEXT `turn/start` (Codex has no
    mid-turn policy switch; assumed acceptable — the card that asked was
    allowed, the rest of this turn may still ask).
  `permission_server.py` stays for pane tools only (`--no-approve`), reached
  as an MCP server (D8).
- **D5** Modes → Codex policy pairs, applied on `thread/start`/`thread/resume`
  AND on every `turn/start` (so a live switch is just the next turn's values):

  | fused mode | `approvalPolicy` | sandbox | what the user sees |
  |---|---|---|---|
  | `prompt` (Ask) | `untrusted` | `read-only` | reads run free inside the sandbox; every file change and every command not on Codex's trusted list raises a card (verified: an `apply_patch` under read-only raises `item/fileChange/requestApproval`) |
  | `acceptEdits` | `untrusted` | `workspace-write` | file changes inside the workspace run without asking; non-trusted commands still raise a card (assumed: `untrusted` gates commands independently of the sandbox) |
  | `auto` | `never` | `workspace-write` | Codex's own `--full-auto`: nothing asks, writes stay inside the workspace |
  | `plan` | — | — | not offered (400, as for Copilot) |

  On `thread/start`/`thread/resume` the sandbox is the string field `sandbox`
  (`"read-only"`, `"workspace-write"`); on `turn/start` it is the structured
  `sandboxPolicy` (`{"type":"readOnly"}` / `{"type":"workspaceWrite"}`), both
  from the generated schema. `caps.live_mode_switch = True` and
  `acceptEdits` needs NO respawn (unlike Copilot): `_send`'s
  `set_permission_mode` control request is answered by the host after it
  stores the mode. `danger-full-access` is never sent.
- **D6** Host **serializes prompts**: a user row arriving mid-turn is HELD in
  `inbox/` and sent after the turn's `result` (Copilot D6 verbatim). Codex
  has `turn/steer`; not used in v1 (`mid_turn_messages = False`). `interrupt`
  control requests → `turn/interrupt {threadId, turnId}` immediately
  (verified: the turn ends with `turn/completed status: "interrupted"`).
- **D7** Model and effort are **per turn**: `turn/start {model, effort}`.
  `caps.live_model_switch = True`; `_send` never answers `{respawn: True}`
  for Codex (not for a model, an effort, a mode or a new attached dir); a
  model or effort change becomes ONE `set_model` control request carrying
  both values (the unchanged half filled from `host.json`), which the host
  stores and uses on the next `turn/start` (answered `success`), and
  `_send` writes both into `host.json`. (Before this, `_send` never wrote
  effort: it respawned on an effort change.)
- **D8** System prompt via `thread/start {developerInstructions: <_appended_prompt(file, pane, "codex")>}`
  (and on `thread/resume`, same field). No plugin dir, no agent file. Pane
  tools (`app_state`, `terminal_*`) via Codex's MCP config as `-c` argv
  overrides on the spawn: `-c mcp_servers.fused_approvals.command="<python>"`,
  `-c mcp_servers.fused_approvals.args=[...]`, `-c mcp_servers.fused_approvals.env={...}`
  (TOML inline values: `json.dumps` IS TOML for a string and an array of
  strings, but NOT for a table — TOML spells `{"k" = "v"}` and Codex 0.163.0
  rejects the JSON `{"k": "v"}` with "expected a map"; `agent._toml_inline`
  spells tables key by key, verified with `codex -c … mcp get fused_approvals
  --json` parsing all three overrides into an enabled stdio server), ONLY
  when a pane or terminal origin exists, same condition as `_copilot_argv`'s
  `--additional-mcp-config`. The tool then reaches the model as MCP server
  `fused_approvals`, tool `app_state`; the host writes it as
  `mcp__fused_approvals__app_state` (assumed: that the configured server
  reaches a thread; the config shape is verified).
- **D9** `extra_read_dirs`/shots → nothing: Codex's `read-only` and
  `workspace-write` sandboxes read the whole filesystem. fused CLI →
  `_fused_cli_dir()` prepended to the spawn's PATH (the same prefix Claude's
  spawn gets); no allow-list flag exists and none is needed, so the prompt
  note's "a bare `fused` command is pre-approved" clause is dropped for
  Codex only (`_FUSED_PREAPPROVED`) — under Ask mode the command raises a
  card like any other. No terminal mirror.
- **D10** `codex_health.py` + `codex_login.py` + `codex_install.py` +
  `routers/codex_health.py` under `/api/codex/{health, health/refresh,
  install, login, login/cancel, link-path}`, same snapshot keys as
  `claude_health.summary()` / `copilot_health`. **Signed-in probe** = spawn
  `codex app-server`, `initialize {clientInfo}` → `initialized` notification
  → `account/read {refreshToken: false}`; reply `{"account": null,
  "requiresOpenaiAuth": true}` → signed out; `{"account": {"type": "chatgpt",
  "planType": "go", ...}}` → signed in, `account.method = "chatgpt"`;
  `{"type": "apiKey"}` → `"api_key"` (both verified; `requiresOpenaiAuth` is
  `true` whether or not someone is signed in, so only `account` decides).
  No model is spawned and no prompt sent: the probe is free. SIGNED-OUT
  SESSIONS: with an empty `CODEX_HOME`, `thread/start` still succeeds and
  mints a thread (verified); the refusal only comes when the first turn
  calls the model, so a signed-out session reads as a session that comes up
  and whose first turn fails as an API error (D13), never as a handshake
  failure. A probe that times out keeps the
  previous snapshot's `account` and reports `signed_in: null`. Binary:
  `FUSED_RENDER_CODEX_BIN`, then PATH, then candidates
  `~/.npm-global/bin/codex`, `/opt/homebrew/bin/codex`, `/usr/local/bin/codex`,
  `~/.local/bin/codex`, `~/.bun/bin/codex`, `~/Library/pnpm/codex`. Version:
  `codex --version` → `codex-cli 0.163.0`; floor `0.160.0` (assumed: the v2
  methods used here exist in 0.163.0; older is unverified). Install:
  `npm install -g @openai/codex` (the documented route; brew `codex` also
  exists — `install_method` reports `npm` | `brew` | `other`). Login: `codex
  login --device-auth` prints (verified, stdout):
  ```
  1. Open this link in your browser and sign in to your account
     https://auth.openai.com/codex/device
  2. Enter this one-time code (expires in 15 minutes)
     9N23-GJGFW
  ```
  then `Successfully logged in` on success (exit 0). The login module parses
  the URL and the `XXXX-XXXXX` code for the health strip's device-code UI,
  exactly as `copilot_login` does. The child inherits the user's
  `CODEX_HOME` untouched (it is where the credential lands).
- **D11** `GET /api/agent-backends` unchanged in shape; Codex is a third row.
- **D12** no-equivalent table: AskUserQuestion → none; bg tasks → none;
  snapshots/artifacts/worktrees → hidden; recap → `codex exec` one-shot
  (`--skip-git-repo-check --ephemeral -s read-only -C <tmp> -m <model>`,
  prompt on stdin, `-o <file>` for the last message; assumed flags from
  `codex exec --help`); terminal continue → `codex resume <sid>`; plan →
  hidden; commit subject `"Codex: <msg>"`; `entrypoint` always `sdk-cli`.
- **D13** errors: `turn/completed` with `turn.status == "failed"` →
  `assistant` row `isApiErrorMessage: true` carrying `turn.error.message`
  (plus the last `error` notification's message when the turn's own is
  empty) + `result is_error`. The `error` notification's shape is
  `{error: {message, additionalDetails}, willRetry, threadId, turnId}` (the
  schema's; a flat `{message}` is read too); a `willRetry: true` error is
  ignored, and the notification alone never ends a turn. Markers in `Backend.account_error_markers` /
  `limit_error_markers` (seeded; see `codex.py`).
- **D14** item → tool name map (the Claude names the page already renders):

  | Codex item (`item/started`) | tool_use | tool_result (`item/completed`) |
  |---|---|---|
  | `commandExecution {command, cwd, commandActions}` | `Bash {command, description}` (`description` = the `commandActions[].command` joined, else the command) | `aggregatedOutput` (text), `is_error` = `exitCode` not 0 or `status` in `failed`/`declined` |
  | `fileChange {changes[{path, kind{type:add\|delete\|update}, diff}]}` | one block per item: `Write {file_path, content: diff}` when every change is `add`, else `Edit {file_path, diff}` — `file_path` = first change's path (made relative to cwd when under it), `diff` = the diffs joined, each prefixed `--- <path>` when there are several | `status` text, `is_error` = `status` not `completed` |
  | `mcpToolCall {server, tool, arguments}` | `mcp__<server>__<tool> {arguments…}` | `result` / `error` as text |
  | `webSearch {query}` | `WebSearch {query}` | results as text |
  | `agentMessage` | — streamed text (below) | — |
  | `reasoning` | — streamed thinking (below) | — |
  | `userMessage` (our own echo, or a replay) | ignored live; replay → backfill | — |
  | `plan`, `functionCallOutput`, `contextCompaction`, `imageView`, `sleep`, others | ignored | — |

  Every mapped input also carries `_codex: {type, itemId}` (the `_acp`
  sidecar's twin). The frontend STRIPS `_acp` from chips and cards
  (`summaries.ts`, `ToolChip.tsx`), so it strips `_codex` the same way —
  otherwise every Codex tool call would show the sidecar as an extra field.
- **D15** erase removes the fused transcript + Codex's own rollout file(s)
  `<CODEX_HOME>/sessions/**/rollout-*-<sid>.jsonl` and
  `<CODEX_HOME>/archived_sessions/**/rollout-*-<sid>.jsonl` (glob by the
  id, which is checked against `_bad_id` first); relocate moves the fused
  transcript bucket only.
- **D16** app-server only for sessions; `codex exec` only for the recap.
- **D17** a third Preferences toggle `codex_enabled` (default off). The PUT
  rule becomes "at least one of the three on"; the error copy "turn on
  another agent first"; `default_backend` re-pointed to the first enabled in
  ORDER. Everything else in COPILOT D17 applies with "Copilot" read as "a
  non-Claude agent".
- **D18** agent always named: label **"Codex"**, long form "OpenAI Codex";
  first-turn notice row `"Handled by OpenAI Codex · model <id>"`; placeholders
  "Ask Codex…"; "Codex is thinking"; "Codex wants to use…"; "Codex finished";
  "Continue with Codex in terminal". Codex mark = fused-drawn glyph + word,
  **never the OpenAI logo**.

## 3. Codex host: spawn

```
codex app-server [-c mcp_servers.fused_approvals.command="…" -c mcp_servers.fused_approvals.args=[…] -c mcp_servers.fused_approvals.env={…}]
cwd = _workdir(file); env = _spawn_env("", backend="codex"); stdin/stdout = JSON-RPC pipes; stderr -> err.log
```

No `--model`, no mode flags: all per request (D5, D7). `-c` only for the
pane MCP server (D8). `_spawn_env(backend="codex")` = Copilot's branch minus
the gh token: `CLAUDE_CODE_*` stripped, `CODEX_HOME` left exactly as found,
`_fused_cli_dir()` first on PATH when present.

Protocol sequence (all verified):
```
→ {"id":1,"method":"initialize","params":{"clientInfo":{"name":"fused-render","version":"1"}}}
← {"id":1,"result":{"userAgent","codexHome","platformFamily","platformOs"}}
→ {"method":"initialized"}                                   # notification, required before anything else
→ thread/start {cwd, model, approvalPolicy, sandbox, developerInstructions}
   | thread/resume {threadId, cwd, approvalPolicy, sandbox, developerInstructions}
← {"result":{"thread":{"id", "cwd", "model", "turns":[...], ...}}}      # + notification thread/started
→ turn/start {threadId, input:[{"type":"text","text"}], model, effort, approvalPolicy, sandboxPolicy}
← {"result":{"turn":{"id","status":"inProgress"}}}             # the turnId `turn/interrupt` needs
← notifications until turn/completed {threadId, turn:{id, status: completed|interrupted|failed, error}}
→ turn/interrupt {threadId, turnId}   ← {"result":{}}          # then turn/completed status interrupted
```
Server→client requests to answer: D4. Notifications seen (verified):
`thread/started`, `thread/status/changed {status:{type: idle|active, activeFlags:[waitingOnApproval]}}`,
`turn/started`, `item/started {item, threadId, turnId}`, `item/completed {item}`,
`item/agentMessage/delta {itemId, delta}`, `item/reasoning/summaryTextDelta {itemId, delta}`,
`item/reasoning/textDelta`, `item/commandExecution/outputDelta {itemId, delta}`,
`thread/tokenUsage/updated {tokenUsage:{total{…}, last{inputTokens, cachedInputTokens, outputTokens, reasoningOutputTokens}, modelContextWindow}}`,
`account/rateLimits/updated`, `account/updated`, `serverRequest/resolved`,
`turn/completed`, `thread/name/updated`, `configWarning`, `deprecationNotice`,
`remoteControl/status/changed`, `mcpServer/startupStatus/updated`, `error`.
Unknown notifications are ignored.

## 4. Row contract (what the host writes)

Same two serializers as COPILOT §4 (out.jsonl stream shape; transcript
record shape), same `timestamp`/`session_id`/`sessionId` stamps, same three
reader rules from `copilot_host.py`'s header. Only the left column changes:

| Codex event | → out.jsonl | → transcript |
|---|---|---|
| after thread/start or resume | `{type:system, subtype:init, session_id:<thread.id>, cwd, model, backend:"codex", permissionMode, slash_commands:[], skills:[], tools:[], context_window?}` **first row** | NEW session: `{type:system, subtype:task_notification, summary:"Handled by OpenAI Codex · model <id>", backend:"codex", model}` |
| host sends turn/start | user echo (text content) **written at send time** | same + `uuid, parentUuid, cwd, entrypoint:"sdk-cli", isMeta:false, isSidechain:false` |
| `item/started agentMessage` (first delta) | `message_start` (usage from the newest `tokenUsage.last`, `model`) | — |
| `item/agentMessage/delta` | `content_block_delta text_delta` | — |
| `item/reasoning/*Delta` | `content_block_delta thinking_delta` (opens the message if none is open) | — |
| `item/completed agentMessage` | `message_delta`, `message_stop`, then `{type:assistant, message:{content:[thinking?, text], usage, model}}` | `{type:assistant, …}` |
| `item/started commandExecution\|fileChange\|mcpToolCall\|webSearch` | close any open message; `content_block_start tool_use` then `{type:assistant, message:{content:[tool_use]}}` (D14) | same |
| `item/completed` of those | `{type:user, message:{content:[{type:tool_result, tool_use_id, content, is_error}]}}` | same |
| `turn/completed status:completed` | close the open message; `{type:result, subtype:success, is_error:false, result:<last text>, session_id, usage, stop_reason:"end_turn", num_turns, duration_ms, backend:"codex"}` | `{type:system, subtype:turn_duration, durationMs}` |
| `turn/completed status:interrupted` | result `subtype:error_during_execution, is_error:true, result:"", stop_reason:"cancelled"` | user row `[Request interrupted by user]` (tasks_store's interrupt mark) + turn_duration |
| `turn/completed status:failed` / transport death / `error` notification mid-turn | `{type:assistant, isApiErrorMessage:true, message:{content:[{type:text,text:<error>}]}}` then result `is_error:true, stop_reason:"error"` | same assistant row + turn_duration |
| `thread/tokenUsage/updated` | kept for the next `message_start`/`result` usage. Codex's `inputTokens` INCLUDES `cachedInputTokens` (verified: 13466 of which 11520 cached), so `input_tokens = last.inputTokens − last.cachedInputTokens`, `cache_read_input_tokens = last.cachedInputTokens`, `output_tokens = last.outputTokens` — the meter's sum then equals Codex's own figure; `modelContextWindow` → `{type:system, subtype:status, context_window}` INSIDE a turn, and `context_window` on the init row once known | — |
| `thread/name/updated {threadName}` (the schema's field; `name` read too) | — | `{type:"ai-title", aiTitle}` |
| `turn/interrupt` reply | `{type:control_response, response:{request_id, subtype:success, response:{still_queued:[]}}}` (answered at once, as Copilot's) | — |
| `set_model` / `set_permission_mode` control requests | `control_response subtype:success` after storing the value | — |
| `get_task_output` / `stop_task` / others | `control_response subtype:error, error:"unsupported by codex"` | — |

Usage per message: Codex reports token usage per thread, not per message;
the newest `last` block rides the next `message_start` and the turn's
`result`, as Copilot's `usage_update` does.

Registry file: `codex_dir()/sessions/<pid>.json`, rewritten on every turn
edge, removed in `finally` — COPILOT §4 verbatim.

## 5. Host lifecycle

COPILOT §5 verbatim, with `turn/interrupt` for `session/cancel`,
`pending_perm {rpc_id → {perm_id, done, kind}}` where `kind` is `command` |
`file`, and `turn_id` (from the `turn/start` reply, needed by the interrupt)
beside `turn_open`. Idle reap 30 s (`FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS`),
handshake bound `FUSED_CODEX_HOST_HANDSHAKE_SECONDS` (120), cancel grace
`FUSED_CODEX_HOST_CANCEL_GRACE_SECONDS` (15). Same four `/api/tasks/queue/event`
posts. The app-server exits on stdin EOF (verified) — same reap road.

## 6. Invariants (each has a test, `tests/test_codex_host.py`)

COPILOT §6 1–12 hold with "Copilot" read as "Codex", plus:

13. `initialized` is sent after the `initialize` reply and before any other
    request — the protocol's order. (0.163.0 happens to answer a request
    sent before it, verified; the fake app-server refuses one, deliberately
    stricter, so the host can never come to rely on that.)
14. A permission answer uses ONLY the v2 enums (`accept`, `acceptForSession`,
    `decline`, `cancel`); the v1 words never leave the host.
15. A model or mode change is applied on the next `turn/start` without a
    respawn, and the control request is answered `success`.
16. Invariant 7's grep test covers `~/.codex` / `CODEX_HOME` literals: only
    `agent_roots.py`, `codex_health.py`, `codex_login.py`, `routers/tasks.py`
    (erase, D15) and `routers/claude_sessions.py` (recap) may name them.

## 7. Caps → behaviour

```
CODEX caps: plan_mode=False, live_model_switch=True, live_mode_switch=True,
            snapshots=False, artifacts=False, worktrees=False, bg_tasks=False,
            ask_user=False, terminal_mirror=False, mid_turn_messages=False
```
COPILOT §7's table applies for every False. The two True caps mean: no
`{respawn: True}` from `_send` for Codex, ever.

## 8. Wire changes

Nothing new in SHAPE. `backend` may now be `"codex"` wherever it was
`"claude" | "copilot"`. `GET /api/prefs` → `agents: {claude, copilot, codex,
default}`; PUT accepts `codex_enabled`. New routes: `/api/codex/*` (D10).
Catalog row for Codex (`agent_backends/codex.py`, the frontend's
`CODEX_FALLBACK` must match it field for field — `tests/test_agent_backend_pins.py`):

```
id "codex", label "Codex"
models: gpt-6-luna "GPT-6 Luna" (default; the plan's default, verified), gpt-6.1-sol "GPT-6.1 Sol",
        gpt-5.6-terra "GPT-5.6 Terra", gpt-5.6-luna "GPT-5.6 Luna"
default_model "gpt-6-luna"; efforts ("low","medium","high","xhigh","max"); modes ("prompt","acceptEdits","auto")
account_error_markers: ("not logged in", "requiresopenaiauth", "codex login", "unauthorized", "authentication", "login required", "sign in")
limit_error_markers:   ("rate limit", "usage limit", "quota", "insufficient_quota", "spend control", "too many requests", "429")
terminal_resume: cd <cwd> && codex resume <sid>
routes_prefix "/api/codex"; pref_key "codex_enabled"
```
(`model/list` is dynamic and plan-dependent — a Go plan listed three — so the
catalog is a seed, ids pass through unvalidated as for the other agents.)

## 9. Workstreams and file ownership (disjoint)

- **WS-A server plumbing**: `templates/shared/agent_roots.py` (BACKENDS,
  `codex_dir`, `base_dir`), `agent_backends/roots.py` (`projects_roots`,
  `sessions_roots`, re-exports), `agent_backends/codex.py` (new),
  `agent_backends/registry.py`, `agent_backends/__init__.py` docstring,
  `shell/prefs.py` (keys, `codex_enabled()`, `_agents_state`, PUT rule +
  error copy), `server/routers/tasks.py` (`_export_markdown` who map, erase
  D15 via a `_codex_rollout_files(sid)` helper), `server/routers/claude_sessions.py`
  (`_recap_codex` + the recap-enabled branch). Tests:
  `tests/test_agent_backends_plumbing.py` (roots ×3, prefs rule with three
  switches, erase, catalog has three rows), `tests/test_agent_backend_pins.py`
  (frontend `CODEX_FALLBACK` vs `codex.py`; WS-E writes the TS side to §8).
- **WS-B host**: `claude_agent/codex_host.py` (new), `tests/fake_codex_app_server.py`
  (new, stdlib, the `fake_copilot_acp.py` twin for this wire),
  `tests/test_codex_host.py` (new). Nothing else.
- **WS-C agent.py**: `claude_agent/agent.py` only (+ `tests/test_agent_backend.py`):
  `_HOST_FILES["codex"]`, `_CODEX_CANDIDATES`, `_codex_bin`, `_codex_argv`,
  `_backend_enabled` (`codex_enabled`), labels + marker tables (copied from
  `codex.py`, pinned by test), `_terminal_command` codex branch,
  `_spawn_env(backend="codex")`, `_SELF_MINTED_ID_BACKENDS` +
  `_record_minted_session`, every `backend == "copilot"` branch re-read as
  either "Copilot only" (argv grants, `_backend_mode`, acceptEdits/model
  respawn, gh token, plugin dir) or "any non-Claude agent" (plan refusal, no
  disk/builtin commands, raw model id, no snapshots, context window from the
  host, history notice row, `_fused_cli_note`/`_blocked_note`), `_account_error`
  codex texts, `_commit_turn` subject, `cli_name`. `_start` builds Codex's
  argv up front like Copilot's.
- **WS-D health**: `codex_health.py`, `codex_login.py`, `codex_install.py`,
  `server/routers/codex_health.py` (new), the two registration lines in
  `server/app.py` (router include) and the warm-up lines in `server/app.py` +
  `cli.py` (`if registry.is_enabled("codex"): codex_health.warm_in_background()`),
  `tests/test_codex_health.py` with a fake app-server (stdlib). Nothing else.
- **WS-E frontend**: everything under `frontend/src` (§8 fallback row, glyph,
  `agentRoute`, `Prefs.agents`, settings toggle + registry entry, health
  strip copy, trouble markers/help, TroubleView words, local-commands model
  regex) + frontend tests. `npx tsc --noEmit` and `npx vitest run` must pass.

Rules for every workstream: stdlib-only + no `fused_render` import inside
`claude_agent/*` and `templates/shared/*`; never run `vite build` or
`scripts/dev.sh`; never commit; match the surrounding comment density and
idiom; every new behaviour in §6 has a test; NO live Codex turns (the TL
runs the few the plan allows) — the signed-out handshake, `account/read`
and `model/list` are free and may be used.

## 10. Verified vs assumed

Verified 2026-10-21 (Codex CLI 0.163.0, ChatGPT Go account): `initialize` →
`initialized` → `thread/start {cwd, model, approvalPolicy, sandbox,
developerInstructions}` → `turn/start {threadId, effort, input}` → stream →
`turn/completed`; thread id shape; `item/started|completed` for
`userMessage`, `agentMessage`, `commandExecution`, `fileChange`;
`item/agentMessage/delta`; `item/commandExecution/requestApproval` params +
`availableDecisions` and the `accept` answer running the command;
`item/fileChange/requestApproval` raised for `apply_patch` under
`untrusted`+`read-only` (and that `approved` is NOT an accepted answer —
the item completed `declined`); `thread/tokenUsage/updated` shape incl.
`modelContextWindow` (258400 for gpt-6-luna); `turn/interrupt` → `{}` and
`turn/completed status: interrupted`; `thread/resume` reply carries
`thread.turns[]` oldest-first with items; `thread/turns/list` (newest
first); `account/read` signed out (`account: null, requiresOpenaiAuth:
true`) and signed in (`type: chatgpt, planType: go`); `model/list` with
`supportedReasoningEfforts`; `codex login --device-auth` output; `codex
--version` banner; the app-server exits on stdin EOF; `codex exec` one-shot
reply (`hello from codex`, 1,923 tokens).

Also verified at build time (2026-10-21): the `-c mcp_servers.*` TOML
override shape (D8, via `codex mcp get --json`); every `codex exec` flag the
recap uses exists in `codex exec --help` (D12, help only); the `codex login
--device-auth` output and `Successfully logged in` (D10, a real login);
`account/read` signed in through the app's own health route; a signed-out
`thread/start` succeeding (D10); the whole Tasks flow through the running
app with a ChatGPT Go account: New task modal → Codex/Ask/GPT-6 Luna/low
→ "Handled by OpenAI Codex" → two "Codex wants to use Bash" cards
(approved) → the file changed → `done` → a follow-up turn in the same host
→ the idle reap (registry empty) → a later message resuming the thread
(`thread/resume`) in a fresh host → the Tasks list and peek naming Codex.

Assumed (design absorbs each): the configured MCP server reaches a thread
(D8); `-32601` on `requestUserInput` / `permissions` requests is harmless
(D4); an unoffered `decline` is honoured (D4); `untrusted` +
`workspace-write` still asks for non-trusted commands (D5 acceptEdits);
rollout file layout under `archived_sessions` (D15); version floor
`0.160.0` (D10); the exact wording of a signed-out turn's error (D13).
