# Cursor backend — build contract

Cursor's CLI (`cursor-agent`, Anysphere, https://cursor.com/cli) as a sixth
agent for **Tasks and Sessions**, beside Claude Code, GitHub Copilot CLI,
OpenAI Codex CLI, Google Gemini CLI and OpenCode. Design settled 2026-11-20.
This file is the contract the work codes against; it condenses, and where
Cursor differs overrides, `docs/GEMINI_BACKEND.md` and
`docs/OPENCODE_BACKEND.md`, which stay the reference for the ACP lane this
follows step for step (the seam, the row contract, the invariants, the caps
table, the lifecycle maps), and `docs/COPILOT_BACKEND.md` for everything a
non-Claude backend shares.

**Read from the CLI's own bundle**, release `2026.11.19-f005e9d`
(`downloads.cursor.com/lab/2026.11.19-f005e9d/linux/x64/agent-cli-package.tar.gz`,
the release `curl https://cursor.com/install | bash` installed that day):
the ACP agent (`src/acp/cursor-acp-agent.ts`, `agent-session.ts`,
`session-resources.ts`, `session-update-presenter.ts`,
`tool-call-presentation.ts`, `types.ts`), the commander option table and the
`status` command. **No live turn was run** — the binary was not executed on
the build machine — so §10 separates what the source says from what is
assumed.

## 0. Verdict and seam

Same seam as Gemini and OpenCode: **the host process and the row shape it
writes**. `cursor-agent acp` (a HIDDEN subcommand: "Start the Cursor Agent as
an ACP (Agent Client Protocol) server") speaks ACP over stdio with the
`@agentclientprotocol/sdk` 0.14.1, so `claude_agent/cursor_host.py` is a COPY
of `gemini_host.py` with Cursor's facts in, not a shared module. It writes the
same run-dir files and the same stream-json rows.

Where Cursor is SIMPLER than Gemini:

* no OAuth prompt on stdout: signed out, `session/new` fails at once with
  `-32000 "Authentication required"`, `data.message` "Authentication
  required. Please run 'agent login' first, then call authenticate() with
  methodId 'cursor_login'." (observed; the CLI names its `agent` alias);
* `session/load` awaits its replay BEFORE replying, so the backfill never
  races the reply (the quiet wait is kept, short, for a trailing update);
* the model list is one extension request (`cursor/list_available_models`)
  that needs no session, so the health probe creates none.

Where it is DIFFERENT:

* Cursor's ACP modes (`agent`, `plan`, `ask`) are NOT approval modes: `plan`
  is read-only planning, `ask` is Q&A with no edits or commands. The fused
  approval modes are the HOST's policy for answering
  `session/request_permission` (D5) — so a mode switch is live and never
  reaches the CLI;
* "Allow always" on a card writes Cursor's GLOBAL allowlist
  (`persistAllowlistEntry` → `~/.cursor/cli-config.json`): the host never
  picks it (D4);
* no prompt file fused can name: the appended prompt rides the first
  `session/prompt` (D8);
* `session/set_model` is process-global inside the CLI and persisted to its
  config (`setCurrentModelWithParameters(…, configProvider)`): a model picked
  in fused becomes the CLI's default for the user's next terminal session too
  (assumed harmless; noted for support).

## 1. Code in the tree the Cursor work extends

```
agent_roots:   BACKENDS += "cursor"; cursor_dir() -> <FUSED_RENDER_HOME|~/.fused-render>/cursor
registry:      BACKENDS += cursor; ORDER = (claude, copilot, codex, gemini, opencode, cursor)
prefs:         AGENT_BACKEND_KEYS["cursor"] = "cursor_enabled" (off unless stored True)
agent.py:      _HOST_FILES["cursor"] = cursor_host.py; _SELF_MINTED_ID_BACKENDS += "cursor"
               _NO_PREAPPROVE_BACKENDS += "cursor"; _NO_CANVAS_HOOK_BACKENDS += "cursor"
               _CURSOR_MODES; _cursor_argv; _cursor_bin; _CURSOR_CANDIDATES
```

## 2. Decisions

- **D1** Seam as above. One host file per backend; no shared host module.
- **D2** `backend: "cursor"` explicit, per task, immutable. **Session id =
  Cursor's own** (`crypto.randomUUID()` in `newSession`); a fresh `_start`
  returns `session_id: ""` and `_poll` learns it from the host's init row
  (`_SELF_MINTED_ID_BACKENDS`). Cursor keeps its own record under
  `~/.cursor/chats`; fused never reads it.
- **D3** Transcript written by the host, Claude shape, under
  `cursor_dir()/projects/<munge(cwd)>/<sid>.jsonl`; live registry
  `cursor_dir()/sessions/<pid>.json`. Resume is `session/load {sessionId,
  cwd, mcpServers}` → `{modes, models, configOptions}` after
  `replayConversationHistory` (user text as `user_message_chunk`, assistant
  text as `agent_message_chunk`, thinking as `agent_thought_chunk`, each tool
  as `tool_call` + `tool_call_update completed` with ids `replay-<turn>-<step>`).
  Terminal turns are backfilled by the text-match diff; the D8 context block
  is stripped off a replayed prompt before it is compared.
- **D4** Permissions through `perm/<id>.req.json` / `.res.json`. Cursor's card:
  `{toolCall: {toolCallId, title, kind, status: "pending", content?},
  options: [{optionId: "allow-once", kind: "allow_once"}, {optionId:
  "allow-always", kind: "allow_always"}, {optionId: "reject-once", kind:
  "reject_once"}]}`. A shell card is titled `` `cmd` `` (backticks, inner
  ones escaped) with the call's `reason` as `content`; an edit card `Edit
  `path`` / `Write path` with an ACP `diff` content entry; an MCP card
  `<server>: <tool>` with the args as JSON text. Answered by KIND: allow (any
  scope) → `allow_once`; deny / expired / timeout / cancel → `reject_once`;
  no usable option → `{"outcome": {"outcome": "cancelled"}}`. NEVER
  `allow_always` (global allowlist, above). "Always approve" (scope
  `session`) is the card's `allow_once` PLUS a host-side allow for the same
  `kind` + `title` for the life of the host process. `fs/*`, `terminal/*`
  and the CLI's own extension requests (`cursor/update_todos`, `cursor/task`,
  `cursor/generate_image` — fired with `extMethod`, failures swallowed) →
  `-32601`.
- **D5** Modes are the host's card policy, set at spawn from `cli_mode` and
  live via `set_permission_mode` (answered by the host, nothing on the wire):

  | fused mode | host policy | what the user sees |
  |---|---|---|
  | `prompt` (Ask) | forward every card | every card Cursor raises reaches the page |
  | `acceptEdits` | allow `kind: edit` / `delete` cards itself | edits run; shell commands raise a card |
  | `auto` | allow every card itself | nothing asks |
  | `plan` | — | not offered (400, as for the other non-Claude agents) |

  After the handshake the host sends `session/set_mode agent` once (a chat
  resumed from a terminal may be in `plan`/`ask`). Never `--force`/`--yolo`
  (Cursor's "Run Everything", which a team admin may also restrict).
  "Allow, and stop asking" / "…accept edits" on a card moves the host's mode.
  `caps.live_mode_switch = True`. Which calls Cursor asks about at all is its
  own policy (its allowlist/denylist in `~/.cursor/cli-config.json` and team
  settings) — the host only answers what arrives.
- **D6** Host serializes prompts (held in `inbox/` mid-turn: `handlePrompt`
  cancels a running prompt); `interrupt` → `session/cancel` notification
  (`pendingPromptCancel`), reply `{stopReason: "cancelled"}`.
- **D7** Model: `initialize` declares `clientCapabilities._meta.
  parameterizedModelPicker: true`, so `session/new`'s `models.availableModels`
  and `session/set_model` speak BASE model names (`e.name`, `auto`, …) rather
  than the exploded variant strings (`variantStringRepresentation`) a client
  without the flag gets. After `session/new`/`load` the host sends
  `session/set_model {modelId}` when the request named a model the session is
  not on, waits (bounded), and on a refusal (`-32602 Invalid model value`)
  adopts `models.currentModelId`. A spawn naming none adopts it too. Live
  switches are the same request. `caps.live_model_switch = True`. No effort:
  the thinking level is a per-model PARAMETER (`configOptions` category
  `thought_level`) that Cursor defaults; `efforts = ()`.
- **D8** System prompt: `_cursor_argv` writes `<run_dir>/cursor/instructions.md`
  = `_appended_prompt(file, pane, "cursor")` with `mcp__fused_render__<tool>`
  spelled `<tool>`, plus the readable directories (D9). The host sends it as a
  leading text block, wrapped in `<fused_render_context>…</fused_render_context>`,
  on the FIRST `session/prompt` of every host process (fresh or resumed); the
  echo and the transcript carry the user's words only. Rejected: writing
  `AGENTS.md` / `.cursor/rules` into the user's folder. Pane tools via
  `session/new`'s `mcpServers: [{name, command, args, env:[{name, value}]}]`
  (Cursor parses this shape, and the legacy ones, in `session-resources.ts`),
  from the same `mcp.json` `_write_mcp_config` writes, only when a pane,
  terminal origin or canvas exists.
- **D9** Readable directories: Cursor's tools are workspace-scoped and a read
  outside the workspace goes through `requestReadApproval` (a card), so
  `extra_read_dirs`, SHOTS and the reference canvases are NAMED in the prompt,
  never granted; no respawn for a new directory. No `fused` allow-list flag
  (`_NO_PREAPPROVE_BACKENDS`).
- **D10** `cursor_health.py` + `cursor_login.py` + `cursor_install.py` +
  `routers/cursor_health.py` under `/api/cursor/{health, health/refresh,
  install, login, login/cancel, link-path}`. Signed-in probe: `cursor-agent
  status --format json` → `{status: "authenticated" | "partially-authenticated"
  | "unauthenticated", userInfo: {email, teamName, …}}` (exit 1 +
  `{status: "error"}` on a failure → unknown). `CURSOR_API_KEY` signs the CLI
  in without a stored login (`run.ts`: `isAuthenticated = token || apiKey ||
  login`), so it reads as signed in, `method: "api_key"`; a stored login is
  `method: "cursor"`, `org` = the team. Models: `cursor-agent acp` →
  `initialize` (parameterized picker) → `cursor/list_available_models` →
  `{models: [{value, name, configOptions}]}`; no session. Binary:
  `FUSED_RENDER_CURSOR_BIN`, then `cursor-agent` on PATH, then
  `~/.local/bin/cursor-agent`, `/opt/homebrew/bin/cursor-agent`,
  `/usr/local/bin/cursor-agent` — never the installer's bare `agent` alias.
  Version: `cursor-agent --version` → `2026.11.19-f005e9d`; the dotted date is
  the version; floor `2026.11.0` (the read release's month). Install: the
  app runs `bash -c "curl https://cursor.com/install -fsS | bash"`; update:
  `cursor-agent update`; a Homebrew cask is recognised, never run. Login:
  `cursor-agent login` opens the browser itself and polls (no code to type);
  the strip's "finish in the browser window" copy is the instruction, and the
  login URL (it carries a one-time challenge) never reaches a record.
- **D11** `GET /api/agent-backends` unchanged in shape; a sixth row.
- **D11a** discovered models: the snapshot's `models` (D10) replace the
  one-row static seed (`auto`) in the catalog, `auto` kept first.
- **D12** no-equivalent table: AskUserQuestion → Cursor's question card
  (options + `__ask_question_skip__`) is answered `skip` by the host; plan
  card (`cursor/create_plan`) → not offered; bg tasks / snapshots /
  artifacts / worktrees → hidden; subagents → not negotiated (no
  `subagents` client capability), so they stay inside the CLI; recap →
  `cursor-agent -p --output-format json --mode ask --trust --workspace
  <empty mkdtemp> [--model <m>] <pointer>` with the prompt on stdin (the CLI
  reads a piped stdin beside the positional prompt), reply = the last
  `{type: "result", result, is_error}` line; a tools one-shot is refused
  (no flag limits the MCP roster). `/api/ai` does NOT offer Cursor (OpenCode
  is not offered there either). Terminal continue → `cursor-agent --resume
  <sid>`. Commit subject `"Cursor: <msg>"`.
- **D13** errors: a JSON-RPC error on `session/prompt` → `assistant` row
  `isApiErrorMessage: true` + `result is_error`. Markers in
  `Backend.account_error_markers` (`"authentication required"`, `"not logged
  in"`, `"api key is invalid"`, `"cursor-agent login"`, …) and
  `limit_error_markers` (`"rate limit"`, `"usage limit"`, `"quota"`, `"429"`,
  …); `trouble.ts` carries the same set.
- **D14** tool → name map: ACP `kind` → `execute`=Bash (input from
  `rawInput.command`, else the title with its backticks removed),
  `edit`=Edit/Write (path from `rawInput.path`, diff from the card's `diff`
  content), `delete`=Delete, `read`=Read, `fetch`=WebFetch, `search`=Grep;
  `other` with `rawInput {providerIdentifier, toolName, args}` is an MCP call
  (`fused_render` → `mcp__fused_render__<tool>`). Cursor announces a call
  while the model is still streaming its arguments (`partialToolCall` →
  `tool_call` with a possibly TRUNCATED `rawInput`), refreshes it with
  `tool_call_update {title, rawInput}`, then `in_progress` at
  `toolCallStarted` and `completed` with `content`/`rawOutput`. The host
  merges by `toolCallId` and raises the tool row only at `in_progress`,
  `completed`/`failed`, or when a card for it arrives.
- **D15** erase removes the fused transcript only; Cursor's chat stores are
  its own.
- **D16** ACP only for sessions; `-p` only for the recap.
- **D17** a sixth Preferences toggle `cursor_enabled` (default off); the PUT
  rule becomes "at least one of the six on".
- **D18** label **"Cursor"**, product name "Cursor CLI"; notice row
  `"Handled by Cursor · model <id>"`. Mark = a pointer-arrow glyph of our own,
  never Cursor's logo.
- **D19** every child runs with `NO_OPEN_BROWSER=1` (setdefault; the CLI's
  own switch), except the Sign-in child, which needs the browser.
  `CURSOR_API_KEY` passes through.

## 3. Cursor host: spawn

```
cursor-agent acp
cwd = _workdir(file); env = _spawn_env("", backend="cursor"); stdin/stdout = JSON-RPC; stderr -> err.log
```

ACP sequence (source): `initialize {protocolVersion: 1, clientCapabilities:
{_meta: {parameterizedModelPicker: true}}, clientInfo}` → `{protocolVersion,
agentCapabilities{loadSession: true, mcpCapabilities{http, sse},
promptCapabilities{audio: false, embeddedContext: false, image: true},
sessionCapabilities{list}}, authMethods[{id: "cursor_login"}]}` →
`session/new {cwd, mcpServers}` → `{sessionId, modes{currentModeId,
availableModes[agent, plan, ask]}, models{currentModelId, availableModels},
configOptions}` → `session/set_mode {modeId: "agent"}` → (`session/set_model`)
→ per turn `session/prompt {sessionId, prompt: [context?, {type: "text",
text}]}` → `{stopReason: "end_turn" | "cancelled"}`. Notifications seen in the
source: `available_commands_update` (after `session/new`/`load`),
`agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`,
`current_mode_update`, `session_info_update {title}` (auto-naming), `plan`.
No `user_message_chunk` echo on a live prompt; no `usage_update`.

## 4–8

GEMINI_BACKEND.md §4–§8 with "Gemini" read as "Cursor" and the D-list above
applied. Catalog row:

```
id "cursor", label "Cursor"
models: ("auto",) seeded; discovered per D11a; every row reasoning=False
default_model "auto"; efforts (); modes ("prompt","acceptEdits","auto")
caps: plan_mode=False, live_model_switch=True, live_mode_switch=True, everything else False
terminal_resume: cd <cwd> && cursor-agent --resume <sid>
routes_prefix "/api/cursor"; pref_key "cursor_enabled"
```

## 10. Read vs assumed

Read in the 2026.11.19-f005e9d bundle: everything in §3; the card shapes and
option ids (D4); `persistAllowlistEntry` on `allow-always`; the mode ids and
their mapping (`agent`/`code` → default, `plan`/`architect` → plan,
`ask`/`chat` → search); the parameterized picker capability and the
`cursor/list_available_models` ext method; `loadSession` awaiting the replay;
`authRequired` on an unauthenticated `session/new`; `CURSOR_API_KEY` counting
as authenticated; the `status --format json` shape; `NO_OPEN_BROWSER`; the
installer's paths; the `-p` result line `{type: "result", subtype:
"success", is_error, result, session_id}`; `--mode ask|plan`, `--trust`,
`--workspace`, `--resume`.

Assumed (design absorbs each): that commander resolves `cursor-agent acp`'s
global options with none given (the host passes none); that a multi-block
`session/prompt` reaches the model as the blocks' text in order (D8); that
`-p` reads a piped stdin beside the positional prompt (D12, read from the
call site, not its body); the exact `--version` output (a bare release
string, as the installer's own `--version` call implies); the update command
being enough for every installer layout; the floor version.
