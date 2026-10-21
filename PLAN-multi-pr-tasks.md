# PLAN: multiple PRs per Claude task, PR chip popover, PR view by status

Decided by the user (do not re-open):
- A task/session can carry several PRs. Server collects every GitHub PR URL seen in the transcript and persists it per session; text-inferred PRs (title/description/last message/reply) still count.
- Chat header chip: newest PR as `PR #N` link + a `+N` button opening a popover listing the others (with status dot when known). Shown on the Task page AND in the explorer's Claude pane.
- Tasks "PR" view: grouping is repo > PR STATUS (Open, Draft, Merged, Closed, Unknown; empty omitted; "No PR" stays last). One row per (task, PR) pair. In the PR view only, the row's project chip is replaced by a `#1818` link chip to GitHub.
- PR status from GitHub via the user's `gh` token, cached server-side.

Line numbers are approximate (read on main at ccf041f8d).

## Facts checked
- Import boundaries (ARCHITECTURE.md:412, enforced by frontend/scripts/check-boundaries.mjs): platform imports only platform; apps may import platform + shell modules in SHELL_OPEN_TO_APPS (ScheduleTaskViews, tasks-lib, tasksPulse, TaskPeekWho). `apps/claude -> shell/TaskPrChip` is NOT allowed, so move the chip to platform/ and leave a one-line shell re-export.
- `gh_token()` (fused_render/copilot_health.py:282) shells out to `gh auth token` every call — cache it.
- `no_egress` is NOT global (per-module, e.g. tests/test_ai_hub_fetch.py:54). New test modules define their own / monkeypatch `urllib.request.urlopen` and `gh_token`.
- Popover primitive: frontend/src/platform/shadcn/ui/popover.tsx (Popover, PopoverTrigger, PopoverContent).
- `useSessionTask(sessionId, file)` in frontend/src/apps/claude/ui/useRecentTasks.ts:410 returns `{task, pending}`; ClaudeChat.tsx:~4946 already calls it as `head` with `state.sessionId`. No onSessionChange prop needed.
- `row_hash` (tasks.py:~5565) json-dumps the whole row, so `row["prs"]` is covered automatically (assert in a test).
- The transcript scan resets when the file shrinks, so rec["prs"] alone is not durable → persistence needed.
- tasks router already imports `run_in_threadpool` and `tasks_store`.

## A. Server accumulation + persistence
1. fused_render/server/routers/tasks.py
   - Module level: `_PR_URL_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+?)/pull/(\d{1,7})(?!\d)")`.
   - `_new_scan()` (~591): add `"prs": []`. Readers use `rec.get("prs") or []`.
   - `_absorb()` (~507): FIRST statement, before existing early returns: `if "github.com" in line and "pull" in line: _note_prs(rec, line)`. `_note_prs` runs the regex over `line.replace("\\/", "/")`, appends `{"repo","number","url"}` in first-seen order, deduped by lowercased `repo#number` (display keeps first spelling), cap 50. Do not return; existing logic continues.
   - Row builder (function with `rec = _scan(task["path"])`, ~3760; `last_reply` ~4006): `stored = tasks_store.session_prs(settings or {}, sid)`; merged = stored + rec prs not already stored. If merged != stored and sid: `tasks_store.record_prs(sid, merged)` inside try/except (OSError, ValueError). Add `"prs": merged` right after `"last_reply"` (oldest→newest; `[]` when none).
   - Check the compact sidebar projection / any row-field allowlist near ~5750: compact endpoint should not carry prs; full listing and /changes must.
2. fused_render/tasks_store.py (after record_recap ~1150, modelled on record_recap/session_recap):
   - `session_prs(state, session_id) -> list[dict]` — validates dicts (str repo, int number), rebuilds url.
   - `record_prs(session_id, prs) -> list[dict]` — `_update(SETTINGS_FILE, mutate)`, merge with on-disk (existing first, dedupe lowercased repo#number), write only if changed; empty sid → []. Comment block: `{"<sid>": {..., "prs": [{"repo","number","url"}]}}`.
   - Confirm record_settings/record_recap keep other keys (`dict(rec)`) so prs survive; regression test. Update forget_session docstring to mention the PR list.

## E. PR status endpoint
3. New fused_render/pr_status.py (stdlib only):
   - `_token()`: caches `gh_token()` 300 s on success, 30 s on None, under a lock.
   - `_CACHE[ref] = (expires_at or inf, status|None)` under a lock: merged/closed never expire; open/draft 120 s; None 60 s.
   - `parse_ref("owner/repo#n")` validated by `^[\w.-]+/[\w.-]+#\d{1,7}$` (also blocks GraphQL injection).
   - `fetch_statuses(refs) -> dict[str, str|None]`: dedupe, cap 100, serve fresh cache hits, batch misses 25 per GraphQL query: `query{ r0: repository(owner:"o",name:"n"){ pullRequest(number:1){state isDraft} } ...}` (json.dumps string literals). POST https://api.github.com/graphql, `Authorization: bearer <token>`, `urlopen(req, timeout=8)`. MERGED→merged, CLOSED→closed, OPEN+isDraft→draft, OPEN→open; anything else/error/null alias → None. No token → all misses None, no network call. Never log the token.
4. tasks router: `class PrStatusBody(BaseModel): refs: list[str] = []`; `@router.post("/api/tasks/pr-status")` with the same X-Fused guard idiom as `api_tasks_seen` (~6291); `return await run_in_threadpool(pr_status.fetch_statuses, body.refs[:100])`.

## B. Shared logic, chip, popover
5. New frontend/src/platform/lib/task-prs.ts: move `prText`, `prOfTask` here; add `type PrRef = {repo: string|null; number: number; url?: string|null}`, `prKey(ref)` = `${repo ?? "?"}#${n}`, `prUrl(ref)`, and `prRefsOf(task)`:
   - server `task.prs` (oldest→newest) first; then text inference over title, description, last_message.text, last_reply: ALL `github.com/<repo>/pull/<n>` matches (global regex, fresh per call), ALL named matches (`/pull/N`, `PRs?[ \t]*#?[ \t]*N`), bare `(?<![\w#])#([1-9]\d{1,5})\b` only when no linked/named match anywhere. Named/bare take `task.repo` (trimmed, else null); skip one whose number already appears with a known repo.
   - Dedupe by prKey (first spelling wins), then REVERSE → newest first.
6. frontend/src/shell/tasks-lib.ts (~6085-6116): delete bodies, re-export `prOfTask, prRefsOf, prKey, prUrl` (and type PrRef) from "@platform/lib/task-prs". Keep `prRefOf = (t) => prRefsOf(t)[0] ?? null` only if a caller/test still needs it.
7. frontend/src/platform/lib/api.ts (`Task`, near `last_reply?`): `prs?: { repo: string; number: number; url: string }[];`
8. New frontend/src/platform/ui/TaskPrChip.tsx (moved from shell):
   - `refs = prRefsOf(task).filter(r => r.repo)`; null if empty; `head = refs[0]`, `rest = refs.slice(1)`.
   - `<span className="task-pr-chips">` + existing `<a className="task-pr-chip">PR #{head.number}</a>` + if rest: `<Popover><PopoverTrigger asChild><button className="task-pr-more" aria-label={`${rest.length} more pull requests`}>+{rest.length}</button></PopoverTrigger><PopoverContent className="task-pr-pop" align="start"><ul>` of `<li><span className="task-pr-dot" data-status=.../><a href target=_blank rel="noopener noreferrer">{repo} #{n}</a></li>`.
   - Statuses: `usePrStatuses(open ? keys : [])`; dot omitted while unknown.
   - frontend/src/shell/TaskPrChip.tsx becomes `export { TaskPrChip } from "@platform/ui/TaskPrChip";` (TaskPeek/TaskCards untouched).
   - CSS (frontend/src/apps/claude/styles/chat.css ~701-716; existing rules are scoped to `.chat-root.chat-peek-head`): add rules scoped `.chat-root .task-pr-chip` / `.task-pr-more` so the explorer pane gets them too; popover renders in a portal outside `.chat-root`, so scope `.task-pr-pop` alone. No Tailwind preflight: reset button border/background/margin/font and ul margin/padding/list-style. 8px `.task-pr-dot` coloured by `[data-status=open|draft|merged|closed]`, var(--c-*) tokens like the existing chip. NEVER write `*/` inside CSS comment prose.
9. New frontend/src/platform/lib/prStatus.ts: `usePrStatuses(keys): Record<string, PrState|null|undefined>` — module Map cache + in-flight dedupe, one batched `POST /api/tasks/pr-status` for uncached keys (copy the X-Fused fetch idiom used for `/api/tasks/seen` in api.ts); refresh non-terminal entries older than 120 s; skip `?#n` keys; export `resetPrStatusForTests()`.

## C. Explorer Claude pane
10. frontend/src/apps/claude/ChatMount.tsx: add `prChip?: boolean`, forwarded like headerExtra (~283). ClaudeChat.tsx: add prop (~252); at ~5225 `extra = props.prChip && head.task ? <><TaskPrChip task={head.task}/>{props.headerExtra}</> : props.headerExtra`, pass only when non-null. Import from "@platform/ui/TaskPrChip". Check Topbar.tsx's merged-head variant also renders `extra`; if not, make it.
   - Add `prChip` to ChatMount in frontend/src/apps/explorer/Preview.tsx (~2239, ~2446) and frontend/src/apps/explorer/ListingPreviewPane.tsx (~175). Not in TaskPeek/TaskCards (they pass headerExtra already).

## D. PR view rework
11. tasks-lib.ts groupRows PR branch (~6228-6290):
   - signature gains optional `prStatus?: (key: string) => PrState | null | undefined` (default → undefined → Unknown).
   - each task: refs = prRefsOf(task); none → "No PR" (unchanged). Else one entry per (task, ref) into repo → status bucket. Bucket order/labels: open "Open", draft "Draft", merged "Merged", closed "Closed", unknown "Unknown"; empty omitted. Group key `pr:${repo|?}:${bucket}`.
   - TaskGroup gains `refs?: PrRef[]` parallel to `rows`. Add `flattenGroupRefs(groups): (PrRef|null)[]` in the same order as `flattenGroups` (No PR rows → null).
   - Repo ordering unchanged (recentAt, Unknown repo and No PR last). groupFoldKeys/openOnFirstVisit/seedCollapsedGroups need no change (keys stay unique).
12. frontend/src/shell/ScheduleTaskViews.tsx:
   - ~2356: `visiblePrKeys` memo from `sorted.flatMap(prRefsOf)` keys (with repo); `prStatuses = usePrStatuses(groupBy === "pr" ? visiblePrKeys : [])`; pass `k => prStatuses[k]` into groupRows, statuses in the memo deps.
   - `rowRefs = useMemo(() => groups ? flattenGroupRefs(groups) : null)`.
   - ~2393 rowKeys: suffix `@${prKey(ref)}` when rowRefs[i] is non-null (stable unique keys).
   - rowAt (~2824): pass `prRef={rowRefs?.[ix] ?? null}` to TaskRow (stable objects from memoized groups, keeps memo effective).
   - TaskRow project chip (~4194): `showProject && !prRef ? <IdentityChip .../> : null`; when prRef: `<a className="<IdentityChip classes> task-pr-num" href={prUrl(prRef)} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()}>#{n}</a>` (span if no repo).
   - expanded/peeked/selected stay keyed by task.key (both copies light up — acceptable, comment it). Keyboard nav is DOM-based; focusedKey/observer use rowKeys.
   - Board/card renderer (~6417) unchanged.

## Tests
- tests/test_tasks_prs.py (FUSED_RENDER_HOME via monkeypatch like tests/test_tasks_api.py:35): `_absorb` on tool_result (`gh pr create` output), assistant text, user lines → first-seen order, deduped (`/pull/12/files` no dup); no-URL line untouched; `_scan` incremental + truncation still returns stored PRs via the row; record_prs/session_prs roundtrip/merge/forget_session/record_settings keeps prs; row has `prs` and row_hash changes with prs.
- tests/test_pr_status.py (own autouse no-egress fixture; monkeypatch `pr_status.gh_token`/urlopen): state mapping, TTLs (monkeypatch time), merged forever, failure 60 s, no token → None without urlopen, bad ref rejected, 30 refs → 2 calls, token fetched once across calls; endpoint via TestClient with fetch_statuses monkeypatched.
- frontend/src/shell/tasks-lib.test.ts (~11625-11750): keep prOfTask tests; port prRefOf tests; rewrite PR-group and fold/openOnFirstVisit tests to new keys; add multi-PR inference (two links → 2 refs newest first; bare ignored when named exists; server prs dedupe with text), task with 2 PRs in two groups with flattenGroupRefs aligned, No PR last, status buckets via the prStatus fn.
- frontend/src/shell/TaskPrChip.test.tsx: keep existing; add 3 PRs → `PR #newest`, `+2`, popover lists the others; dot only when status known. Avoid `mock.module` (process-wide in bun) — inject or reset.
- frontend/src/platform/lib/prStatus.test.ts: batching, dedupe, TTL (stub fetch).
- ClaudeChat `prChip` render test via frontend/src/apps/claude/ClaudeChat.boot.test.tsx harness if feasible.
- Some pytest files grep literal frontend source lines (e.g. tests/test_new_task_css.py; AppPage substrings): after touching chat.css / ClaudeChat.tsx / ScheduleTaskViews.tsx run `pytest tests -q -k "css or source or task"` scoped, and grep tests/ for any string you changed.

## Scoped commands
- `pytest tests/test_tasks_prs.py tests/test_pr_status.py tests/test_tasks_api.py -q`
- from frontend/: `bun test src/shell/tasks-lib.test.ts`, `bun test src/shell/TaskPrChip.test.tsx`, `bun test src/platform/lib/prStatus.test.ts`, `bun test src/apps/claude/ClaudeChat.boot.test.tsx`, `bunx tsc --noEmit`, `node scripts/check-boundaries.mjs`, and finally `bun run build` once (catches CSS comment issues).

## Commit sequence
1. Server: scan accumulation, tasks_store record_prs/session_prs, row["prs"], tests.
2. Server: pr_status.py + /api/tasks/pr-status, tests.
3. Frontend: api.ts prs?, platform/lib/task-prs.ts, tasks-lib re-exports, prRefsOf tests.
4. Frontend: prStatus hook, platform/ui/TaskPrChip + popover + CSS, shell re-export, tests.
5. Frontend: PR view regrouping, flattenGroupRefs, row keys, `#N` link chip, tests.
6. Frontend: ChatMount/ClaudeChat prChip + explorer mount sites.

## Decisions log (builders append below)

- Popover primitive is Base UI: the trigger takes `render={<button .../>}`, not `asChild`. The chip's popover list is its own exported component (`PrPopList`) that mounts only while the popover is open, so `usePrStatuses` is asked only then; tests render it directly (react-test-renderer cannot see a portal).
- `prRefsOf` dedupes case-insensitively (the server does too); `prKey` itself keeps the spelling it is given.
- ScheduleTaskViews: the List keeps the source text `{showProject && (` (a bun test pins it), so the `#N` chip is rendered by a separate `PrNumChip` and `showProject` is passed as `showProject && !rowRefs?.[ix]`. The `groupRows(sorted, ...)` call changed text, so that pin in tasks-lib.test.ts was updated.
- Topbar rendered `extra` only on the slim line; the task-identity head now renders it too (`.c-tb-extra`), which is the explorer pane's header. The fallback heads (no task row yet) have no chip, deliberately.
- No ClaudeChat-level `prChip` render test (too heavy a harness); covered by a Topbar `extra` test plus tsc. Pulse endpoint uses a field allowlist so it carries no `prs` (tested).
- tests/test_tasks_api.py has 6 pre-existing failures (record_settings now returns `permission`) and tests/test_browser_storage_allowlist.py 1 pre-existing failure; identical on the unmodified tree.
