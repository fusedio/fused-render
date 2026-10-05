# apps/claude — native Claude chat

THE Claude chat. It began as a React port of the iframe chat page
`fused_render/templates/claude/template.html` (`T` below — the line cites in
this folder still point into it) and replaced it: since D1308 that page,
its `vendor/` and `app.py` are deleted, there is no flag, and every surface
that offers mode `claude` mounts this. `templates/claude/` keeps only
`condition.py` (the gate), `icon.svg` and a `native` marker so the registry
still resolves the mode (SPEC PT-6).
Plan: `.claude-design/design.md`; behaviour inventories: `.claude-design/inventory/`.

## Backend

`fused_render/claude_agent/` — one in-process module in the server (`agent.py`,
plus `session_host.py` and `permission_server.py`, which run as children by
path), reached through `server/routers/claude_agent.py`:

- `POST /api/claude/agent` `{action, ...fields}` — the 17 `agent.py` actions,
  every field string-shaped. 200 = the handler's dict verbatim (a handler's
  `{"error"}` included, and the folder-busy refusal for `start`/`send`); 400 =
  unknown action or unbindable params; 500 = `{error: {type, message,
  traceback}}`; 504 = `{error: {type: "Timeout", message}}` past the action's
  budget. Requires `X-Fused: 1`.
- `POST /api/claude/app-entry` `{dir}` → `{entry}` (was `templates/claude/app.py`).
- `POST /api/claude/artifacts` `{action, file, session_id}` → `{artifacts}`.

Calls carry `X-Fused-Page: fused-render://claude` (`CLAUDE_PAGE_ID`), which the
call log files as first-party. Nothing here resolves an agent directory any more.

## Layering

`apps/claude` imports **platform only** (`scripts/check-boundaries.mjs`). It is
also the one entry in that script's `SHARED_APPS`, because it is an embedded
SURFACE rather than a route: the explorer and the canvases workspace host it as a
pane, and both are apps. Read the script's own comment for why it is not lifted
into `platform/` instead.

- `feature-flag.ts` — `project_queue_enabled` (`prefs.queue.enabled`), `useProjectQueueEnabled()`.
  (Its native-chat half went with the `native_chat_enabled` pref.)
- `ChatMount.tsx` — the one mount every embed site calls: `<ClaudeChat/>` behind a `lazy` boundary, a load failure shown as an error card. Mounted at all 6 sites (00-shell-infra §1): the tasks cards wall and its popup (`shell/TaskCards.tsx`), the explorer file sidebar (`apps/explorer/Preview.tsx` → `PreviewSidebar`'s `chat` slot), the folder listing pane (`ListingPreviewPane.tsx`), the canvases workspace (`apps/canvases/CanvasWorkspace.tsx`) and the explorer content pane (`_mode=claude`).
- `ChatPlaceholder.tsx` — the skeleton every chat wait shows (`ChatFramePlaceholder`; styles in `styles/chat-frame.css`).
- `ClaudeChat.tsx` — root `.chat-root`, layout variants (split / chat-only / compact / peek / narrow), boot.
- `protocol/` — pure TS, bun-tested, no React:
  - `types.ts` every `agent.py` action's request/response (04-core-chat §B/§C).
  - `agent.ts` `runAgent(action, fields)` → `POST /api/claude/agent`; `runAppEntry`, `runArtifacts`; `AgentNeedsInstall`, `AgentError`.
  - `markdown.ts` `renderMd` (marked + DOMPurify) and `enhanceCodeBlocks` (hljs + copy button) — the one innerHTML funnel.
  - `recap.ts` `fetchRecap` (`GET /api/claude-sessions/recap`) + `recapAnchor` — the session-recap read.
  - later: `run-controller.ts`, `segments.ts`, `wire.ts`, `summaries.ts`, `typer.ts`.
- `params/` — `ParamsStore`: `createUrlParamsStore()` (shell URL, runtime.js D99 coalescing) and `createMemoryParamsStore()` (cards / peek); `useChatParam(s)`.
- `styles/chat.css` — `T:13-101` tokens as `--c-*` under `.chat-root`; `styles/hljs.css` scoped highlight theme.
- `ui/`, `pane/`, `shots/`, `ann/`, `sched/`, `live/` — where each old subsystem lands (design.md §1).

## Where the old subsystems go

| `T` subsystem | here |
|---|---|
| `fused.runPython("./agent.py")` | `protocol/agent.ts` → `/api/claude/agent` |
| `fused.params.*` | `params/` |
| `renderMd` / `attachCodeCopy` / typer | `protocol/markdown.ts`, `protocol/typer.ts` |
| poll loop, seats, cards | `protocol/run-controller.ts` + `ui/` |
| — (new, no `T` original) | session recap: `protocol/recap.ts` + `ui/useAwayRecap.ts` + `ui/RecapFold.tsx` |
| left pane / split / narrow | `pane/` |
| screenshots / attachments | `shots/` (PR2) · annotations `ann/` (PR3) · sched/live/lists `sched/`, `live/` (PR4) |

## Checks

`cd frontend && npm run typecheck && npm run check:boundaries && bun test`.
Backend: `pytest -n0 tests/test_claude_agent_api.py tests/test_claude_agent_forksafe.py tests/test_run_folder_gate.py`.

## Session recap ("While you were away")

Claude Code's `awaySummaryEnabled` fold, ported — design in
`.claude-design/session-recap.md`.

`ui/useAwayRecap.ts` binds `visibilitychange` + window `blur`/`focus` and keeps
one clock. On RETURN (never on blur — the server call is not a cache hit we have
already paid for) it asks
`GET /api/claude-sessions/recap?file=&session_id=&for_uuid=` when **all** of:
away ≥ `AWAY_MS` (60 s), the pref is on, no turn running, the composer is empty,
this `for_uuid` has not been answered before, and fewer than `MAX_FAILURES` (2)
failures this mount. `for_uuid` is `recapAnchor(state.turns)` — the last USER
turn's uuid, because assistant turns carry none, and `null` (so: no fetch) when
that turn has no uuid yet.

`text: ""` is "nothing to show", never an error. Generation is ~12 s and the UI
stays silent for it; the answer is dropped if the anchor moved meanwhile.
`ui/RecapFold.tsx` draws the row between `Transcript` and `SchedBlock`
(`.chat-recap`, `styles/transcript.css`); the body sets `?msg=<for_uuid>` so the
existing anchor scroll + flare does the scrolling.

Always on: the Preferences switch that gated it left on 2026-09-21.
