# Gemini backend — build contract

Google Gemini CLI as a fourth agent for **Tasks and Sessions** (not Bots),
beside Claude Code, GitHub Copilot CLI and OpenAI Codex CLI. Design settled
2026-10-23. This file is the contract the work codes against; it condenses,
and where Gemini differs overrides, `docs/COPILOT_BACKEND.md`, which stays the
reference for everything a non-Claude backend shares (the seam, the row
contract's readers, the invariants, the caps table, the lifecycle maps), and
`docs/CODEX_BACKEND.md`, whose "third backend" plumbing this follows step for
step.

Verified against Gemini CLI 0.65.0 (`@google/gemini-cli`, npm) on 2026-10-23:
a live `initialize` + `session/new` handshake signed out, `--help`,
`--version`, and the CLI's own source for everything in §10 Verified.
Anything not listed there is an assumption and is marked. No signed-in turn
was run.

## 0. Verdict and seam

Same seam as Copilot (COPILOT_BACKEND.md §0): **the host process and the row
shape it writes**. Gemini CLI speaks the Agent Client Protocol (`gemini
--acp`: JSON-RPC 2.0, newline-delimited, over stdio — the same wire Copilot
speaks), so `claude_agent/gemini_host.py` is a COPY of `copilot_host.py` with
Gemini's facts in, not a shared module (the hosts stay separate files,
stdlib-only, loaded by path). It writes **the same run-dir files and the same
stream-json rows** the other hosts produce; `_poll`, `_segments_from_rows`,
`_turn_state`, `_history`, the tasks scan, the queue and the watchers are not
rewritten.

Where Gemini is SIMPLER than Copilot, the design takes the simpler road:

* the model is a live ACP request (`session/set_model`), so a model change
  never respawns;
* the approval mode is `session/set_mode` with Gemini's own ids, so a mode
  change never respawns either (no `allow_all` config option, no
  `--allow-tool write` argv grant);
* the pane MCP server rides `session/new`'s own `mcpServers` list, so no
  argv flag and no generated config beyond the `mcp.json` the other agents
  already get.

Where it is HARDER: Gemini has no system-prompt flag and ACP's `session/new`
takes no instructions, so the prompt reaches the model as a `GEMINI.md` in an
included directory (D8); its file tools read the WORKSPACE only, so a readable
directory is a spawn-time `--include-directories` grant (D9, a respawn like
Copilot's `--add-dir`); and a Google login whose token has lapsed starts the
CLI's browser flow INSIDE `session/new` (D13).

## 1. Code in the tree the Gemini work extends

`agent_roots.py` grows a fourth root; `agent_backends/` a fourth row; `prefs`
a fourth switch; `agent.py` a fourth host file and argv; the frontend a fourth
fallback row, glyph, toggle and health strip. Exact lists in §9.

```
agent_roots:   BACKENDS = ("claude", "copilot", "codex", "gemini")
               gemini_dir() -> <FUSED_RENDER_HOME|~/.fused-render>/gemini   # unnested home, like copilot_dir()
               base_dir("gemini") -> gemini_dir(); projects/sessions/transcript_roots/registry_roots follow
registry:      BACKENDS = {claude, copilot, codex, gemini}; ORDER = (claude, copilot, codex, gemini)
prefs:         AGENT_BACKEND_KEYS["gemini"] = "gemini_enabled" (off unless stored True)
               VALID_DEFAULT_BACKENDS = ("claude", "copilot", "codex", "gemini")
agent.py:      _HOST_FILES["gemini"] = gemini_host.py; _SELF_MINTED_ID_BACKENDS += "gemini"
               _NO_PREAPPROVE_BACKENDS = ("codex", "gemini"); _GEMINI_MODES
```

## 2. Decisions

- **D1** Seam as above. One host file per backend; no shared host module.
- **D2** `backend: "gemini"` explicit, per task, immutable (COPILOT D2
  verbatim). **Session id = Gemini's own**: `session/new` mints a uuid4
  (`randomUUID()` in `acpSessionManager.ts`; the `--session-id` flag is not
  read in ACP mode), so a fresh `_start` returns `session_id: ""` and `_poll`
  learns it from the host's init row — the Copilot path, through
  `_SELF_MINTED_ID_BACKENDS` and `_record_minted_session`. Gemini's own record
  of the session is its chat file (D15); fused never reads it.
- **D3** Transcript **written by the host**, Claude shape, under
  `gemini_dir()/projects/<munge(cwd)>/<sid>.jsonl`; live registry
  `gemini_dir()/sessions/<pid>.json` (same `{sessionId, cwd, status,
  updatedAt, pid}`). A resume is `session/load {sessionId, cwd, mcpServers}`,
  which streams the history back as `user_message_chunk` /
  `agent_message_chunk` updates — `streamHistory` is NOT awaited before the
  reply (verified: the first update is queued ahead of the reply, the rest
  land after it), so the host keeps `replaying` past the reply until the
  updates have been quiet for `_REPLAY_QUIET_S`, bounded by
  `_REPLAY_SETTLE_S`, and only then backfills and sends the first prompt;
  turns taken in a terminal (`gemini --resume <sid>`) are
  backfilled by the same text-match diff `copilot_host.backfill_replay` does.
- **D4** Permissions through `perm/<id>.req.json` / `.res.json` (COPILOT D4).
  Gemini's `session/request_permission` carries `options` whose kinds are
  `allow_once` / `allow_always` / `reject_once` (verified in the source:
  `basicPermissionOptions` + `toPermissionOptions`; `allow_always` is listed
  FIRST and per tool type says "Allow for this session"). Answered by KIND, as
  Copilot's: allow + scope `session` → `allow_always`, allow → `allow_once`,
  deny / expired / timeout / cancel → `reject_once`; no usable option →
  `{"outcome": {"outcome": "cancelled"}}`. "Allow, and accept edits" (mode
  `acceptEdits`) → the card's `allow_once`, then `session/set_mode autoEdit`
  — an EXPLICIT session mode the host records, on any card, as Claude's own
  acceptEdits is. (Not `allow_always` on an edit card: for `replace` /
  `write_file` + ProceedAlways the CLI's `updatePolicy` calls
  `setApprovalMode(AUTO_EDIT)` itself, verified — a session-wide switch
  behind the host's back; the explicit request keeps the host authoritative.)
  "Allow, and stop asking" (mode `auto`) → the card's answer, then
  `session/set_mode yolo`. A plain "Always approve" (scope `session`, no
  mode) on an edit card is still that card's `allow_always`, which the CLI
  turns into AUTO_EDIT for the session — what the click asked for ("every
  Edit for the rest of this chat"); the host's `mode` follows the notice
  below while the pill keeps saying Ask, and `_send` never undoes it since
  host.json's mode is unchanged. Every mode change the CLI makes arrives as an
  `agent_message_chunk` whose text is `[MODE_UPDATE] <ApprovalMode id>`
  (`Session.handleApprovalModeChanged`, verified): the host reads it as the
  CLI's authoritative mode notice (`self.mode` follows it) and never as
  assistant text. `fs/*`, `terminal/*` and anything else the client is asked for →
  JSON-RPC error `-32601` (`clientCapabilities` is empty). `permission_server.py`
  stays for pane tools only (`--no-approve`), reached as an MCP server (D8).
- **D5** Modes → Gemini's approval modes, on argv at spawn (`--approval-mode`,
  which spells them in snake case: `auto_edit`, `yolo` — `_GEMINI_ARGV_MODES`;
  the enum ids below are the wire's and are refused on the flag)
  AND as `session/set_mode` after the handshake and on every switch (so a live
  switch is one request, `caps.live_mode_switch = True`, `acceptEdits` needs
  NO respawn — unlike Copilot):

  | fused mode | Gemini `ApprovalMode` id | what the user sees |
  |---|---|---|
  | `prompt` (Ask) | `default` | every edit and every shell command raises a card |
  | `acceptEdits` | `autoEdit` | edit tools run without asking; shell commands still raise a card |
  | `auto` | `yolo` | nothing asks |
  | `plan` | — | not offered (400, as for Copilot and Codex; Gemini's own `plan` mode is experimental and gated) |

  Ids verified in the source (`ApprovalMode` enum, `buildAvailableModes`); a
  `session/set_mode` naming anything else is the CLI's "Invalid or
  unavailable mode" error, and the host refuses an unknown mode before the
  wire. `_GEMINI_MODES` is spelled in `agent.py` and the host; a test pins
  them equal.
- **D6** Host **serializes prompts**: a user row arriving mid-turn is HELD in
  `inbox/` and sent after the turn's `result` (COPILOT D6 verbatim: a second
  `session/prompt` cancels the first). `interrupt` control requests →
  `session/cancel` (notification) at once, answered as Copilot's are.
- **D7** Model: `-m <id>` at spawn, `session/set_model {sessionId, modelId}`
  live (verified: `setModel` in the ACP session; `agentCapabilities` does not
  advertise it, but the dispatcher serves it). `caps.live_model_switch =
  True`; a `set_model` control request becomes that one ACP request, answered
  after Gemini's reply, and the host's `model` (every row names it) moves only
  once the CLI took it. **No reasoning effort of any kind**: `efforts = ()`,
  every model row `reasoning=False`, the pickers never show the pill, `_send`
  ignores an effort for Gemini. A spawn that named no model adopts the
  `session/new` reply's `models.currentModelId` (`auto`), so the init row, the
  D18 notice and the settings record name a real id.
- **D8** System prompt via the CLI's hierarchical memory: `_gemini_argv` writes
  `<run_dir>/gemini/GEMINI.md` = `_appended_prompt(file, pane, "gemini")` and
  spawns with `--include-directories <run_dir>/gemini`; every workspace
  directory's `GEMINI.md` is read into the system prompt
  (`discoverMemoryPaths` → `getEnvironmentMemoryPaths(workspaceContext.
  getDirectories())`, verified in the source; gated on `isTrustedFolder()`,
  which `--skip-trust` makes true). The pane tool is named
  `mcp_fused_approvals_<tool>` in that file (Claude's `mcp__<server>__<tool>`
  with single underscores): `DiscoveredMCPTool` declares an MCP tool to the
  model as `generateValidName("<server>_<tool>")`, which prefixes `mcp_`
  (verified in the source). The display title is a different string: `<tool>
  (<server> MCP Server)` for a call with no arguments, the `command` value
  alone when the call has one, else `<tool>(key: value, …)`
  (`formatToolDisplayTitle`) — `gemini_host._map_tool` reads all three for
  fused's own tools. Pane tools
  (`app_state`, `terminal_*`) via `session/new`'s `mcpServers:[{name, command,
  args, env:[{name, value}]}]` — ACP's own stdio-server shape, which Gemini
  merges into its MCP config (verified in `newSessionConfig`) — built by the
  host from the very `mcp.json` `_write_mcp_config` writes with `--no-approve`,
  ONLY when a pane or terminal origin exists. No skills ride along in v1
  (Gemini reads `~/.gemini/skills`; the run dir is not a skills root).
- **D9** Readable directories: Gemini's file tools are workspace-bound, so SHOTS
  and every `extra_read_dirs` entry go on the spawn line as
  `--include-directories` (directories that exist only); a message naming a
  directory the host was not spawned with is a **respawn** (`_send` →
  `{respawn: True}`), as for Copilot. fused CLI → `_fused_cli_dir()` first on
  the spawn's PATH; no allow-list flag exists, so the prompt note's
  "pre-approved" clause is dropped (`_NO_PREAPPROVE_BACKENDS`) — under Ask mode
  a `fused` command raises a card like any other. No terminal mirror.
- **D10** `gemini_health.py` + `gemini_login.py` + `gemini_install.py` +
  `routers/gemini_health.py` under `/api/gemini/{health, health/refresh,
  install, login, login/cancel, link-path}`, same snapshot keys as the other
  three plus `current_model`. **Signed-in probe** = the files first, then ACP:
  a Google login (`security.auth.selectedType == "oauth-personal"` in
  `~/.gemini/settings.json`) with no `oauth_creds.json` is signed out WITHOUT a
  spawn (the only thing the CLI could do is start the browser flow); otherwise
  `gemini --acp`, `initialize` → `session/new {cwd: <fused home>/gemini-probe,
  mcpServers: []}` with `NO_BROWSER=1` and `GEMINI_CLI_TRUST_WORKSPACE=true`.
  A result (`sessionId`) → signed in, and its `models.availableModels` is the
  account's model list (D11a); an error `-32000` worded as a missing
  credential ("Gemini API key is missing or not configured." — verified live
  signed out; "Authentication required." — the CLI's fallback wording) →
  signed out; anything else, a timeout (the OAuth prompt waiting on stdin
  included) → unknown, carrying the last snapshot's account and list forward.
  ONE fixed probe dir, because Gemini registers every workspace it sees in its
  `projects.json`. `account.method` = `google` | `api_key` | `vertex` |
  `gateway` from the settings file, else the environment (`GEMINI_API_KEY` /
  `GOOGLE_API_KEY`, `GOOGLE_GENAI_USE_VERTEXAI`); `email` = the cached active
  Google account (`google_accounts.json`). Binary: `FUSED_RENDER_GEMINI_BIN`,
  then PATH, then `~/.npm-global/bin/gemini`, `/opt/homebrew/bin/gemini`,
  `/usr/local/bin/gemini`, `~/.local/bin/gemini`, `~/.bun/bin/gemini`,
  `~/Library/pnpm/gemini`. Version: `gemini --version` → `0.65.0` (bare);
  floor `0.65.0`, the verified release, because it spells the flag `--acp`
  (`--experimental-acp` is a deprecated alias) and the release that renamed it
  is unknown. Install: `npm install -g @google/gemini-cli` (brew `gemini-cli`
  recognised, not run). **Login**: Gemini has no `login` subcommand; the
  module spawns `gemini --acp` WITHOUT `NO_BROWSER` and sends `initialize` →
  `authenticate {methodId: "oauth-personal"}` (the "Log in with Google" method
  every `initialize` reply lists, verified live); the CLI opens the browser,
  listens on its own loopback callback for up to five minutes, caches the
  token and records the method in `settings.json` (verified in the source); a
  result is the outcome, re-probed, never inferred. The authorize URL carries
  OAuth state and never reaches the UI; the strip says "finish signing in with
  the browser window that just opened", as for Claude.
- **D11** `GET /api/agent-backends` unchanged in shape; Gemini is a fourth row.
- **D11a** Gemini's models are the ACCOUNT's, discovered: `session/new`'s
  `models.availableModels` (`{modelId, name, description}`; the preview ids
  show only with preview access — `buildAvailableModels`, verified in the
  source) are stored in the health snapshot as `models` and served by
  `agent_backends/gemini.discovered_models` with `auto` first, every row
  effortless; the static `MODELS` are the first frame and the no-cache
  fallback. `registry.foreign_model_error` admits a discovered id.
- **D12** no-equivalent table: AskUserQuestion → none; bg tasks → none;
  snapshots/artifacts/worktrees → hidden; recap → `gemini -p <pointer> -o json
  --approval-mode default -m auto` with the prompt on stdin (the `-p` text is
  appended after stdin, per `--help`), the reply read off the JSON's
  `response`, and the run's chat file removed afterwards by its `session_id`;
  terminal continue → `gemini --resume <sid>` (assumed: the interactive flag's
  help names "latest" and an index, and `session/load` resolves a full id
  through the same selector); plan → hidden; commit subject `"Gemini: <msg>"`;
  `entrypoint` always `sdk-cli`.
- **D13** errors: a JSON-RPC error on `session/prompt` → `assistant` row
  `isApiErrorMessage: true` + `result is_error` (COPILOT D13). Markers in
  `Backend.account_error_markers` / `limit_error_markers` (seeded from the
  CLI's own wording and Google's API vocabulary; see `gemini.py`). Not
  markers, after review: `PERMISSION_DENIED` (Google's "API not enabled on
  this project" / IAM refusal — a signed-in user's problem that Sign in does
  not fix) and the bare number `401` (byte counts, ports); "unauthorized" is
  the word an HTTP 401 carries. The frontend twin (`trouble.ts`) also binds
  "sign in" to word boundaries ("design in" is not a login failure) and
  reads an ENOENT on a path inside an agent's own config dir
  (`~/.gemini/settings.json`) as `raw`, never as "the CLI is not
  installed". **The OAuth
  prompt**: with `NO_BROWSER=1` on the spawn env (`_spawn_env(backend=
  "gemini")`, setdefault), a Google login whose token cannot be refreshed
  runs the paste-a-code flow — "Please visit the following URL to
  authorize…" on STDOUT, then a wait on stdin for a code nobody will type —
  AT STARTUP, before the ACP server answers `initialize` (verified 0.65.0:
  `main` awaits `getOauthClient` when the selected method is Google and the
  browser is suppressed, then starts ACP; the first JSON line the host
  wrote is swallowed as "the code"). The host reads that line
  (`_Peer.on_text`) and fails whichever handshake request is pending —
  `initialize` included (`_wait_rpc`) — AT ONCE as signed out
  (`_SIGNED_OUT_TEXT`, matched by the `not signed in` marker), never waiting
  the handshake bound out and never showing the URL. The health probe reads
  the same line as "signed out" (D10). A related CLI inconsistency: with
  only `GEMINI_API_KEY` exported and no `security.auth.selectedType` in
  settings.json, `session/new` works (the CLI defaults the method) but
  `session/load` refuses with "Authentication required" (verified in
  `prepareSessionConfig`), so a user who never picked a method in `gemini`
  can start chats and not resume them; the sign-in brief says so.
- **D14** tool → name map: ACP `kind` → `execute`=Bash{command,description},
  `edit` new file=Write{file_path,content} / existing=Edit{file_path,diff},
  `read`=Read{file_path}, `fetch`=WebFetch{url}, `search`=Grep{pattern,path}
  (COPILOT D14), with one difference: Gemini sends **no `rawInput`** (verified
  in the source: `tool_call` carries `title`, `kind`, `status`, `content`,
  `locations`), so the title — the tool's own description of the call, a
  shell command's command line — is the command, and the path comes from
  `locations` or the `diff` content entry. An MCP tool is titled `<tool>
  (<server> MCP Server)`: fused's own becomes `mcp__fused_approvals__<tool>`,
  another server's `<server>-<tool>`. Every input carries `_acp: {kind,
  title}`, which the frontend already strips.
- **D15** erase removes the fused transcript + Gemini's own chat file(s):
  `<gemini home>/tmp/<project>/chats/session-<ts>-<sid[:8]>.jsonl` (and
  `.json`, the older shape; a name collision adds `-<n>` before the short
  id, which the glob still matches), where `<project>` is the slug
  `projects.json` assigns the cwd (`ProjectRegistry.getShortId`, not a hash)
  and `<gemini home>` is `<GEMINI_CLI_HOME|~>/.gemini`
  (verified in the source: `GEMINI_DIR`, `homedir()`, `SESSION_FILE_PREFIX`,
  `deriveSessionShortId`). Eight characters are not an id, so a file counts
  only when its text carries the whole session id (the CLI writes `sessionId`
  into every record), and every hit must resolve inside the `tmp` dir it was
  globbed under (`_gemini_chat_files`). Relocate moves the fused transcript
  bucket only.
- **D16** ACP only for sessions; `-p` only for the recap one-shot.
- **D17** a fourth Preferences toggle `gemini_enabled` (default off). The PUT
  rule becomes "at least one of the four on"; `default_backend` re-pointed to
  the first enabled in ORDER. Everything else in COPILOT D17 applies with
  "Copilot" read as "a non-Claude agent".
- **D18** agent always named: label **"Gemini"**, long form "Gemini CLI" (the
  Preferences row says "Google Gemini CLI"); first-turn notice row `"Handled
  by Google Gemini · model <id>"`; placeholders "Ask Gemini…"; "Gemini is
  thinking"; "Gemini wants to use…"; "Gemini finished"; "Continue with Gemini
  in terminal". Gemini mark = **a glyph of our own** (a four-point star), the
  Copilot rule — never Google's Gemini mark.
- **D19** every Gemini child runs with **`GEMINI_CLI_NO_RELAUNCH=1`** (the
  host's `_spawn_env`, the health probe, the login, the recap). The `gemini`
  entry point is a launcher: without that variable it re-spawns node as a
  child with a `--max-old-space-size` flag, inherits our stdio to it, and
  installs **no-op SIGINT/SIGTERM/SIGHUP handlers** on itself (verified
  0.65.0, `bundle/gemini.js`). So `terminate()` on what we spawned does
  nothing, and `kill()` kills the launcher and orphans the real CLI — which
  keeps our stdout open, so any reaper that waits for EOF (`_drain` in the
  login, the host's stop) waits forever. Found live: the Preferences Cancel
  button left "Working…" on screen with the CLI still running. In-process,
  the CLI *is* the process we hold, signals land, EOF comes. The variable is
  the CLI's own and documented; the heap flag it skips is a tuning default.

## 3. Gemini host: spawn

```
gemini --acp --skip-trust [-m <id>] [--approval-mode auto_edit|yolo]
       --include-directories <run_dir>/gemini [--include-directories <shots>] [--include-directories <extra>]...
cwd = _workdir(file); env = _spawn_env("", backend="gemini"); stdin/stdout = JSON-RPC pipes; stderr -> err.log
```

`_spawn_env(backend="gemini")` = Codex's branch plus `NO_BROWSER=1` and
`GEMINI_CLI_NO_RELAUNCH=1` (D19, set unconditionally): `CLAUDE_CODE_*` stripped, `GEMINI_CLI_HOME` and the Google credential
variables left exactly as found, `_fused_cli_dir()` first on PATH when
present. `--skip-trust` because fused is the client that chose this folder
(as Claude's spawn is); without it a user who turned folder trust on gets a
CLI that dies at startup in an untrusted cwd (verified in the source:
`isHeadlessMode()` → `FatalUntrustedWorkspaceError`).

ACP sequence (initialize and the signed-out `session/new` verified live; the
rest in the source): `initialize {protocolVersion: 1, clientCapabilities: {},
clientInfo}` → `{protocolVersion: 1, authMethods[], agentInfo{name:
"gemini-cli", version}, agentCapabilities{loadSession: true}}` →
`session/new {cwd, mcpServers}` → `{sessionId, modes{availableModes,
currentModeId}, models{availableModels, currentModelId}}` (or `session/load
{sessionId, cwd, mcpServers}` → `{modes, models}` after the replay) →
`session/set_mode {sessionId, modeId}` → per turn `session/prompt {sessionId,
prompt:[{type:"text", text}]}` → reply `{stopReason: end_turn | cancelled |
max_tokens | max_turn_requests | refusal, usage: {inputTokens, outputTokens,
cachedReadTokens, thoughtTokens, totalTokens}}`. Notifications
`session/update {sessionId, update:{sessionUpdate: ...}}` kinds:
`user_message_chunk` (replay), `agent_message_chunk`, `agent_thought_chunk`,
`tool_call`, `tool_call_update`, `plan`, `available_commands_update`,
`current_mode_update`, `config_option_update`, `session_info_update`,
`usage_update{used, size}` (once per model reply, at its END — so the count
rides the finalized row and the `result`, not `message_start`). Unknown kinds
are ignored. Mid-session: `session/set_mode`, `session/set_model`,
`session/cancel` (notification). Never `session/set_config_option` (Copilot's
switch; the fake refuses it).

## 4. Row contract (what the host writes)

COPILOT §4 verbatim with "Copilot" read as "Gemini" and these differences:
the init row's `permissionMode` is the host's live mode (moved by
`set_permission_mode`); `message_start.usage` is all zeros (no reading) and
the `usage_update` lands at the reply's end; the `result`'s `usage` is
`{input_tokens: used, …0}`; the D18 notice reads "Handled by Google Gemini ·
model <id>"; a `set_model` control request is answered `success` after
`session/set_model` replied (`error` otherwise); `get_task_output` /
`stop_task` → `control_response subtype:error, error:"unsupported by gemini"`.

Registry file: `gemini_dir()/sessions/<pid>.json`, rewritten on every turn
edge, removed in `finally` — COPILOT §4 verbatim.

## 5. Host lifecycle

COPILOT §5 verbatim, plus `oauth_prompted` (set by the stdout prose, read by
`_wait_session`) and `mode` (the live CLI-wire mode). Idle reap 30 s
(`FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS`), handshake bound
`FUSED_GEMINI_HOST_HANDSHAKE_SECONDS` (120), cancel grace
`FUSED_GEMINI_HOST_CANCEL_GRACE_SECONDS` (15). Same four
`/api/tasks/queue/event` posts. The ACP server exits on stdin EOF — same reap
road.

## 6. Invariants (each has a test, `tests/test_gemini_host.py`)

COPILOT §6 1–12 hold with "Copilot" read as "Gemini", plus:

13. The mode is set in Gemini's own ids, before the first prompt and on every
    switch; `session/set_config_option` is never sent.
14. A model change is one `session/set_model`, answered after the CLI's
    reply, and every later row names the new model; nothing respawns for it.
15. The OAuth prompt on stdout fails the handshake as signed out at once —
    inside the handshake bound — and the authorize URL reaches no row.
16. Invariant 7's grep test covers `~/.gemini` / `.gemini` / `GEMINI_CLI_HOME`
    literals: only `agent_roots.py`, `gemini_health.py`, `gemini_login.py`
    and `gemini_install.py` may name them (erase and the recap reach the dir
    through `gemini_health.config_dir`).

## 7. Caps → behaviour

```
GEMINI caps: plan_mode=False, live_model_switch=True, live_mode_switch=True,
             snapshots=False, artifacts=False, worktrees=False, bg_tasks=False,
             ask_user=False, terminal_mirror=False, mid_turn_messages=False
```
COPILOT §7's table applies for every False. The two True caps mean: `_send`
answers `{respawn: True}` for Gemini on a NEW DIRECTORY only (D9), never for
a model, a mode or an effort.

## 8. Wire changes

Nothing new in SHAPE. `backend` may now be `"gemini"` wherever it was
`"claude" | "copilot" | "codex"`. `GET /api/prefs` → `agents: {claude,
copilot, codex, gemini, default}`; PUT accepts `gemini_enabled`. New routes:
`/api/gemini/*` (D10). Catalog row for Gemini (`agent_backends/gemini.py`, the
frontend's `GEMINI_FALLBACK` must match it field for field —
`tests/test_agent_backend_pins.py`):

```
id "gemini", label "Gemini"
models: auto "Auto (Gemini picks)" (default; the CLI's own), gemini-3-pro-preview "Gemini 3 Pro (preview)",
        gemini-3-flash-preview "Gemini 3 Flash (preview)", gemini-2.5-pro "Gemini 2.5 Pro",
        gemini-2.5-flash "Gemini 2.5 Flash", gemini-2.5-flash-lite "Gemini 2.5 Flash-Lite" — every row reasoning=False
default_model "auto"; efforts (); modes ("prompt","acceptEdits","auto")
account_error_markers: ("api key is missing", "authentication required", "not logged in", "not signed in",
                        "unauthenticated", "invalid_grant", "login required", "sign in", "unauthorized")
limit_error_markers:   ("resource_exhausted", "rate limit", "quota", "too many requests", "429")
terminal_resume: cd <cwd> && gemini --resume <sid>
routes_prefix "/api/gemini"; pref_key "gemini_enabled"
```
(The ids are the ones the CLI's source names — `DEFAULT_GEMINI_MODEL`,
`PREVIEW_GEMINI_MODEL`, `PREVIEW_GEMINI_FLASH_MODEL` and the 2.5 flash pair —
a seed; the account's own list replaces it once measured, D11a.)

## 9. Workstreams and file ownership (disjoint)

- **WS-A server plumbing**: `templates/shared/agent_roots.py` (BACKENDS,
  `gemini_dir`, `base_dir`), `agent_backends/roots.py`, `agent_backends/gemini.py`
  (new), `agent_backends/registry.py`, `agent_backends/__init__.py` docstring,
  `shell/prefs.py` (key, `gemini_enabled()`, `_agents_state`, PUT rule),
  `server/routers/tasks.py` (`_export_markdown` who map, erase D15 via
  `_gemini_home` / `_gemini_chat_files`), `server/routers/claude_sessions.py`
  (`_recap_gemini` + the branch), `claude_config/skills.py` (`~/.gemini/skills`
  as a reader). Tests: `tests/test_agent_backends_plumbing.py` (roots ×4,
  prefs rule with four switches, erase, catalog has four rows, discovered
  models, invariant 16's Gemini twin), `tests/test_agent_backend_pins.py`.
- **WS-B host**: `claude_agent/gemini_host.py` (new), `tests/fake_gemini_acp.py`
  (new, stdlib, the `fake_copilot_acp.py` twin for Gemini's shapes),
  `tests/test_gemini_host.py` (new).
- **WS-C agent.py**: `_HOST_FILES["gemini"]`, `_GEMINI_CANDIDATES`,
  `_gemini_bin`, `_gemini_argv` + `_gemini_context_dir`, `_GEMINI_MODES`,
  `_backend_enabled` (`gemini_enabled`), labels + marker tables (copied from
  `gemini.py`, pinned by test), `_terminal_command` gemini branch,
  `_spawn_env(backend="gemini")`, `_SELF_MINTED_ID_BACKENDS`,
  `_NO_PREAPPROVE_BACKENDS`, `_send` (model live, effort ignored, new dir
  respawns), `_account_error` gemini texts. Tests in `tests/test_agent_backend.py`.
- **WS-D health**: `gemini_health.py`, `gemini_login.py`, `gemini_install.py`,
  `server/routers/gemini_health.py` (new), the registration line in
  `server/app.py` and the warm-up lines in `server/app.py` + `cli.py`,
  `tests/test_gemini_health.py` with a fake ACP `gemini` (stdlib).
- **WS-E frontend**: `GEMINI_FALLBACK`, `AgentMark`'s `GeminiGlyph`,
  `AGENT_ROUTES`, `Prefs.agents.gemini`, the `agent-gemini` settings row +
  control, `SETUP_WORDS.gemini` + `agentAccountLine`, `GEMINI_NAMED` trouble
  markers + brief, `TroubleCard`'s words and install line, `TroubleView`'s
  words, `rawModelIds`, the Claude Config `CliId` and reader label. `npx tsc
  --noEmit` and `bun test` pass.

## 10. Verified vs assumed

Verified 2026-10-23 (Gemini CLI 0.65.0): `--help` (every flag named here:
`--acp`, `--experimental-acp` deprecated, `-m`, `-p` "appended to input on
stdin", `-o json`, `--approval-mode default|auto_edit|yolo|plan` — the
flag's snake-case spelling, where `session/set_mode` takes the enum id
`autoEdit`; fed `autoEdit`, the flag answers "Invalid values" and exits,
verified live (`_GEMINI_ARGV_MODES` keeps the two apart),
`--include-directories`, `--skip-trust`, `--resume`, `--session-id`);
`--version` → `0.65.0`; a live `initialize` reply (`authMethods` with
`oauth-personal` / `gemini-api-key` / `vertex-ai` / `gateway`, `agentInfo`,
`loadSession: true`) and a live signed-out `session/new` → `-32000 "Gemini
API key is missing or not configured."`. Verified in the CLI's own bundled
source: `session/new` mints `randomUUID()` and answers `{sessionId, modes,
models}`; `session/load` validates the id, resolves the chat file and streams
the history; `ApprovalMode` ids `default` / `autoEdit` / `yolo` / `plan` and
`buildAvailableModes`; `setMode` and `setModel` on the session;
`session/request_permission` option kinds and "Allow for this session" first;
`tool_call` fields (no `rawInput`) and the MCP title `<tool> (<server> MCP
Server)`; `usage_update {used, size}` at the reply's end; `stopReason`
values; `--skip-trust` → `GEMINI_CLI_TRUST_WORKSPACE=true`; memory discovery
over the workspace directories gated on trust; `GEMINI_DIR = ".gemini"`
under `GEMINI_CLI_HOME` or the home; `oauth_creds.json`,
`google_accounts.json {active, old}`, `settings.json`'s
`security.auth.selectedType`; the chat file name `session-<ts>-<sid[:8]>.jsonl`
under `tmp/<project>/chats`; the `JsonFormatter` shape `{session_id,
response, stats, error}`; the OAuth web flow's stdout lines and its five-minute
wait; `authenticate` writing `selectedType`; the launcher's relaunch-as-child
and no-op signal handlers, and `GEMINI_CLI_NO_RELAUNCH` turning both off
(D19 — reproduced live: SIGTERM to the launcher ignored, the real CLI
surviving a SIGKILL of it; in-process, `terminate()` exits 0); on Linux with
no `DISPLAY` the OAuth flow takes the paste-a-code road even without
`NO_BROWSER` ("Please visit the following URL…", then "Enter the
authorization code:" on stdin), so on a headless box the Preferences Sign in
cannot finish — the strip's Cancel is the way out.

Assumed (design absorbs each): `--resume <full uuid>` in the interactive CLI
(D12); the bare MCP tool name reaching the model (D8); `--approval-mode` being
honoured in ACP mode as `session/new`'s initial mode (D5 — the host sets it
again right after); the recap's `-o json` carrying `session_id` for the
cleanup (D12); the exact wording of a signed-in turn's quota and sign-out
errors (D13).

Corrected after the adversarial fact-check of the branch (2026-10-23, bundle
+ live runs): the `--approval-mode` flag takes `auto_edit`, not the enum id
(D5, `_GEMINI_ARGV_MODES`); `session/load` replays AFTER its reply (D3,
quiet period); the model-facing MCP name is `mcp_<server>_<tool>` (D8); MCP
titles with arguments are `<tool>(key: value, …)` or the bare `command`
value (D8, `_map_tool`); `allow_always` on an edit card is a session-wide
`AUTO_EDIT` switch inside the CLI, and every mode change is announced as an
`agent_message_chunk` `[MODE_UPDATE] <id>` (D4); the `session/prompt` reply
carries `usage` (§3); `<project>` in the chat path is a registry slug, not a
hash (D15); a missing `--include-directories` entry is NOT a startup error
(live exit 0 — the `isdir` filter stays, for the comma rule and tidiness);
`session/load` of an unknown id answers `-32603` with `data.details`, not
`-32000` (the host treats any error there as a failed handshake, so no code
is assumed); the probe's `session/new` leaves `projects.json`,
`tmp/<slug>/{chats,logs,.project_root}`, `history/<slug>/.project_root` and
`installation_id` under the CLI home (D10 — all inside the CLI's own store,
nothing in the probe dir); Gemini also reads skills from the shared
`~/.agents/skills` (`claude_config/skills.py`'s reader list); the launcher's relaunch and signal
swallowing, `GEMINI_CLI_NO_RELAUNCH` (D19); on Linux without a display the
OAuth flow takes the paste-a-code road even without `NO_BROWSER`.
