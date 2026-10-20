# Bot terminals — plan

## Goal

An ordinary agent-engine bot can drive its own visible shell, so the GitHub bot can use `gh` instead of the browser. Two switches gate it: a global "Bot terminals" preference (Preferences › Optional features, off by default) that also acts as a kill switch, and a per-bot "Terminal" opt-in (off by default, presets included). The bot gets a pty session that it owns, reusing `fused_render/pty_session.py`, and three MCP tools gated by the existing approval-card and allow-rule machinery. The terminal shows in the bot's screen next to the browser.

Out of scope for v1: Super Bot (it already has Bash), the steps engine (local models), and attaching a bot to one of the user's terminals.

## Tasks

TDD throughout: write the failing test first in the named file, following the pattern cited.

### 1. Pty registry: owned sessions, env overrides, last-command output

- **`fused_render/pty_session.py`**
  - `PtySessionRegistry.create` (:597) gains `owner: str | None = None` and `env: dict | None = None`.
  - Merge `env` into `profile.env` between `resolve_profile` and `PtySession(...)` (:604-610).
  - Store `owner` on `PtySession` and add it to `snapshot()` (:448).
  - Sessions whose owner starts with `bot:` get their own cap, `MAX_BOT_SESSIONS = 4`, next to `MAX_SESSIONS` (:69). User sessions keep counting only user-owned sessions toward 8.
  - Add `last_output(max_lines)`. It renders only the bytes between the latest OSC 133;C and 133;D through `render_screen_text` (:89). If the ring has dropped the start, prefix "[output truncated]".
- **`fused_render/shell_integration.py`**
  - `ShellEventParser._handle` (:392) records the byte offset of 133;C (:400) and 133;D (:405).
  - `PtySession` passes the running scrollback byte total into `feed()` (:370) so those offsets line up with `_scrollback`.
- **`fused_render/server/routers/terminal.py`**
  - `GET /api/terminal` (:76) leaves out sessions with a `bot:` owner, so they never appear in the user's drawer.
  - `/{sid}/text`, `/{sid}/input` and the stream WS keep working for them, since the bot UI mounts them by id.
- **Tests:** add cases to `tests/test_pty_session.py` (owner, env, bot cap, `last_output` slicing and truncation) and `tests/test_terminal_routes.py` (drawer list excludes bot-owned).
- **Verify:** `pytest tests/test_pty_session.py tests/test_terminal_routes.py tests/test_terminal_text.py -q` is green.
- **Commit:** `Terminals: owner-tagged sessions, env overrides, last-command output`

### 2. Global preference "Bot terminals"

- **`fused_render/shell/prefs.py`**
  - Add `bot_terminals_enabled()`, mirroring `marketplace_enabled()` at :289-303 (key `bot_terminals_enabled`, literal `True` only).
  - Add it to the GET payload as `"bot_terminals": {"enabled": ...}`, beside :801.
  - Add the PUT branch, mirroring :1071-1076, and add the key to the accepted-keys error text at :1320.
  - When the PUT turns the pref off, call the bots hook from Task 3 that kills every `bot:`-owned session.
- **Frontend**, mirroring the Marketplace sites:
  - `frontend/src/platform/lib/api.ts`: type at :1469, put helper at :1837.
  - Create `frontend/src/platform/lib/bot-terminals-flag.ts`, a copy of the `marketplace-flag.ts` pattern (:65, :74).
  - `frontend/src/shell/settings/registry.ts`: new entry in the "features" group, after :344-351.
  - `frontend/src/shell/settings/toggles.tsx`: new `BotTerminalsControl`, mirroring :181-191. The description says it lets bots you opt in run commands in their own terminal, with approvals.
  - `frontend/src/shell/settings/controls.tsx`: map entry beside :79.
- **Tests:** in `tests/test_shell_prefs.py`, a case mirroring `test_marketplace_enabled_defaults_off_and_toggles` (:217-233), plus one asserting that turning the pref off kills bot-owned sessions (fake the hook).
- **Verify:** `pytest tests/test_shell_prefs.py -q` is green.
- **Commit:** `Preferences: Bot terminals optional feature (off by default)`

### 3. Per-bot "terminal" setting and session lifecycle

- **`fused_render/bots/bot.py`**
  - New meta bool `terminal`, default false, read lazily the way `build_access` is.
  - Add `"terminal"` to the clone copy at :4298-4304.
  - Do not add it to `MANAGE_FIELDS` (:3948), so it stays user-only.
- **Terminal eligibility.** Add `terminal_enabled(bot)` in `fused_render/bots/tools.py` beside `effective_build_access` (:365). It returns true only when all of these hold:
  - the pref is on;
  - `meta.terminal` is true;
  - the bot is not Super Bot (`is_super`, :346);
  - `_engine_for` (`bot.py:486`) resolves to `agent`.
- **Session lifecycle**
  - New `Bot.terminal_session()`. It lazily creates the session through `REGISTRY.create` with:
    - `owner="bot:<bid>"`;
    - `cwd` set to the bot's own folder from `fused_render/bots/paths.py`;
    - `env={"PAGER":"cat","GH_PAGER":"cat","GIT_PAGER":"cat"}`.
  - It reuses the live session across tasks and recreates it if the session died.
  - The session closes in `Bot.shutdown` (:2752), which `delete` (:4361) also goes through, and in `_settings` when `terminal` flips to false.
  - Add a module-level `close_all_terminals()` for the pref kill switch in Task 2.
- **`Bot.summary()`** (:1858) adds `terminal_id` (null when there is no live session) and `terminal_available` (from `terminal_enabled`).
- **`fused_render/bots/routes.py`**: accept a `terminal` bool in `bots_create` (:316-324) and `_settings` (:666-689), mirroring `build_access`. A non-bool, or a Super Bot, is a 400.
- **Tests:** in `tests/test_bots_routes.py`, following the `build_access` cases at :65-72 and :100:
  - create and settings round-trip `terminal`;
  - Super Bot is refused;
  - flipping it off kills the session;
  - delete kills the session.
- **Verify:** `pytest tests/test_bots_routes.py -q` is green.
- **Commit:** `Bots: per-bot Terminal opt-in and owned session lifecycle`

### 4. Terminal tools and gating

- **`fused_render/bots/tools.py`**
  - **Specs.** Add three `TOOL_SPECS` entries next to the existing ones at :128-240:
    - `terminal_run` takes `command` and `wait_s` (default 30, clamped 1..120). It types the command plus a newline, waits for 133;D or the timeout, and returns `last_output` (200 lines), the exit code and the cwd. On timeout it returns "still running" plus the screen tail.
    - `terminal_read` takes `lines` and returns the screen text.
    - `terminal_interrupt` sends Ctrl-C.
  - **`roster()`** (:498-532) drops all three unless `terminal_enabled(bot)`.
  - **`describe()`** (:914) shows `$ <command>` for `terminal_run`.
  - **`execute()`** (:1314) gets a branch for each tool. It re-checks `terminal_enabled` and refuses with "error: bot terminals are off (Preferences › Optional features, then this bot's settings)", mirroring the Marketplace refusal at :1433-1437. A 409 from a busy session becomes "terminal is busy; read it or interrupt".
  - **`risk()`** (:1040), for `terminal_run`:
    1. A command containing shell metacharacters (`;` `|` `&` `` ` `` `$(` `>` `<`, or a newline in the body) always returns a reason.
    2. An outside sender (`chan.is_outside(bot.task_via)`, `channels/base.py:~92`) always returns a reason.
    3. Otherwise it returns a reason unless `forever_match` finds a `terminal` rule.
    4. `terminal_read` and `terminal_interrupt` return "".
  - **Allow rules**
    - Add `terminal` to `FOREVER_KINDS` (:390).
    - In `forever_rule` (:408), the `terminal` key is the executable plus following words up to the first token that starts with `-`, contains `/`, or is numeric, at most 3 words. So `gh pr view 12` keys as `gh pr view`.
    - `forever_rule` returns None when the command has metacharacters or comes from an outside sender.
- **`fused_render/bots/agent_engine.py`**
  - In `_act` (:1773-1810), `terminal_run` never takes the `effective_approval == "auto"` shortcut. Only a forever match skips its card.
  - Add one prompt line near :153: when `terminal_run` is among your tools, prefer CLI tools such as `gh` over browsing, and read the output instead of screenshotting.
- **`fused_render/bots/steps_engine.py`:** no prompt change. `execute()` refuses terminal tools for a bot whose engine is not `agent`.
- **Tests:** new `tests/test_bots_terminal.py`, using the fixtures in `tests/_bots_conftest.py` and the fake CLI in `tests/_bots_fake_claude.py`. Cover:
  - roster gating across all four conditions;
  - metacharacter card with no forever offer;
  - allow-rule key derivation;
  - forever-match skip;
  - auto-mode still carding;
  - outside sender always carding;
  - pref-off refusal;
  - a real `terminal_run` of `echo hi; exit code` against a live pty (approved), returning only that command's output.
- **Verify:** `pytest tests/test_bots_terminal.py tests/test_bots_agent_engine.py tests/test_bots_steps_engine.py -q` is green.
- **Commit:** `Bots: terminal_run/read/interrupt tools behind approval cards and allow rules`

### 5. Bots UI: settings toggle and terminal pane

- **Bot settings:**
  - `frontend/src/apps/bots/dialogs/BotSettings.tsx`: a "Terminal" toggle row beside build_access (:243-250). Show it only when the global flag is on (Task 2's `bot-terminals-flag.ts`) and the bot is not Super. When `terminal_available` is false because the engine is steps, disable it with the hint "needs a Claude model".
  - Wire state through :58, :72 and :106, `dialogs/actions.ts` (:11, :46, :67), and `lib/api.ts` (Bot type :167, CreateBody and SettingsBody :359-368).
- **Terminal pane:**
  - `frontend/src/apps/bots/components/PreviewPane.tsx` (:201): when `b.terminal_id` is set, show a Browser | Terminal switch above `.shotwrap` (:232). The Terminal side mounts `TerminalView` (`frontend/src/platform/ui/TerminalView.tsx`, props :66-83) with `id={b.terminal_id}`.
  - Auto-switch to Terminal when the bot's latest step is a terminal tool.
- **Verify:** `bun test` on any touched bots test files is green, and `bun run build` succeeds. The orchestrator screenshots the live page; the builder does not claim UI verification.
- **Commit:** `Bots UI: Terminal opt-in and live terminal pane`

### 6. GitHub preset, docs, decision log

- **`fused_render/bots/presets/github/preset.json` and playbooks:**
  - Instructions say to use `gh` through `terminal_run` when it's available and fall back to the browser otherwise.
  - The preset seeds `terminal` allow rules for `gh pr view`, `gh pr list`, `gh pr diff`, `gh pr checks`, `gh issue view`, `gh issue list`, `gh run view`, `gh run list`, `gh repo view` and `gh search`. Do not seed `gh api`.
  - Find where presets are applied to new bots (grep `preset.json` in `fused_render/bots/`). Carry `allow_rules` through `clean_allow_rules` (:464) if they aren't already carried.
  - Installing the preset does not set `terminal: true`.
- **`docs/bots.md`:** a new §16 "Bot terminals" after §15 (:2018) covering the two switches, the tools, gating and lifecycle. Add roster lines in §6 (:672) and the `terminal` and `terminal_id` fields in §2 (:157).
- **`DECISIONS.md`:** append one entry; it is append-only. Take the next D-number from `origin/main` at commit time.
- **Verify:** `pytest tests/test_bots_terminal.py -q` (the preset-seed case is added here) is green.
- **Commit:** `Bots: GitHub preset drives gh via the terminal; docs §16`

## Decisions & risks

- **A bot-owned pty, not one of the user's terminals.** This keeps the user's shells out of reach and the drawer uncluttered.
- **Auto approval never covers `terminal_run`.** Command output (issue and PR bodies) is untrusted, so only explicit allow rules skip the card. This replaces Super Bot's `web_touched` taint with a simpler, stricter rule.
- **Metacharacters always card and are never forever-allowable.** The allow-rule check runs on typed shell text, so this is what makes allow rules safe. A missed metacharacter is a real injection path, so the test list must cover each character.
- **Allow-rule keys can be broad.** The key stops at the first flag or path, so `gh api` would cover POSTs. That's why the preset doesn't seed it and only an explicit user approval adds it.
- **Bot sessions get their own cap of 4** so bots can't starve the user's 8 drawer slots.
- **The session outlives a task and dies on delete, shutdown, opt-out or pref-off.** That gives the user a stable pane and cwd across tasks.
- **Existing risk, not fixed here:** the `/api/terminal/{sid}/stream` WS has no origin check (`terminal.py:238`; compare `run.py:152`). Ship it as its own PR first, since bot sessions add more live shells.
- **Rejected:** the argv-exec `run` tool. It is safer but gives no visible shell, and the user wants a terminal the bot drives.

## How we'll know it works

1. With the pref off, Preferences › Optional features shows "Bot terminals" off, no bot shows a Terminal setting, and a forced `terminal_run` is refused.
2. Turn the pref on and turn Terminal on for the GitHub bot. Ask it to "summarize my open PRs". A Terminal pane appears and runs `gh pr list` with no card, and the answer comes from `gh` output, not browsing.
3. Ask it to comment on a PR. An approval card shows `$ gh pr comment …`, and denying it runs nothing.
4. Text the same request from the phone. Even `gh pr list` asks.
5. Turn the pref off mid-session. The bot's terminal closes and the pane disappears.
6. Your own terminal drawer never lists the bot's session.

## Builder notes

Deviations from the plan, and why:

- Task 1/4: `ShellEventParser` got a `done_count` (commands finished) so `terminal_run` can wait for "its" command's 133;D rather than polling `running`. It was added with the Task 4 commit.
- Task 2/3: `bot.close_all_terminals` is defined in Task 2 (the pref kill switch needs it); the per-bot session logic stays in Task 3's `Bot` methods.
- Task 4: terminal allow rules match by WORD PREFIX (a stored `gh search` covers `gh search prs`), not exact key equality, so the preset's `gh search` rule is useful. `risk()` returns "" for a covered command and `_act` forces a card for every other `terminal_run`, including under "Never ask".
- Task 4: the live-pty tests rely on the real shell integration emitting 133;C/133;D under `$SHELL` in a test pty; they passed locally.
- Task 5: the Terminal row in Bot settings is a On/Off select (like its neighbours), and the pane switch lives in PreviewPane with local state. Stage (full live view) and side-app modes hide the switch and terminal. Not browser-verified by the builder.
- Task 6: the preset test lives in `tests/test_bots_presets.py` (it owns the `bot` fixture) rather than `test_bots_terminal.py`. Preset `terminal_allow` is read by `presets.presets()` and hidden from `GET /api/bots/presets`. The GitHub instructions were rewritten tighter to stay under the 700-char preset rule.
- DECISIONS.md entry is D1384 (origin/main's last was D1383 when written; renumber if main has moved).
- Fix: an allow rule may carry `source` (preset display name); it marks preset-seeded rows in Approved forever, and card-approved rules have none.
