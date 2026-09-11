# apps/claude — native Claude chat

React port of `fused_render/templates/claude/template.html` (`T`), flag-gated.
Plan: `.claude-design/design.md`; behaviour inventories: `.claude-design/inventory/`.

## Layering

`apps/claude` imports **platform only** (`scripts/check-boundaries.mjs`). It is
also the one entry in that script's `SHARED_APPS`, because it is an embedded
SURFACE rather than a route: the explorer and the canvases workspace host it as a
pane, and both are apps. Read the script's own comment for why it is not lifted
into `platform/` instead.

- `feature-flag.ts` — `native_chat_enabled` pref (`prefs.chat.native`), `useNativeChatEnabled()`;
  and, off the SAME one prefs read, `chat_recap_enabled` (`prefs.chat.recap`, default ON),
  `useChatRecapEnabled()`.
- `ChatMount.tsx` — the switch: `<ClaudeChat/>` on, legacy `<ChatFrame/>` iframe off. Mounted at all 6 sites (00-shell-infra §1): the tasks cards wall and its popup (`shell/TaskCards.tsx`), the explorer file sidebar (`apps/explorer/Preview.tsx` → `PreviewSidebar`'s `chat` slot), the folder listing pane (`ListingPreviewPane.tsx`), the canvases workspace (`apps/canvases/CanvasWorkspace.tsx`) and the explorer content pane (`_mode=claude`). The two sites that framed a PLAIN iframe hand their old element over as `legacy` so the flag off is the same node it always was.
- `legacy-src.ts` — the six flag-off `/render` URLs in one place, pinned byte-for-byte by `legacy-src.test.ts`. `shell/schedule-lib.ts` re-exports two of them.
- `ClaudeChat.tsx` — root `.chat-root`, layout variants (split / chat-only / compact / peek / narrow), boot.
- `protocol/` — pure TS, bun-tested, no React:
  - `types.ts` every `agent.py` action's request/response (04-core-chat §B/§C).
  - `agent.ts` `runAgent(dir, action, fields)` → `POST /api/run`; per-key supersede, `AgentNeedsInstall`, `AgentError`; `resolveAgentDir(file)`.
  - `markdown.ts` `renderMd` (marked + DOMPurify) and `enhanceCodeBlocks` (hljs + copy button) — the one innerHTML funnel.
  - `recap.ts` `fetchRecap` (`GET /api/claude-sessions/recap`) + `recapAnchor` — the session-recap read.
  - later: `run-controller.ts`, `segments.ts`, `wire.ts`, `summaries.ts`, `typer.ts`.
- `params/` — `ParamsStore`: `createUrlParamsStore()` (shell URL, runtime.js D99 coalescing) and `createMemoryParamsStore()` (cards / peek); `useChatParam(s)`.
- `styles/chat.css` — `T:13-101` tokens as `--c-*` under `.chat-root`; `styles/hljs.css` scoped highlight theme.
- `ui/`, `pane/`, `shots/`, `ann/`, `sched/`, `live/` — where each old subsystem lands (design.md §1).

## Where the old subsystems go

| `T` subsystem | here |
|---|---|
| `fused.runPython("./agent.py")` | `protocol/agent.ts` |
| `fused.params.*` | `params/` |
| `renderMd` / `attachCodeCopy` / typer | `protocol/markdown.ts`, `protocol/typer.ts` |
| poll loop, seats, cards | `protocol/run-controller.ts` + `ui/` |
| — (new, no `T` original) | session recap: `protocol/recap.ts` + `ui/useAwayRecap.ts` + `ui/RecapFold.tsx` |
| left pane / split / narrow | `pane/` |
| screenshots / attachments | `shots/` (PR2) · annotations `ann/` (PR3) · sched/live/lists `sched/`, `live/` (PR4) |

## Running with the flag on

`FUSED_RENDER_NATIVE_CHAT=1 scripts/dev.sh` (env beats the pref), or Preferences → "Native chat (beta)".
Checks: `cd frontend && npm run typecheck && npm run check:boundaries && bun test`.

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

Turn it off in Preferences → Native chat (beta) → **Session recap**.
