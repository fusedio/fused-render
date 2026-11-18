# Cursor CLI as the sixth agent backend

Contract and build log for `cursor` (`cursor-agent`, also installed as `agent`).
The shape follows `docs/GEMINI_BACKEND.md` (the file-ownership checklist is its
section 9) and `docs/OPENCODE_BACKEND.md`. Verified against Cursor CLI
2026.11.13, **signed out**: nothing here was run against Cursor's servers, so
every signed-in behaviour is listed under "Verified vs assumed".

## 0. Verdict and seam

Cursor speaks ACP (`cursor-agent acp`: JSON-RPC 2.0, newline-delimited, stdio),
so `claude_agent/cursor_host.py` is `gemini_host.py` with Cursor's facts in. The
host is both halves: the ACP client and the writer of every file the rest of
the app reads for a Claude session (out.jsonl, the Claude-shaped transcript,
the registry row, `perm/`, the inbox). Cursor's own transcripts under
`~/.cursor/projects/<slug>/agent-transcripts/` are undocumented and never read.

## 1. Decisions

- **D1 Spawn.** `cursor-agent --approve-mcps --trust [--model <id>] acp`
  (`agent._cursor_argv`). The binary ladder is `cursor-agent`, then `agent`
  (accepted only when `--version` has the `YYYY.MM.DD-hash` shape, since `agent`
  is a generic name). The model is a spawn flag, so a model change respawns
  (`live_model_switch=False`). Effort: Cursor has none.
- **D2 Session id.** Cursor's own: `session/new` mints it. A resume is
  `session/load`, whose replay is backfilled into the transcript after a quiet
  period (as Gemini D3).
- **D3 Auth.** The handshake is `initialize`, `authenticate` (`methodId` from
  `authMethods[0]`, else `cursor_login`), then `session/new|load`. Signed out,
  `authenticate` never answers (measured) and `session/new` answers `-32000
  "Authentication required"`. The wait on `authenticate` is bounded by its own
  short knob (`FUSED_CURSOR_HOST_AUTH_SECONDS`, 10 s) and the host then goes on
  to `session/new`; the refusal there fails the handshake with a text carrying
  "Authentication required" and "agent login" so the account-error markers
  (and the Sign in card) match. `CURSOR_API_KEY` / `CURSOR_AUTH_TOKEN` in the
  environment satisfy the login with no browser.
- **D4 MCP injection (the main unknown).** The app's pane MCP server
  (`fused_approvals`: `app_state`, `terminal_*`, canvas, S3) rides
  `session/new` / `session/load` `mcpServers`, built from the run dir's
  `mcp.json` exactly as Gemini's is; `--approve-mcps` stands in for the
  approval prompt. Why this and not `CURSOR_CONFIG_DIR` or a project
  `.cursor/mcp.json`: `initialize` advertises `mcpCapabilities {http, sse}`, the
  standard ACP signal that `session/new.mcpServers` is honoured; a per-run
  config dir would have to be seeded with the user's login and would split the
  CLI's store, and a project `mcp.json` would write into the user's folder.
  **Unverified while signed out.** If a signed-in run shows the tools missing,
  the fallback is a per-run `CURSOR_CONFIG_DIR` seeded from the user's, with an
  `mcp.json` in it.
- **D5 System prompt.** No flag and no ACP field: `agent._cursor_context_dir`
  writes `<run_dir>/cursor/AGENTS.md` and the host puts it ahead of a NEW
  chat's first `session/prompt` on the wire (never into the echo or the
  transcript, never on a resume). Cursor also reads the folder's own
  AGENTS.md / CLAUDE.md and `.claude/skills`, `.cursor/skills`, `.agents/skills`
  and `~/.cursor/skills` itself (`~/.cursor/skills` is a Claude Config reader).
- **D6 Approval modes are the host's.** Cursor's ACP modes (agent / plan / ask)
  are not approval modes, so the host never sends one. `prompt`: every
  `session/request_permission` becomes a `perm/<id>.req.json` card (same round
  trip as Gemini). `acceptEdits`: edit-kind requests are answered allow-once
  without a card, the rest carded. `auto`: everything answered without a card
  (tool rows still written). A mode change is live and host-side
  (`live_mode_switch=True`). The option is chosen by `kind`; with no options
  offered the host falls back to the documented literals `allow-once`,
  `allow-always`, `reject-once`.
- **D7 Extension methods.** `cursor/ask_question` answers `skipped`
  (`caps.ask_user=False`), `cursor/create_plan` answers `rejected` (plan mode
  is not offered), `cursor/update_todos`, `cursor/task`, `cursor/generate_image`
  notifications are ignored, any other request gets `-32601`.
- **D8 Tool mapping.** `_map_tool` accepts a `rawInput`-bearing call and a
  title-only call, and several spellings of an MCP title; fused's own tools map
  to `mcp__fused_approvals__<tool>`. The spellings are guesses (see below).
- **D9 No usage, no compaction.** Usage rows are zero; there is no compaction
  event. Rows otherwise follow the row contract of `docs/COPILOT_BACKEND.md` 4.
- **D10 Health.** `cursor_health` never spawns `acp` and never touches Cursor's
  servers signed out: it runs `--version`, `cursor-agent status` (exit 0 and no
  "not logged in" is signed in; an API key in the environment is signed in with
  `account.method == "api_key"`), and, only when signed in, `cursor-agent
  models`. Install is `curl https://cursor.com/install -fsS | bash` (macOS and
  Linux; refused on Windows); login is `cursor-agent login` under a pty.
- **D11 Erase and recap.** The app's own run/transcript files are the source of
  truth. The CLI-side store (`<home>/projects/*/agent-transcripts/*<sid>*`) is
  removed by a bounded glob; its layout is undocumented. The recap is
  headless `cursor-agent -p --output-format json`.

## 2. Catalog row (`agent_backends/cursor.py`; the frontend `CURSOR_FALLBACK` matches it field for field, `tests/test_agent_backend_pins.py`)

```
id "cursor", label "Cursor"
models: auto "Auto (Cursor picks)" (default), gpt-5, sonnet-4.5, sonnet-4.5-thinking, composer-1
        — every row reasoning=False; a seed, replaced by the account's own `cursor-agent models` list
default_model "auto"; efforts (); modes ("prompt","acceptEdits","auto")
caps: plan_mode F, live_model_switch F, live_mode_switch T, snapshots F, artifacts F,
      worktrees F, bg_tasks F, ask_user F, terminal_mirror F, mid_turn_messages F
account_error_markers: authentication required, not logged in, not signed in, unauthenticated,
                       login required, agent login, invalid api key, unauthorized
limit_error_markers:   rate limit, usage limit, out of usage, quota, too many requests, 429
terminal_resume: cd <cwd> && cursor-agent --resume <sid>
routes_prefix "/api/cursor"; pref_key "cursor_enabled" (default off)
```

## 3. File ownership

Python: `claude_agent/cursor_host.py`, `agent_backends/cursor.py` (+ registry,
roots, `__init__`), `templates/shared/agent_roots.py`, `shell/prefs.py`,
`claude_agent/agent.py` (spawn tables), `agent_oneshot.py`,
`server/routers/{tasks,claude_sessions,cursor_health}.py`,
`claude_config/skills.py`, `cursor_{health,install,login}.py`, router and
warm-up lines in `server/app.py`, `app.py`, `cli.py`.
Frontend: `platform/lib/{agent-backends,api,claude-health,claude-setup,trouble}.ts`,
`platform/ui/{AgentMark,TroubleCard}.tsx`, `shell/settings/*`, the `backend`
unions, `local-commands.ts`, Claude Config readers.
Tests: `tests/fake_cursor_acp.py`, `tests/test_cursor_host.py`,
`tests/fake_cursor_health_cli.py`, `tests/test_cursor_health.py`, plus the
plumbing, pins, agent_backend, recap and config-api tests extended to six.

## 4. Verified vs assumed

Verified (2026.11.13, signed out): `--help` and `acp --help`; `initialize` reply
(`loadSession`, `mcpCapabilities {http, sse}`, `promptCapabilities`, one auth
method `cursor_login`, no `agentInfo`); `authenticate` silent when signed out;
`session/new` signed out `-32000 "Authentication required"`; `status` / `about`
text when signed out.

Assumed (the design absorbs each): `session/new.mcpServers` is honoured;
`session/request_permission` option ids and kinds; the title or `rawInput` shape
of an MCP tool call; the shape of `session/new`'s reply beyond `sessionId`; the
signed-in `status` and `models` output; `-p --output-format json` returning
`{result, session_id, is_error}`; whether a prompt on stdin works in `-p` mode;
the `agent-transcripts` layout; the `cursor-cli` brew cask name; whether
`--model` is honoured by `acp` (the host does not send `session/set_model`).

## 5. Known gaps

- The login strip renders a sign-in `detail` only when it has a `code` and a
  `url`; Cursor's login prints a URL only, so the strip shows the status line
  and the browser opens on its own.
- Local checks: `tests/test_agent_backends_plumbing.py::
  test_the_catalog_endpoint_serves_the_registry` and four commit-subject tests
  in `tests/test_agent_backend.py` fail in this environment on the base commit
  too (local models serve OpenCode's catalog; the sandbox's `git config` yields
  no stdout). The spawn-env tests need `COPILOT_GITHUB_TOKEN` and
  `FUSED_CLAUDE_CMD_LOG` unset.
