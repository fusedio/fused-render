// AI Models app entry point.
//
// Native React over the server's AI routers: the cache inventory
// (`/api/ai-models`), the runtime and catalog (`/api/ai/*`) and the Hub search
// (`/api/ai-models/hub/*`). One sidebar route, `/ai-models`, whose five tabs
// are five sub-paths beneath it (routes.ts) — the shell dispatches the whole
// prefix to a single lazy-loaded page (shell/App.tsx).
//
// It lived flat in `shell/` as eighteen `Ai*`/`Playground*` files interleaved
// with the scheduler, the queue dock and the mounts page. Nothing about it was
// ever shell: it imports only `@platform`, and the only two things outside it
// that reach in are shell surfaces LINKING here — which is the allowed
// direction (scripts/check-boundaries.mjs).
//
// **This barrel is the page's heavy surface** and is what App.tsx lazy-loads.
// Three modules are deliberately NOT re-exported through it — each is
// imported by code that runs on EVERY page:
//
//   * `lib/aiRuntime` — the shared runtime poll, read by the sidebar's live dot
//     (shell/GlobalSidebar.tsx) on every page.
//   * `playground/groups` — the capability copy, read by the Home strip
//     (shell/Home.tsx) to label its cards.
//   * `routes` — the path codec. App.tsx needs `isAiModelsPath` to DISPATCH the
//     route, which is a decision it makes on every render of every page; going
//     through this barrel for it would defeat the lazy import sitting two lines
//     below it in the same file.
//
// All three are tiny and none pulls the page in behind it. Routing them through
// here would put the entire AI Models chunk on the front door's critical path,
// which is the "chunks larger than 500 kB" warning the lazy split exists to
// fix. Same argument as `apps/claude_config/available.ts`. Import them by
// their own paths.
export { default as AiModels } from "./AiModelsPage";
