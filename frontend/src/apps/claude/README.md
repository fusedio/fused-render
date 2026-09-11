# apps/claude — native Claude chat

React port of `fused_render/templates/claude/template.html` (`T`), flag-gated.
Plan: `.claude-design/design.md`; behaviour inventories: `.claude-design/inventory/`.

## Layering

`apps/claude` imports **platform only** (`scripts/check-boundaries.mjs`). It is
also the one entry in that script's `SHARED_APPS`, because it is an embedded
SURFACE rather than a route: the explorer and the canvases workspace host it as a
pane, and both are apps. Read the script's own comment for why it is not lifted
into `platform/` instead.

- `feature-flag.ts` — `native_chat_enabled` pref (`prefs.chat.native`), `useNativeChatEnabled()`.
- `ChatMount.tsx` — the switch: `<ClaudeChat/>` on, legacy `<ChatFrame/>` iframe off. Mounted at all 6 sites (00-shell-infra §1): the tasks cards wall and its popup (`shell/TaskCards.tsx`), the explorer file sidebar (`apps/explorer/Preview.tsx` → `PreviewSidebar`'s `chat` slot), the folder listing pane (`ListingPreviewPane.tsx`), the canvases workspace (`apps/canvases/CanvasWorkspace.tsx`) and the explorer content pane (`_mode=claude`). The two sites that framed a PLAIN iframe hand their old element over as `legacy` so the flag off is the same node it always was.
- `legacy-src.ts` — the six flag-off `/render` URLs in one place, pinned byte-for-byte by `legacy-src.test.ts`. `shell/schedule-lib.ts` re-exports two of them.
- `ClaudeChat.tsx` — root `.chat-root`, layout variants (split / chat-only / compact / peek / narrow), boot.
- `protocol/` — pure TS, bun-tested, no React:
  - `types.ts` every `agent.py` action's request/response (04-core-chat §B/§C).
  - `agent.ts` `runAgent(dir, action, fields)` → `POST /api/run`; per-key supersede, `AgentNeedsInstall`, `AgentError`; `resolveAgentDir(file)`.
  - `markdown.ts` `renderMd` (marked + DOMPurify) and `enhanceCodeBlocks` (hljs + copy button) — the one innerHTML funnel.
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
| left pane / split / narrow | `pane/` |
| screenshots / attachments | `shots/` (PR2) · annotations `ann/` (PR3) · sched/live/lists `sched/`, `live/` (PR4) |

## Running with the flag on

`FUSED_RENDER_NATIVE_CHAT=1 scripts/dev.sh` (env beats the pref), or Preferences → "Native chat (beta)".
Checks: `cd frontend && npm run typecheck && npm run check:boundaries && bun test`.
