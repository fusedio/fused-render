# OpenCode backend — build contract

OpenCode (`opencode-ai`, MIT, https://opencode.ai) as a fifth agent for
**Tasks and Sessions**, beside Claude Code, GitHub Copilot CLI, OpenAI Codex
CLI and Google Gemini CLI. Design settled 2026-11-07. This file is the
contract the work codes against; it condenses, and where OpenCode differs
overrides, `docs/GEMINI_BACKEND.md`, which stays the reference for the ACP
lane this follows step for step (the seam, the row contract, the invariants,
the caps table, the lifecycle maps), and `docs/COPILOT_BACKEND.md` for
everything a non-Claude backend shares.

Verified against OpenCode 1.19.5 (npm `opencode-ai`) on 2026-11-07: a live
`initialize` + `session/new` + `session/set_mode` + `session/set_model` +
`session/prompt` turn with a tool call and a permission card, against a
stand-in OpenAI-compatible server; `--help`; `--version`; the config schema
`OPENCODE_CONFIG_CONTENT` accepts. Everything in §10 Verified was observed on
the wire. Anything not listed there is an assumption and is marked.

## 0. Verdict and seam

Same seam as Gemini (GEMINI_BACKEND.md §0): **the host process and the row
shape it writes**. OpenCode speaks the Agent Client Protocol (`opencode acp`:
JSON-RPC 2.0, newline-delimited, over stdio), so
`claude_agent/opencode_host.py` is a COPY of `gemini_host.py` with OpenCode's
facts in, not a shared module. It writes the same run-dir files and the same
stream-json rows; `_poll`, `_segments_from_rows`, `_turn_state`, `_history`,
the tasks scan, the queue and the watchers are not rewritten.

Where OpenCode is SIMPLER than Gemini:

* no OAuth prompt on stdout at startup — a provider needing a credential it
  does not have fails the `session/prompt`, never the handshake;
* no launcher relaunch, no swallowed signals: `terminate()` works;
* no `GEMINI.md` dance for the system prompt: OpenCode reads `AGENTS.md`
  from the cwd AND every `instructions` path in config, and config can be
  injected per child through `OPENCODE_CONFIG_CONTENT` (verified), so the
  appended prompt is a file under the run dir named in `instructions`.

Where it is DIFFERENT:

* `session/new` answers `{sessionId, configOptions}` — no `modes`, no
  `models` (verified). `configOptions` carries `{id:"model", type:"select",
  currentValue, options:[{value, name}]}` and `{id:"mode", type:"select",
  currentValue:"build", options:[build, plan]}`. `session/set_mode` and
  `session/set_model` are both served (verified, `{}` results);
  `session/set_config_option {sessionId, configId, value}` is served too and
  answers the full `configOptions` list — the host uses the two named
  requests and never `set_config_option`.
* permissions are NOT modes. OpenCode's approval policy is the `permission`
  config map (`{"bash": "ask"|"allow"|"deny", "edit": …}`, per tool, with
  glob patterns per command). The fused modes map onto that config at spawn
  (D5) — a mode switch is a respawn, `caps.live_mode_switch = False`.
* OpenCode fires SIDE REQUESTS at the provider on every turn (a title
  generator prompt, verified: a second `POST /v1/chat/completions` with
  `max_tokens: 4000` in parallel with the main turn). Against a local model
  this queues behind the main turn (see `docs/LOCAL_MODEL_GATEWAY.md`).

## 1. Code in the tree the OpenCode work extends

```
agent_roots:   BACKENDS = ("claude", "copilot", "codex", "gemini", "opencode")
               opencode_dir() -> <FUSED_RENDER_HOME|~/.fused-render>/opencode
registry:      BACKENDS += opencode; ORDER = (claude, copilot, codex, gemini, opencode)
prefs:         AGENT_BACKEND_KEYS["opencode"] = "opencode_enabled" (off unless stored True)
agent.py:      _HOST_FILES["opencode"] = opencode_host.py; _SELF_MINTED_ID_BACKENDS += "opencode"
               _NO_PREAPPROVE_BACKENDS += "opencode"; _OPENCODE_MODES
```

## 2. Decisions

- **D1** Seam as above. One host file per backend; no shared host module.
- **D2** `backend: "opencode"` explicit, per task, immutable. **Session id =
  OpenCode's own** (`ses_<id>`, minted by `session/new`, verified); a fresh
  `_start` returns `session_id: ""` and `_poll` learns it from the host's
  init row (`_SELF_MINTED_ID_BACKENDS`). OpenCode keeps its own record in its
  SQLite store (`opencode debug paths db`); fused never reads it.
- **D3** Transcript written by the host, Claude shape, under
  `opencode_dir()/projects/<munge(cwd)>/<sid>.jsonl`; live registry
  `opencode_dir()/sessions/<pid>.json`. Resume is `session/load {sessionId,
  cwd, mcpServers}` → `{configOptions}` after replaying history as
  `user_message_chunk` / `agent_message_chunk` (ACP's `loadSession: true`,
  verified in `initialize`); the host keeps `replaying` through the quiet
  period as Gemini's does and backfills by text-match diff.
- **D4** Permissions through `perm/<id>.req.json` / `.res.json`.
  `session/request_permission {sessionId, toolCall{toolCallId, title, kind,
  status, locations, rawInput}, options:[{optionId:"once", kind:"allow_once",
  name:"Allow once"}, {optionId:"always", kind:"allow_always", name:"Always
  allow"}, {optionId:"reject", kind:"reject_once", name:"Reject"}]}`
  (verified). Answered by KIND: allow + scope `session` → `allow_always`,
  allow → `allow_once`, deny / expired / timeout / cancel → `reject_once`
  with `{"outcome": {"outcome": "selected", "optionId": <id>}}`; no usable
  option → `{"outcome": {"outcome": "cancelled"}}`. A permission request
  only arrives for a tool whose `permission` config says `ask` (verified: with
  no config the bash tool ran without a card; with `{"bash": "ask"}` the card
  arrived before it ran). `fs/*` and `terminal/*` client requests → `-32601`.
- **D5** Modes → the `permission` block of the injected config, at spawn:

  | fused mode | injected `permission` | what the user sees |
  |---|---|---|
  | `prompt` (Ask) | `{"edit":"ask","bash":"ask","webfetch":"ask"}` | every edit and shell command raises a card |
  | `acceptEdits` | `{"edit":"allow","bash":"ask","webfetch":"ask"}` | edits run; shell commands raise a card |
  | `auto` | `{"edit":"allow","bash":"allow","webfetch":"allow"}` | nothing asks |
  | `plan` | — | not offered (OpenCode's `plan` agent is a different concept; 400) |

  `caps.live_mode_switch = False`: a mode change is a respawn (`_send` →
  `{respawn: True}`), Copilot's road. The ACP `mode` config option
  (`build`/`plan`) is OpenCode's AGENT selector and is left at `build`.
- **D6** Host serializes prompts (held in `inbox/` mid-turn); `interrupt` →
  `session/cancel` notification.
- **D7** Model: on the injected config as `"model": "<provider>/<id>"` at
  spawn and `session/set_model {sessionId, modelId}` live (verified `{}`),
  `caps.live_model_switch = True`. Ids are `provider/model` throughout
  (`anthropic/claude-sonnet-5-5`, `fused/<catalog id>`); fused's `Model.id`
  IS that full string. No reasoning effort: `efforts = ()`. A spawn naming
  no model adopts `configOptions[id=model].currentValue`.
- **D8** System prompt: `_opencode_argv` writes `<run_dir>/opencode/AGENTS.md`
  = `_appended_prompt(file, pane, "opencode")` and names it in the injected
  config's `instructions: ["<run_dir>/opencode/AGENTS.md"]`. Pane tools via
  `session/new`'s `mcpServers:[{name, command, args, env}]` (ACP's stdio
  shape, `mcpCapabilities` verified), built from the same `mcp.json`
  `_write_mcp_config` writes, only when a pane or terminal origin exists.
- **D9** Readable directories: OpenCode's tools are not workspace-bound, so
  no `--include-directories` equivalent; `extra_read_dirs` are named in the
  AGENTS.md prompt only, and so are the reference canvases of a target in a
  live canvas clone (DECISIONS D1478). No respawn for a new directory.
- **D10** `opencode_health.py` + `opencode_login.py` + `opencode_install.py`
  + `routers/opencode_health.py` under `/api/opencode/{health,
  health/refresh, install, login, login/cancel, link-path}`. Signed-in probe:
  `opencode acp` → `initialize` → `session/new {cwd: <fused
  home>/opencode-probe, mcpServers: []}`; a result → installed and usable
  (`configOptions[id=model].options` is the provider-wide model list, D11a);
  `account.method` = `local` when the default model is `fused/…`, else
  `providers` (OpenCode's own `auth.json`, never read). "Signed in" for
  OpenCode means "has at least one usable provider": the probe injects the
  same `fused` provider the sessions get, so a machine with a loaded local
  model is signed in with no credential at all. Binary: `FUSED_RENDER_OPENCODE_BIN`,
  then PATH, then `~/.opencode/bin/opencode`, `~/.npm-global/bin/opencode`,
  `/opt/homebrew/bin/opencode`, `/usr/local/bin/opencode`,
  `~/.local/bin/opencode`, `~/.bun/bin/opencode`. Version: `opencode
  --version` → `1.19.5` (bare); floor `1.19.0` (assumed: the verified
  release's minor). Install: `npm install -g opencode-ai` (curl
  `https://opencode.ai/install` and brew `opencode` recognised, not run).
  Login: `opencode auth login` is interactive (provider picker on a TTY), so
  the Preferences Sign in runs it in the status-bar terminal (`pty_session`)
  and re-probes on exit — no loopback flow of our own.
- **D11** `GET /api/agent-backends` unchanged in shape; a fifth row.
- **D11a** discovered models: `configOptions[id=model].options` from the probe
  is huge (every models.dev provider, verified ~hundreds of rows); the
  snapshot keeps only rows whose provider prefix is `fused/` (the local
  catalog) or a provider OpenCode reports as connected. The static `MODELS`
  seed: `fused/<recommended local>` when one is installed, else
  `anthropic/claude-sonnet-5-5`, `openai/gpt-5.5`.
- **D12** no-equivalent table: AskUserQuestion → none; bg tasks → none;
  snapshots/artifacts/worktrees → hidden; recap → `opencode run --format json
  -m <model> <pointer>` with the prompt on stdin, tools denied through an
  injected `permission` of `deny` for every tool; terminal continue →
  `opencode --session <sid>` (verified flag on `run`; assumed on the TUI);
  commit subject `"OpenCode: <msg>"`; `entrypoint` always `sdk-cli`.
- **D13** errors: a JSON-RPC error on `session/prompt` → `assistant` row
  `isApiErrorMessage: true` + `result is_error`. Markers in
  `Backend.account_error_markers` (`"no api key"`, `"not authenticated"`,
  `"unauthorized"`, `"invalid api key"`, `"provider not found"`, `"model not
  found"`) and `limit_error_markers` (`"rate limit"`, `"quota"`, `"too many
  requests"`, `"429"`, `"overloaded"`).
- **D14** tool → name map: ACP `kind` → `execute`=Bash, `edit`=Edit/Write,
  `read`=Read, `fetch`=WebFetch, `search`=Grep (Gemini's D14), and OpenCode
  DOES send `rawInput` (verified: `{command, description, cwd}` on an
  execute) so the input is taken from it, with `title` as the fallback. The
  first `tool_call` row may carry a partial `rawInput` (`{cwd}` only,
  verified) that the following `tool_call_update` completes — the host
  merges by `toolCallId`. `rawOutput {output, metadata{exit, truncated}}`
  rides the `completed` update.
- **D15** erase removes the fused transcript only. OpenCode's store is one
  SQLite file shared by every session; deleting rows from it is not ours.
- **D16** ACP only for sessions; `run --format json` only for the recap.
- **D17** a fifth Preferences toggle `opencode_enabled` (default off); the
  PUT rule becomes "at least one of the five on".
- **D18** label **"OpenCode"**, long form "OpenCode"; notice row `"Handled by
  OpenCode · model <id>"`; placeholders "Ask OpenCode…". Mark = a glyph of
  our own (an angle-bracket pair), never the project's logo.
- **D19** every child runs with `OPENCODE_DISABLE_AUTOUPDATE=1` and
  `OPENCODE_CONFIG_CONTENT=<json>` (the injected config, D5/D7/D8/D20);
  `OPENCODE_CONFIG_DIR` and the user's own global config are left alone —
  inline content MERGES over them (verified: the user's skills and commands
  still arrived in `available_commands_update` with content injected).
- **D20 local models.** The injected config always carries the `fused`
  provider when the local text capability has at least one installed model:

  ```
  "provider": {"fused": {"npm": "@ai-sdk/openai-compatible", "name": "Fused Render (local)",
     "options": {"baseURL": "<FUSED_RENDER_ORIGIN>/v1", "apiKey": "{env:FUSED_RENDER_V1_KEY}"},
     "models": {"<catalog id>": {"name": "<label> (local)", "limit": {"context": <n_ctx>, "output": <max_out>}}}}}
  ```
  (the `provider`/`npm`/`options` spelling is the one 1.19.5 accepts,
  verified). `FUSED_RENDER_V1_KEY` is minted per spawn by the host and known
  to the gateway (`docs/LOCAL_MODEL_GATEWAY.md`). The models map is the
  catalog filtered by `fit`; a row whose chat template has no tools branch
  is listed with `"tool_call": false` so OpenCode disables tools for it.

## 3. OpenCode host: spawn

```
opencode acp
cwd = _workdir(file); env = _spawn_env("", backend="opencode"); stdin/stdout = JSON-RPC pipes; stderr -> err.log
```

ACP sequence (verified): `initialize {protocolVersion: 1, clientCapabilities:
{}, clientInfo}` → `{protocolVersion: 1, agentCapabilities{loadSession: true,
mcpCapabilities{http, sse}, promptCapabilities{embeddedContext, image},
sessionCapabilities{close, fork, list, resume}}, authMethods[{id:
"opencode-login"}], agentInfo{name: "OpenCode", version}}` → `session/new
{cwd, mcpServers}` → `{sessionId, configOptions}` → `session/set_model` →
per turn `session/prompt {sessionId, prompt:[{type:"text", text}]}` → reply
`{stopReason: end_turn | cancelled | …, usage: {inputTokens, outputTokens,
totalTokens}, _meta}`. Notifications `session/update` kinds seen:
`available_commands_update` (on new AND on every prompt), `user_message_chunk`
(echo of the prompt, with `messageId`), `agent_message_chunk {messageId,
content{type:"text", text}}`, `tool_call {toolCallId, title, kind, status:
"pending", locations, rawInput}`, `tool_call_update {toolCallId, status:
in_progress | completed, title, kind?, rawInput?, content?[{type:"content",
content{type:"text", text}}], rawOutput?}`, `usage_update {used, size, cost}`
(once, at the reply's end). Unknown kinds are ignored.

## 4–8

GEMINI_BACKEND.md §4–§8 verbatim with "Gemini" read as "OpenCode" and the
D-list above applied. Catalog row:

```
id "opencode", label "OpenCode"
models: seeded per D11a; every row reasoning=False
default_model: first seeded; efforts (); modes ("prompt","acceptEdits","auto")
caps: plan_mode=False, live_model_switch=True, live_mode_switch=False, everything else False
terminal_resume: cd <cwd> && opencode --session <sid>
routes_prefix "/api/opencode"; pref_key "opencode_enabled"
```

## 9. Workstreams and file ownership (disjoint)

As GEMINI §9 with `opencode` substituted, plus WS-F (`docs/LOCAL_MODEL_GATEWAY.md`).

## 10. Verified vs assumed

Verified 2026-11-07 (OpenCode 1.19.5, live on the wire): everything in §3;
`session/set_mode`, `session/set_model`, `session/set_config_option`,
`session/load`, `session/list`; `-32602 "session not found"` for a bad id;
the permission card shape and its gating by the `permission` config; the
inline config spelling `provider.<id>.{npm, name, options{baseURL, apiKey},
models{<id>{name, limit{context, output}}}}` and top-level `model`; the side
title-generation request; request headers `Authorization: Bearer <apiKey>`,
`x-opencode-session-id`, `x-session-id`; `max_tokens` from `limit.output`;
`--help` (the subcommand list, `--print-logs`, `--log-level`);
`OPENCODE_DISABLE_AUTOUPDATE`.

Assumed (design absorbs each): the `instructions` config key reaching the
system prompt (D8; fallback: write `AGENTS.md` into the cwd is NOT acceptable,
so if `instructions` does not take an absolute path the prompt rides the
first `session/prompt` as a prefixed text block); `opencode --session <sid>`
on the TUI (D12); `opencode run --format json` output shape (D12); the
`tool_call: false` model flag (D20); the exact error wording of a provider
with a bad key (D13).
