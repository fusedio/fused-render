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
`status` command. The design was built from the source alone; the binary
was then run, signed out (2026-11-20, commit `80bae23`) and signed in by
`CURSOR_API_KEY` with real model turns (2026-11-20, `tests/test_cursor_live.py`).
§10 separates what was verified live from what is still read or assumed.

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
  and `session/set_model` speak BASE model names (`auto-smart`, `gpt-5.5`, …)
  rather than the exploded variant strings (`variantStringRepresentation`) a
  client without the flag gets. **`auto` is not a wire id** (observed:
  `-32602 Invalid model value: auto`); Cursor's Auto is the listed
  `auto-smart` row, named "Auto". The host resolves fused's `auto` to the
  first row whose id is or starts with `auto` (or whose name is "Auto"), at
  spawn and on a live switch (`_wire_model`). Without that, a refused `auto`
  adopted `currentModelId` — the model last picked ANYWHERE, since
  `set_model` (and `-p --model`) persist to the CLI's config. After `session/new`/`load` the host sends
  `session/set_model {modelId}` when the request named a model the session is
  not on, waits (bounded), and on a refusal (`-32602 Invalid model value`)
  adopts `models.currentModelId`. A spawn naming none adopts it too. Live
  switches are the same request. `caps.live_model_switch = True`. No effort:
  the thinking level is a per-model PARAMETER (`configOptions` category
  `thought_level`) that Cursor defaults; `efforts = ()`.
- **D8** System prompt: `_cursor_argv` writes `<run_dir>/cursor_agent/instructions.md` (not `cursor/`: that is the poll's byte-offset file) Skills ride in the same file, listed by SKILL.md path (`_skills_note`): `cursor-agent acp` ignores `--plugin-dir`, `--add-dir` and a workspace `.cursor/skills`, and only loads `~/.cursor/skills`, which fused does not write (measured on CLI 2026.11.22-e9042b2 with a real ACP turn).
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
  `method: "cursor"`, `org` = the team. Observed: `status` ignores the key
  (`unauthenticated` until the first key-signed run), and that first run
  stores the exchanged token under `~/.config/cursor/auth.json`, after which
  `status` says `authenticated` with the key owner's `userInfo`. The CLI
  reaches for the key before a stored login, so with the key set the method
  stays `api_key` whatever `status` says. Models: `cursor-agent acp` →
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
  one-row static seed (`auto`) in the catalog. When the account lists its
  own Auto row (`auto-smart`) that row goes first IN PLACE of the seed (one
  "Auto", not two), and `default_from_discovered` makes it the default; a
  list with no Auto row keeps the seed first.
- **D12** no-equivalent table: AskUserQuestion → Cursor's question card
  (options + `__ask_question_skip__`) is answered `skip` by the host; plan
  card (`cursor/create_plan`) → not offered; bg tasks / snapshots /
  artifacts / worktrees → hidden; subagents → not negotiated (no
  `subagents` client capability), so they stay inside the CLI; recap →
  `cursor-agent -p --output-format json --mode ask --trust --workspace
  <empty mkdtemp> --model <m>` with the prompt on stdin and NO positional
  prompt (observed: given a positional prompt the CLI IGNORES a piped stdin;
  `--model auto` is accepted by `-p`, and left out the run would take the
  persisted last-picked model), reply = the last
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
  `completed`/`failed`, or when a card for it arrives. Observed live: a
  shell call's card arrives AFTER its `in_progress`; a REJECTED call ends
  with a bare `tool_call_update {status: "completed"}` (no content, not
  `failed`), which the host writes as an error result "The user denied this
  request."; an EDIT is announced `Edit File` with an empty `rawInput`, the
  path comes in a refresh, and the diff only with `completed` — so an edit
  row waits for `completed` (or its card). For a NEW file the diff is
  mangled (`oldText: "-- /dev/null"`, `newText: "++ b/<path>\n<content>"`);
  the host repairs it to an empty old text (`_normalize_diff`), and an empty
  old text is what makes the row a Write.
- **D15** erase removes the fused transcript only; Cursor's chat stores are
  its own.
- **D16** ACP only for sessions; `-p` only for the recap.
- **D17** a sixth Preferences toggle `cursor_enabled` (default off); the PUT
  rule becomes "at least one of the six on".
- **D18** label **"Cursor"**, product name "Cursor CLI"; notice row
  `"Handled by Cursor · model <id>"`. Mark = **Cursor's own mark** (the cube)
  + word (owner, 2026-11-20: "use cursor's icon" — the Codex exception again,
  reversing the first draft's pointer-arrow glyph of our own). Path data from
  simple-icons 16.38.0 (CC0), filled with `currentColor` in a 24-unit box, in
  `AgentMark.tsx`.
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

## Warm spare (per folder)

A Cursor chat's first message used to wait about 4.5 s. Measured against the
real CLI: `initialize` takes 0.25 s, the first `session/new` in a process
4.1-4.8 s, and a later `session/new` in the same process 1.2 s. Only a
completed `session/new` warms the process. Cursor loads rules and skills from
the PROCESS cwd, so a warmed process can only serve a chat with that cwd.

- **Trigger.** The composer's `defaults` call resolves the backend; when it is
  `cursor` (and the session has no live host) `_prewarm_cursor(file)` runs.
  It never raises and never changes what `defaults` answers.
  A spare is started only when Cursor is the default agent (the
  `default_backend` pref), so a user whose default is another agent pays
  nothing for it; a Cursor chat opened while another agent is the default
  starts cold.
- **Spare.** The server starts at most one spare per cwd (keyed by realpath):
  `cursor_host.py` in spare mode. It spawns the CLI, runs `initialize` and a
  throwaway `session/new`, writes `<runs parent>/spares/<id>/ready`, then waits
  on stdin for the real request.
- **Claim.** `_start` hands the chat to a ready spare whose cwd and argv match
  (the request is written to the spare's stdin); otherwise, or if the pipe is
  broken, it spawns a cold host as before. A spare is claimed at most once.
- **Limits.** At most 2 spares (the oldest is retired for a third); an
  unclaimed spare is retired after 5 minutes. Retiring means closing its
  stdin: the host kills its CLI and exits. A spare whose CLI dies or whose
  warm-up fails exits without `ready`. Nothing is started when the backend is
  off, the binary is missing/broken, or the cached health says signed out.
- **Opt-out.** `FUSED_CURSOR_PREWARM=0` (also `false`/`off`/`no`).
- **Leftover.** The throwaway session leaves one
  `~/.cursor/acp-sessions/<id>/meta.json`; fused never reads it. Updates for
  the throwaway session id are dropped by the host.

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

## 10. Verified live vs read vs assumed

**Verified against the real CLI**, release `2026.11.19-f005e9d`, Linux x64,
signed out (2026-11-20) and signed in by `CURSOR_API_KEY` with real model
turns (2026-11-20; `tests/test_cursor_live.py`, run with `pytest -m
integration -n0`, plus the app driven end to end in a browser):

* `--version` prints the bare release string; `status --format json` has the
  shape in D10; `cursor-agent acp` starts with no global options;
* the `initialize` reply; `cursor/list_available_models` answers without a
  session, BASE ids under the parameterized picker (`auto-smart`, `gpt-5.5`,
  …; no variant strings); signed out, `session/new` fails `-32000` with the
  sentence under `data.message`; the ACP server ignores stdin EOF and exits
  on SIGTERM;
* health signed in with `method: "api_key"` and the account's model list;
  `status` ignoring the key until the first key-signed run (D10);
* a plain turn: init row, the D18 notice, streamed `agent_message_chunk`s,
  one `success` result; a multi-block `session/prompt` reaches the model in
  order (a rule placed only in the D8 context block was followed);
* `session/set_mode agent`, `session/set_model` live (the next turn's rows
  name the new model), and `auto` refused as a wire id (D7);
* Ask mode: a shell command raises a card (`` `cmd` `` title, the reason —
  "Not in allowlist: echo" — as content, the option ids in D4); allow runs
  it, deny does not; an edit inside the workspace raises NO card, in any
  mode (Cursor's own policy); `acceptEdits` and `auto` behave as D5 says;
  the tool row carries the whole command, never a half-streamed one (D14);
* `session/cancel` ends the turn `{stopReason: "cancelled"}` within
  milliseconds;
* `session/load` replays the history before replying: no duplicate turns in
  the transcript, the context block stripped, and the model remembers the
  earlier turn;
* the recap one-shot: `-p` reads a piped stdin only WITHOUT a positional
  prompt; `--model auto` accepted; the `{type: "result"}` line as in D12;
* `session/set_model` and `-p --model` both persist the pick to
  `~/.cursor/cli-config.json` (`selectedModel`).

**Read in the bundle, not exercised live**: `persistAllowlistEntry` on
`allow-always` (the host never picks it); the mode-id aliases
(`code`/`architect`/`chat`); the question card and `cursor/create_plan`;
the edit/MCP card shapes (no edit card was raised live); `NO_OPEN_BROWSER`
and the interactive `login`; the installer's paths; `--resume` in a
terminal.

**Assumed** (design absorbs each): the update command being enough for
every installer layout; the floor version; that a team admin's allowlist or
"Run Everything" restriction only changes WHICH calls raise a card.
