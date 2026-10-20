# SPEC: Heads-up notes in the Claude chat

Status: build plan for branch `surya/chat-heads-up`. Append decisions and deviations to the end of this file under "Builder log".

## What it is

fused-render's own version of Claude Code's built-in "You should know" plugin (`cc-plugin-you-should-know`). While a chat turn runs, a side call reads the conversation. When it finds one thing the user should really know and has probably missed, a short card appears above the chat composer. Examples: a quiet trade-off, a constraint, an edge case, a decision with real stakes.

Most checks should produce nothing. The bar is high.

The plugin itself cannot be reused. It loads inside fused-render's `claude -p` sessions, but it stops because no terminal display is attached ("nothing attached draws"), and it has no stream-json output. That was verified on 2.1.301.

## Facts measured before this plan (do not re-measure)

- The plugin calls the model by **forking the main session**: same model, system prompt, tools and messages, plus its own prompt appended. It does not use Haiku. Tools are denied at call time and the transcript is not written.
- In fused-render the fork is `claude -p --resume <sid> --fork-session --no-session-persistence ...`. Prompt-cache results on a 34k-token session:
  - Fork with the **same tool list** and `--permission-mode dontAsk`: cache_read 34,393, cache_write 0. A full hit.
  - Fork with the same tools and the default permission mode: a full hit.
  - Fork with `--tools ""`: cache_read 649. **Cache miss.** Do not use this.
  - Fork with `--disallowedTools ...`: cache_read 0. **Cache miss.** Do not use this.
  - `--no-session-persistence` with `--fork-session` leaves no `.jsonl` behind. Verified.
- Therefore the fork's argv must be built by `agent._claude_argv(...)` with the **same launch parameters as the main session**, so the system prompt, tools and MCP roster match exactly.

## Server

### 1. Persist each run's launch parameters (`fused_render/claude_agent/agent.py`, `_start`, around line 3005)

`_start` builds `req = {...}` for the session host and passes it on stdin only. Also write it to `run_dir/launch.json`:

- Write it with `_private_open`, like `meta.json` is written a few lines above.
- Include `file, cwd, pane, cli_mode, session_id, model, effort, extra_read_dirs, new_session_id`. Leave out `agent` and `run_dir`.
- Add one short comment saying the heads-up fork reads it to rebuild an identical argv.

### 2. New module `fused_render/claude_agent/headsup.py`

Model it on the recap code in `fused_render/server/routers/claude_sessions.py` (`_recap_generate`, `_recap`, `_recap_result`, the cache and single-flight). It contains the following pieces.

**`build_fork_argv(agent, run_dir) -> (argv, cwd) | None`**
- Read `run_dir/launch.json`. Return `None` if it is missing.
- Resolve the session to fork with `agent._run_own_session(run_dir, meta)`, where `meta` is `meta.json`. Return `None` if there is none.
- Make a fresh `tempfile.mkdtemp(prefix="headsup-")` as the fork's run dir. The MCP config and permission server for the fork must never write into the live run's dir.
- Call `agent._claude_argv(fork_dir, pane, None, session_id, model, effort, extra_read_dirs, file)`. Pass `cli_mode=None` and `session_id` set to the resolved session, which yields `--resume <sid>`.
- Append `["--fork-session", "--no-session-persistence", "--permission-mode", "dontAsk", "--max-turns", "1"]`.
- `cwd` is `launch["cwd"]`, falling back to `tempfile.gettempdir()` if that dir does not exist.
- Remove the temp dir after the run, using `shutil.rmtree(ignore_errors=True)`.

**`main_steps(run_dir) -> (turn_index, steps_in_current_turn, turn_open)`**
- Read `out.jsonl` with the same row rules as `_turn_state` (agent.py:3308). Skip rows with `parent_tool_use_id` and skip `_not_a_turn_row` rows.
- `turn_index` is the number of `result` rows seen.
- `steps_in_current_turn` is the number of `tool_use` content blocks in main-thread `assistant` rows after the last `result` row. If the last row is a `result`, it counts the turn that just ended, i.e. the rows between the previous `result` and this one.
- `turn_open` uses the same rule as `_turn_state`.

**`generate(agent, run_dir) -> dict | None`**
- Build the argv and spawn it the same way `_recap_generate` spawns:
  - `subprocess.Popen` with `env=agent._spawn_env()`, `stdin=PIPE`, `stdout=PIPE`, `stderr=DEVNULL`, `text=True`, `encoding="utf-8"`, `errors="replace"`, `close_fds=False`.
  - Follow the same Popen kwargs as recap. Do not pass `cwd=` if recap does not. Check the PROJ atfork note: posix_spawn needs an absolute exe path, and `_claude_bin()` already returns one. Copy recap's spawn kwargs exactly, adding only `cwd` as recap does.
- Send the prompt below over stdin as one stream-json user message.
- Timeout is `HEADSUP_TIMEOUT = 120.0`. On timeout, kill the process and return `None`.
- Parse the terminal `result` event with the same logic as `_recap_result`. Reuse it by importing it, or move it somewhere shared if importing the router module would create a cycle. Write down which you chose.
- Parse the reply with `parse_note(text)`.

**`parse_note(text) -> dict | None`**
- Accepts the format `learn: <line>` / `tag: <You should know | Heads up>` / `explain:` followed by a `**Title**` line and `* ` bullets.
- Returns `None` for any of these: `learn: none` (case-insensitive; allow trailing punctuation), a missing learn line, a missing title, or zero bullets.
- Otherwise returns `{"learn": str, "tag": "You should know" | "Heads up", "title": str, "bullets": [str, ...]}`.
  - Cap at 3 bullets (extras dropped silently; 1 bullet is valid), the title at 120 chars and each bullet at 400 chars.
  - Strip `**` around the title.
  - Keep `**bold**` inside bullets; the card renders it.
  - Default the tag to "You should know".

**`HEADSUP_PROMPT`**
Write this in our own words. Do not paste Claude Code's text. It must say all of the following:
- You are an observer of this session. Do not use any tools.
- Find at most ONE thing the user should really know about this session and very likely does not understand. Examples: a constraint, a trade-off the agent chose, a non-obvious edge case, or a decision with a real cost in money, time, wasted work or a wrong result.
- Interesting is not important. If the best reason is "their understanding would be more complete", answer `learn: none`.
- Skip anything the user has shown they already understand, anything the main agent already explained clearly, and anything you are not confident is true.
- Default to `learn: none`; the bar is very high.
- Write for a tired reader who switched away and forgot the details. Use plain words, name the real thing (never "this" or "it"), define any technical term, and do not end with a question.
- Use the "Heads up" tag for a risk or problem with the work. Use "You should know" for understanding something that matters.
- Output exactly either `learn: none`, or:

  ```
  learn: <one plain line>
  tag: <You should know | Heads up>
  explain:
  **<two to six word title>**
  * <bullet>
  * <bullet>
  ```

  Use 1 to 3 short bullets (capped at 3 by the parser), each one sentence of at most 20 words, no recap of what the agent did, no file paths unless the path is the point. The last bullet may be one concrete thing to check or do.
- Write in the user's language, but keep the labels `learn:`, `tag:` and `explain:` and the two tag texts in English.

**Scheduler and state.** Keep module-level state behind a `threading.Lock`, keyed by `run_id`: `{turn_index_checked: set of (turn_index, steps_bucket), offered_turn: int|None, inflight: bool, note: dict|None, note_id: str, dismissed_ids: set}`.

**`poll(agent, run_id) -> dict`** is called by the endpoint. It never raises; any exception is logged and the response is `{"state": "idle"}`.

1. Resolve `run_dir = os.path.join(agent.RUNS, run_id)`. Validate `run_id` the way other routes validate run ids (look for an existing helper in `server/routers/run.py`). Return idle if the dir is missing.
2. If a note exists and is not dismissed, return `{"state": "note", "id": note_id, "note": note}`.
3. If `inflight`, return `{"state": "checking"}`.
4. Compute `main_steps`. A check is **due** when all of these hold:
   - `steps >= 6`
   - `bucket = steps // 6` has not been checked for this `turn_index`
   - `offered_turn != turn_index`

   This matches the plugin: a check at steps 6, 12, 18 and so on, and at most one note per turn.

   Also, when the turn has just closed (`not turn_open`), `steps >= 6`, and no check ran in that turn at all, run one final check. Record that as bucket `-1`.
5. If due, mark the bucket checked, set `inflight`, start a daemon `threading.Thread` that runs `generate`, then return `{"state": "checking"}`. When the thread finishes it stores the note (with a fresh `note_id` from `uuid4().hex[:12]`) and sets `offered_turn = turn_index` if a note was found. It always clears `inflight`.
6. Otherwise return `{"state": "idle"}`.

**`dismiss(run_id, note_id)`** adds the id to `dismissed_ids` and clears `note`.

Bound the state dict to the 64 most recent `run_id`s using an `OrderedDict` LRU.

### 3. Pref (`fused_render/shell/prefs.py`)

- Add `chat_headsup_enabled() -> bool`, modeled exactly on `live_previews_enabled` (line 249): only a stored `true` counts.
- Add it to the module docstring list (lines 27-28 pattern).
- Add it wherever `live_previews_enabled` is accepted by `PUT /api/prefs` and returned by `GET /api/prefs`. Follow every place `live_previews_enabled` appears in prefs.py and the prefs router.

### 4. Routes (`fused_render/server/routers/claude_sessions.py`)

- `GET /api/claude-sessions/headsup?run_id=...`:
  - Return `{"state": "disabled"}` when the pref is off.
  - Return 503 if the agent module is missing (same as recap), and 400 if `run_id` is empty.
  - Otherwise return `headsup.poll(agent, run_id)`.
- `POST /api/claude-sessions/headsup/dismiss` with JSON body `{run_id, id}` calls `headsup.dismiss` and returns `{"ok": true}`.

## Frontend

### 5. Flag

- Add `putChatHeadsupEnabled(enabled)` to `frontend/src/platform/lib/api.ts`, next to `putLivePreviewsEnabled` (around line 1835).
- Add a `chat_headsup_enabled?: boolean` field to the `Prefs` type.
- Add the hook `useChatHeadsupEnabled()` to `frontend/src/apps/claude/feature-flag.ts`. Read it from the same shared prefs GET that `queue` uses, opt-in (`=== true`), with its own listener set.

### 6. Settings toggle

In `frontend/src/shell/Preferences.tsx`, add a switch next to the Live previews switch. Copy its markup and handler exactly.
- Label: "Heads-up notes in chat".
- Description: "While Claude works, a side check looks for something important you might miss and shows a short note above the message box. Uses extra tokens from your Claude plan."

### 7. Card: `frontend/src/apps/claude/ui/HeadsUp.tsx` plus the hook `ui/useHeadsUp.ts`

**`useHeadsUp({ runId, running, enabled, controller })`:**
- While `enabled && runId`, poll `GET /api/claude-sessions/headsup?run_id=` every 10 s while `running`.
- When `running` goes from true to false, poll once more, 1.5 s later.
- Stop polling when `running` is false and the state is not `checking`. While it is `checking`, keep polling every 5 s.
- Returns `{ note, id, dismiss(), gotIt(), askInChat() }`.
- `dismiss` and `gotIt` both POST to the dismiss endpoint and hide the card. They are separate functions so telemetry can tell them apart later. No telemetry now.
- `askInChat` sends `Tell me more about this: ${title}\n\n${bullets joined with "\n"}`.
  - If a turn is running, use `controller.sendFollowUp`. Otherwise use `controller.sendMessage`. See `protocol/controller-api.ts:470/486`.
  - Then dismiss.
- The card clears itself after the user sends 2 more messages of their own. Count them by watching the controller's sent-message count or the transcript's user turns, whichever ClaudeChat already exposes. Write down which.

**`HeadsUp.tsx`** renders:
- the tag as a small label ("You should know" or "Heads up"),
- the bold title,
- the bullets, using the chat's existing inline-markdown renderer for `**bold**`,
- and three buttons: **Got it**, **Ask in chat**, **Dismiss**.

Style it after `ui/PermCard.tsx` and `PlanCard.tsx`, using existing classes and tokens only. It must follow light and dark themes. Keep it compact: no taller than about 40% of the chat pane, and scroll inside if longer.

**Wiring in `ClaudeChat.tsx`:** render the card in the same slot the recap uses. See `recap={<RecapFold .../>}` around line 5142 and the `useAwayRecap` call around line 4754. If a recap and a heads-up are both present, show the heads-up first. Get `runId` and `running` from the same controller state the recap logic reads.

## Tests (scoped only; never the full suite)

- `tests/test_chat_headsup.py`:
  - `parse_note`: a none variant, a full note, a missing title, too many bullets, and the default tag.
  - `main_steps` on hand-written `out.jsonl` fixtures, including subagent rows (ignored), a closed turn and an open turn.
  - `poll` scheduling, with `generate` monkeypatched: idle below 6 steps; checking at 6; no second check in the same bucket; a check again at 12; none after a note in that turn; the final-check-at-turn-end rule.
  - `build_fork_argv`:
    - it contains `--resume <sid>`, `--fork-session`, `--no-session-persistence`, `--permission-mode dontAsk` and `--max-turns 1`;
    - it does NOT contain `--tools` or `--disallowedTools`;
    - its mcp-config path is not inside the live run_dir.
  - The route returns `disabled` when the pref is off, and 400 without `run_id`.
  - Prefs round-trip for `chat_headsup_enabled`.
- Frontend:
  - a bun test for `HeadsUp.tsx` rendering and the three buttons' callbacks;
  - a bun test for `useHeadsUp`'s polling stop rule, with a mocked fetch.

  Watch out: `mock.module` is process-wide in bun. Mock fetch locally rather than mocking shared modules.
- Some pytest files grep literal frontend source lines (for example around `AppPage.tsx` and `ClaudeChat.tsx`). Run `pytest -k "claude_chat or recap or prefs"` once after editing `ClaudeChat.tsx`, to catch any source-pinning test you broke.

## Out of scope for this branch

- Telemetry.
- The plugin's "make an artifact" and "explain simpler/more" follow-ups.
- Any mid-turn server-side scheduler. The page's polling drives it, so a closed tab means no checks. That is acceptable.

## Builder log

(append here)

### Fix round 1

- Server: a note expires once the current turn index reaches the offered turn + 2 (cleared in `_poll`); `_worker` drops a note when a newer turn is open at completion.
- `build_fork_argv`: model/effort come from `host.json` when valid (set_model rewrites only that file), else launch.json; adds `--settings '{"disableAllHooks": true}'`.
- `main_steps` is incremental: per-run_dir LRU cache (offset, last-64-byte tail check, counters), complete lines only. Beyond the brief, a mismatch of the tail bytes before the offset also forces a rescan, so a same-size rewrite is caught.
- Frontend: `useHeadsUp` resets note and poll state on `runId` change (the shown note also carries its runId); `disabled` stops polling; `askInChat` sends first and only then hides and dismisses, with a rejection caught and `console.warn`ed.
- prefs.py docstring: removed the stray blank line.

### Decisions and deviations (builder)

- **Fork cwd without `Popen(cwd=)`.** `--resume` finds the session under the cwd's project dir, so the fork must start in the main session's cwd. But recap spawns with no cwd (a `cwd=` forces fork+exec, PROJ SIGSEGV). So on POSIX `headsup._spawn_argv` wraps the argv as `/bin/sh -c 'cd "$1" || exit 1; shift; exec "$@"' sh <cwd> <argv...>` (absolute `/bin/sh`, `close_fds=False`, no `cwd=` kwarg). On Windows it passes `cwd=` (no fork path there). Test: stub CLI reports its getcwd.
- **`_recap_result` not reused.** It ends in the recap's plain-text and length rules, which would mangle a structured note, so `headsup._result_text` re-implements the terminal-`result` scan (same clean-success rule).
- **`turn_index` meaning.** `main_steps` returns the 0-based index of the turn the steps belong to (results seen BEFORE it), not the raw count of `result` rows. With the raw count, the closing `result` would renumber the turn and defeat the per-turn bucket dedupe and the one-note-per-turn rule.
- **launch.json includes `backend`.** Beyond the spec's field list, so a Copilot run is never forked as if it were Claude (`build_fork_argv` returns None for non-claude). `argv` (Copilot only) is excluded.
- **`build_fork_argv` returns a `Fork(argv, cwd, dir, session_id)` namedtuple**, not `(argv, cwd)`: the caller needs the temp dir to remove it and the session id for `_spawn_env`.
- **Prefs shape.** The server/GET key is `chat_headsup: {enabled}` (the shape every other flag uses, e.g. `live_previews`), PUT body key `chat_headsup_enabled`. The frontend `Prefs` type has `chat_headsup?: {enabled: boolean}`, not a flat `chat_headsup_enabled`.
- **Auto-clear counting.** The card clears after 2 more user rows in `state.turns` (counted in ClaudeChat with `useMemo`, passed as `userTurns`); the base is the count when the note first arrived.
- **Poll timing seam.** `useHeadsUp` takes an optional `timing` (tests use ms-scale intervals); `nextPollDelay` is exported and unit-tested for the 10 s / 5 s / 1.5 s rules. (Superseded by the refresh fix: every mount polls once, even when idle.)
- **Settings toggle** is its own section "Heads-up notes" (heading + the spec's description + a "Heads-up notes in chat" checkbox), in the Live previews section's position. Markup copied from the Task recaps section (opt-in, not the inverted live-previews checkbox).
- **Card placement.** Rendered in the Transcript `recap` slot before `RecapFold`, per spec. Claude-backend chats only (`chatBackend === "claude"`).
- **Pre-existing failures** (not from this branch; reproduced with agent.py reverted to HEAD): `tests/test_agent_backend.py::test_copilot_spawn_env_strips_claude_and_adds_the_gh_token` and `::test_poll_context_has_no_window_until_known_and_claude_is_untouched`.

- **Fix round 2.** CI has no `claude` on PATH, so 7 `build_fork_argv` tests in `tests/test_chat_headsup.py` raised `FileNotFoundError: claude CLI not found` from `agent._claude_bin()`; they only passed locally because this machine has the CLI installed. The shared `agent` fixture now patches `_claude_bin` on the loaded module to return `FUSED_RENDER_CLAUDE_BIN` when set (the two `generate` tests point it at a stub CLI) and `/fake/bin/claude` otherwise. No production change. Verified with `PATH=/usr/bin:/bin FUSED_RENDER_CLAUDE_BIN= HOME=<tmp>`: 37 passed; normal run: 37 passed. The rest of the file is already machine-independent (tmp_path, `FUSED_RENDER_HOME` override).

- **Fix round 3 (PR review).** `useHeadsUp`: the polling effect resets `lastState` to idle on every enable/run, so re-enabling the pref after a `disabled` answer polls again; `visible` now requires `enabled`, so the card hides the moment the pref goes off. The 2-turn expiry count moved into an exported pure helper `countUserTurns` (useHeadsUp.ts, re-exported from ui/index.ts) that skips `isInterruptMark` rows, matching `promptHistory`. Three new tests in useHeadsUp.test.tsx; HeadsUp + useHeadsUp suites 16 pass, `tsc --noEmit` clean. No pytest reads ClaudeChat.tsx.

- **Fix round 4.** `useHeadsUp` tick checks `dead` before writing `lastState`, so a poll in flight at teardown cannot leak a stale state (e.g. `disabled`) into the next effect. New test in useHeadsUp.test.tsx (failed first); HeadsUp + useHeadsUp suites 17 pass, `tsc --noEmit` clean.

- **Merge of origin/main (task A).** main split Preferences into a settings registry (`shell/settings/{registry,controls,toggles}`); our section became the `chat-headsup` registry row (Tasks group, "Chat") + `ChatHeadsupControl` in toggles.tsx. Row copy avoids the word "tokens" (registry.test pins `token` -> huggingface only). Merge 7ce625300, copy fix f3ee05490.
- **Fix round 5 (task B).** `countUserTurns` also skips rows with a truthy `pending` (queued/unsent optimistic rows; the run controller drops `pending` on adoption), so they cannot age the card. Test failed first; HeadsUp + useHeadsUp suites 18 pass.
- **Brevity round.** Notes were too verbose: prompt now asks for 1-3 short bullets (<=20 words, no recap, "read in five seconds"); `parse_note` caps at 3 bullets and accepts 1. Tests added first and failed; test_chat_headsup passes.
- **Refresh/restart fix.** The card vanished on refresh because an idle chat never polled, and the note lived only in the `_state` LRU. `useHeadsUp` now polls once on every mount / run / enable (a just-stopped run keeps its 1.5 s settle poll); a restored note uses the current `userTurns` as its base. `headsup.py` persists `{note, note_id, offered_turn, dismissed}` to `<run_dir>/headsup.json` (atomic temp + `os.replace`) on store, expiry and dismiss, and `_run_state` seeds from it (missing/corrupt ignored); `checked`/`inflight` stay in memory. Tests added for both sides.
