# SPEC: Auto-sync app commits with their remote, and a confirm-first "Fix with Claude"

Status: agreed scope (2026-10-02). Builders: read this whole file, then append
decisions / dead ends / corrections to the **Decisions log** at the bottom.

## Context

The app makes commits on its own in workspace app folders: Claude-turn sweep
commits, and app create/delete/move commits. Those commits are never pushed,
and nothing tells the user. Local and remote drift; the next pull diverges and
the user falls back to "Fix with Claude". That sends a vague prompt (only the
last line of git's stderr, no mention of which action ran) and the AI acts
immediately without showing a plan.

## What's changing (behavior)

1. **Auto-push of commits the app makes itself.** After a Claude-turn sweep
   commit or an app create/delete/move lifecycle commit: fast-forward from the
   remote, then push. **Default branch only.** Commits the user makes in the
   git view are NOT auto-pushed.
2. **Auto-pull on app open.** The existing background check (`note_app_opened`,
   throttled 5 min/repo) now fast-forwards the default branch itself instead of
   only offering the "Update" card.
3. **Fast-forward only, never merge/rebase/force.** On a dirty tree, divergence,
   rejected push or auth failure: change nothing locally; post a **persistent**
   notification naming the repo and the reason, with **Retry** and
   **Fix with Claude** actions. One row per repo+reason (update in place, don't stack).
4. **Silent cases.** Offline / unreachable remote → silent, retried at next
   trigger. Repo with no remote, or HEAD not on the default branch → skip
   silently (no notification).
5. **Success feedback.** A pull that actually brought in commits → brief
   transient popup ("Updated <app> with N changes"). Successful push → nothing.
6. **Global setting**, on by default: "Automatically pull and push app changes".
   Off → today's behavior exactly (Update card + manual flows). With it on, the
   "Update" card for behind repos is replaced by the auto fast-forward.
7. **Fix with Claude: overview → confirm → apply → ask about push.** Applies to
   ALL three entry points: the git view's "Fix with AI" toast, the shell
   RepoUpdatesDock "Fix with Claude", and the new auto-sync failure
   notifications. Before changing anything the AI must present: the strategy
   (merge vs rebase), one line per file (keep mine / keep theirs / combine /
   new from remote / new locally — names only, no contents), and whether it
   intends to push. Nothing changes until the user approves (use the chat's
   existing plan-approval card / AskUserQuestion — see pointers). After
   applying, it shows the result and asks whether to push; it must not push
   without asking.
8. **Fix with Claude prompt carries real context:** the action taken (e.g.
   "Clicked Send (push)", "Auto-push after Claude commit", "Auto-update on app
   open") plus the actual git command, and git's **complete** output (all of
   stderr incl. "rejected… fetch first" hints, not only the last line).

## Constraints

- Pull = `--ff-only` only. Never force push. Never auto merge/rebase/stash.
- Auto-sync touches only the default branch; no-remote / other-branch repos skipped silently.
- Git-view user commits are never auto-pushed.
- Background git must never hang on credentials (keep the existing
  `GIT_TERMINAL_PROMPT=0` etc. env; timeouts). Fail fast and notify, except offline (silent).
- Mount-backed repos still refused, as today.
- Fix with Claude must not change files before approval nor push without asking.
- Setting off ⇒ identical to today.
- Auto-push must not block the Claude turn / app lifecycle request path — run it in the background.

## Out of scope

- Auto-push of git-view commits; syncing non-default branches; auto-publishing branches.
- Stash/auto-merge workarounds for failed fast-forwards.
- App-launch sweep of recent repos; per-repo opt-out.
- The per-file "resolve with AI" button on conflicted rows (already previews+confirms) — leave unchanged.
- Repo-state snapshot / app-made plain-language diagnosis in the prompt; changing the chat title.

## Code pointers (from read-only exploration — verify before relying on them)

**Commit sites (auto-push triggers):**
- `fused_render/app_git.py` — `init_repo` (:263-296, "New app from starter"),
  `commit` (:302-352, scoped add -A + commit). Callers: `server/routers/apps.py:1389`,
  `server/fs_mutate.py:997` (delete), `:1406-1412` (move/restore).
  Also the shared `<workspace>/local` repo (:237-256) — often has no remote → skip silently.
- `fused_render/templates/claude/agent.py` — `_commit_turn` (:2818), invoked from the
  sweep at :5313-5325. Note: the app's CLAUDE.md also tells Claude to commit its own
  work during a turn; the sweep runs after a clean turn — a post-turn sync should push
  whatever is ahead on the default branch, not only the sweep commit.

**Background upstream check / pull (auto-pull):**
- `fused_render/git_upstream.py` — `note_app_opened` (:683-761), `check_repo` (:223-245),
  `CHECK_TTL_S=300` (:102), `_check_slot`, in-memory `_state` (:680), `known_repos()` (:821),
  `is_known_repo()` (:845), `update_repo` (:503, `pull --ff-only origin <default>`),
  `_mutation_preflight` (:354), `_mutation_slot` (:474), git env (:82-96), mount refusal (:154-170).
  Failures are deliberately silent today (:37-41) — that changes for non-offline failures.
- `fused_render/server/routers/git_upstream.py:36-61` — GET/POST `/api/git-upstream`.
- `fused_render/server/routers/render.py:~104` — the single "app opened" trigger.
- `fused_render/app_doctor.py:618-745` — consumes git_upstream state; keep it working.

**Git view (manual ops; source of the "Fix with AI" toast):**
- `fused_render/templates/git/ops.py` — `_fetch` :1540, `_pull` :1567-1592 (diverged refusal),
  `_push` :1595-1633, git env :140-153. Check how stderr is truncated into the error message.
- `fused_render/templates/git/template.html` — `askClaudeOnError` :3437-3488 (prompt; last line
  :3460-3462), toast with "Fix with AI" :4445-4477.

**Shell dock & notifications:**
- `frontend/src/shell/RepoUpdatesDock.tsx` — polls GET /api/git-upstream (:173), Update/Switch
  (:518-523), failure + "Fix with Claude" (:534, :547, :601).
- `frontend/src/shell/repo-updates-lib.ts:155-186` — `repoFixPrompt`.
- `frontend/src/platform/lib/notifications.ts` — `notify()` (:705), `action` (:70), second action
  (:74), retention rules (~:350-381). Specs: `SPEC-actionable-notifications.md`,
  `SPEC-status-popovers.md`, `SPEC-quiet-notifications.md`.
- Server-side job rows that reach the notification panel: `fused_render/jobs.py:731-743`,
  `server/routers/jobs.py:61-64` — a possible channel for background sync failures.
- `frontend/src/platform/lib/pending-claude-ask.ts` — 60 s TTL ask hand-over.

**Claude chat plan/confirm support (for the confirm-first fix):**
- `fused_render/templates/claude/agent.py:2326-2329` (`--permission-prompt-tool` un-gates
  `AskUserQuestion` / `ExitPlanMode`), `PERMISSION_MODES` :250-278, `PLAN_TOOL` :310.
- `fused_render/templates/claude/permission_server.py:45-65,112`.
- `frontend/src/apps/claude/ui/PlanCard.tsx`; ask path `frontend/src/apps/claude/ClaudeChat.tsx:1738-1919`
  (`sendMessage(ask)` :1918) — ask is text-only today; decide whether to carry a permission
  mode (e.g. start in `plan`) or rely on prompt instructions. Record the choice below.
- Explorer hop: `frontend/src/apps/explorer/Listing.tsx:1136-1163`, `Preview.tsx:1494-1537`.

**Settings:** no git settings exist yet; find where existing global prefs live (e.g. the
indexing pref `indexing_enabled()`) and follow that pattern for the new toggle + its UI.

**Tests that pin current text/wiring (update in lockstep):**
- `tests/test_git_conflicts.py` (~342, 405, 424-454, 488, 520) — pins `askClaudeOnError` body.
- `tests/test_claude_fix_with_ai_ask.py`, `tests/test_ask_claude_hop.py`.
- `frontend/src/shell/repo-updates-lib.test.ts:150-200`.
- `frontend/src/platform/lib/explain-with-ai.test.ts:48` — explain-only prompt must NOT contain
  the fix wording; keep that distinction.
- `tests/test_git_ops.py:749`, `tests/test_git_view_renders.py` (real DOM probe of the git view).
- Do NOT edit built artifacts under `fused_render/static/shell-dist/`.

## Acceptance

- Claude-turn / lifecycle commit in an app repo on the default branch with a reachable remote
  ends with local == remote, no user action.
- Opening an app whose repo is behind & clean fast-forwards it and shows the brief popup.
- Diverged / dirty / auth / rejected → one persistent notification with Retry + Fix with Claude;
  offline → nothing; no-remote / other branch → nothing.
- Setting off → no auto pull/push, Update card returns.
- Fix with Claude from any of the three entry points: prompt contains the action + git command +
  full git output and instructs overview-first; the chat shows the overview and waits for approval
  before changing anything, then asks before pushing.

## Decisions log (append-only; builders write here)

- **Setting location.** `git_auto_sync_enabled` in `~/.fused-render/prefs.json`
  via `shell/prefs.py` (GET `git.auto_sync`, PUT `git_auto_sync_enabled`), the
  `indexing_enabled` idiom: absent or non-false reads as ON. Toggle lives in
  `Preferences.tsx` (`GitAutoSyncSection`). The engine reads it lazily
  (`git_upstream.auto_sync_enabled`), so no restart is needed.
- **How background failures reach the frontend.** Not a push channel: the
  server keeps standing failures in memory (`git_upstream.sync_failures`, one
  per repo+reason, repeats update in place) and the dock's existing 6 s poll of
  GET /api/git-upstream returns `sync_failures`, `pulls`, `auto_sync`. A
  sixth dock row kind (`SyncFailureRowView`, "Needs you") draws each failure:
  Retry (POST `sync-retry`), Fix with Claude, dismiss (POST `sync-dismiss`).
  Pull popups: new pull ids since the first poll go through `notify({title})`
  (no action, so never retained); pulls already present on first poll are
  history.
- **Claude post-turn sync.** `agent.py` cannot import `fused_render`, so
  `_commit_turn` POSTs `{action:"sync", path, trigger:"claude-turn"}` to
  `/api/git-upstream` (X-Fused, 3 s, errors swallowed). The endpoint only
  accepts paths inside an app folder and queues a background thread.
- **Retry scope.** `sync-retry` reuses the stored action and push scope of the
  failure (an open-time failure never pushes; a post-commit failure does).
- **`on_default` rows hidden while auto-sync is on**: the Update card would
  duplicate what auto-pull does. Off-default rows (Switch) are unchanged.
  With the setting off the card is identical to before.
- **Confirm-first is enforced by the prompt, not the permission mode.**
  Carrying a permission mode through `stageClaudeAsk` -> host -> ChatMount ->
  `sendMessage(ask)` touches four layers and the pending-ask TTL store; the
  prompt (FIX_PROTOCOL in `repo-updates-lib.ts`, mirrored in
  `templates/git/template.html`) instructs overview first, approval through the
  plan-approval / AskUserQuestion tools (already un-gated in agent.py), apply,
  then ask before pushing. Consequence: it is an instruction, so a model that
  ignores it is not blocked. Human verification needed with the real chat.
- **Complete git output.** `ops.py` records the last `_run` (command + all of
  stdout/stderr, hints included) and `_Refused(with_output=True)` attaches it
  as `command`/`output`; `git_upstream._refuse` does the same for Update/Switch.
  The git view's `run` stores `action` ("Clicked Send (push)" ...), `command`,
  `output` on the failed flash. Auto-sync failures store the same three.
- **Test hygiene.** `tests/conftest.py` autouse fixture forces
  `auto_sync_enabled` False so no unrelated test spawns sync threads;
  `test_git_auto_sync.py` opts in explicitly.
- **Spec pointer note.** The two-call-site claim for close_fds was not relied
  on; every new subprocess follows the git_upstream `_run` pattern.


### Failure-row actions by reason (user: "a retry button definitely won't be helping")
- `syncFailureActions(reason)` in `repo-updates-lib.ts` is the single source:
  dirty = Open git view + Fix; diverged = Fix; auth = Sign in + Retry;
  rejected / git-failed / unknown = Retry + Fix. Dismiss stays on every row.
  First action takes the head slot, second the line below the status, so a
  one-action row has no empty slot.
- **Sign in** has no shell-level entry point: the gh login flow
  (`/api/github/login`) is driven only from the git template's own panel
  (`ghLoginClick`, inside the iframe). So Sign in and Open git view both do
  what the row body already did: `navigateUrl(repoGitHref(root))`
  (`?_side=git`) and close the panel. Navigation, not an in-place URL write,
  so the mount-only `_side` read is honored.

### Code-review round 1
- **sync-retry ok (real, fixed).** The route returned `ok: True` for every
  non-`failed` status, so offline / busy / skipped hid a row the server still
  held. Now only `synced` is ok; the others return `ok: False` with a
  reason (`offline`, `busy`, or the skip `why`) and a message the row shows
  in place. Test: `test_retry_that_did_not_sync_is_not_ok`.
- **_background_check auth/git-failed (not real).** `_sync_locked` calls
  `_record_failure` before returning `failed`, so an auth failure on the
  open-time fetch already produces a persistent row; only `state` is absent,
  and `check_repo` would fail on the same fetch anyway. Pinned by
  `test_auth_failure_on_open_fetch_records_a_row_offline_stays_silent`
  (passed with no code change). Offline: no row, no state lost: `_state`
  keeps the last known entry, `_checked` is stamped, and the next trigger
  (after CHECK_TTL_S, or any app-made commit sync) re-fetches. Consistent
  with "offline stays silent, retried at the next trigger".
- **ops._LAST_RUN (real in principle, fixed).** The built-in executor is one
  subprocess per call (moot there), but the `fused` engine path's process
  model is not guaranteed one-per-request, so `_LAST_RUN` is now a
  `threading.local` set whole by `_run`. Test:
  `test_refusal_output_is_per_call_not_shared_across_threads`.
