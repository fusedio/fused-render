// The /ai-models URL space: one path per tab, no query-string tab.
//
//   /ai-models/playground   (the default — bare /ai-models redirects here)
//   /ai-models/local
//   /ai-models/discover
//   /ai-models/engines
//   /ai-models/usage
//
// It used to be `?tab=`, with the default expressed as the ABSENCE of the
// param so that `/ai-models` stayed the page's URL. That is the trade this
// replaces: a tab is a PAGE here — five unrelated surfaces (a chat client, a
// disk inventory, a Hub search, an engine picker, a usage graph) that share
// only a heading — and naming four of them in a query string while the fifth
// had no name at all made the page's own address ambiguous. Bare
// `/ai-models` now redirects (App.tsx, the same render-time `replaceState`
// the shell uses for `/` → `/home`), so every tab has exactly one address and
// the default has a name like the rest.
//
// **The tabs still do not each get their own React route.** One `AiModelsPage`
// stays mounted across a tab switch, unkeyed by the nav epoch — see the branch
// in App.tsx for why (a remount re-walks every blob in the Hugging Face cache
// and throws away Discover's typed search). What changed is where the choice
// is WRITTEN, not how it is rendered.
//
// **Query params survive the move and mean what they always did.** `?model=`
// and `?cap=` seed the playground's picker (Home's cards link in with `cap`,
// a Local card's "Try" with `model`), and each stage's non-default settings
// ride along beside them. Only the tab left the query string.
//
// NO LEGACY REWRITE. `/ai-models?tab=local` does not map to
// `/ai-models/local` — an unknown sub-path falls back to the default tab
// (`tabFromPath` below), so a stale link opens the page on the playground
// rather than erroring, and that is the whole of the migration. Owner's call:
// the shape is days old and not worth a permanent alias in `router.ts`.

/** The prefix every route on this page shares. Also the bare page URL, which
 *  App.tsx redirects to the default tab. */
export const AI_MODELS_PREFIX = "/ai-models";

export type AiModelsTab = "playground" | "local" | "discover" | "engines" | "usage";

/** Tab order is TAB-STRIP order, and the first entry is the default. Playground
 *  leads because it is what an empty machine should land on: the sidebar entry
 *  is unconditional (HF-8, D265), so a machine with no cache still has a door,
 *  and the door opens on "pick a model and try it" rather than on an empty
 *  inventory listing. */
export const AI_MODELS_TABS: readonly AiModelsTab[] = [
  "playground",
  "local",
  "discover",
  "engines",
  "usage",
] as const;

export const DEFAULT_TAB: AiModelsTab = AI_MODELS_TABS[0];

/** Is this pathname anywhere on the AI Models page? Exported so App.tsx's route
 *  dispatch and this module cannot drift into two spellings of one prefix — the
 *  same reason `isPanelPath` exists in the platform router. */
export function isAiModelsPath(pathname: string): boolean {
  return pathname === AI_MODELS_PREFIX || pathname.startsWith(AI_MODELS_PREFIX + "/");
}

/** The tab a pathname names. An unrecognised sub-path falls back to the default
 *  SILENTLY, the same forgiving posture the shell takes for an unknown `_mode`
 *  (PT-9) and the `?tab=` codec took before it: a stale link should open the
 *  page, not an error. Deeper paths (`/ai-models/local/anything`) fall back
 *  too — this page has no second level. */
export function tabFromPath(pathname: string): AiModelsTab {
  if (!isAiModelsPath(pathname)) return DEFAULT_TAB;
  const rest = pathname.slice(AI_MODELS_PREFIX.length).replace(/^\/+/, "");
  return (AI_MODELS_TABS as readonly string[]).includes(rest)
    ? (rest as AiModelsTab)
    : DEFAULT_TAB;
}

/** The URL for a tab, KEEPING the current query string.
 *
 *  Keeping it is the point and it is not obvious: `?model=` is the
 *  playground's selection, and a user who switches to Local to unload
 *  something and comes back should find the same model selected. The tab
 *  strip is a view switch, not a navigation away from what was set up. The
 *  three-argument shape exists so tests can drive it without a `location`.
 */
export function tabHref(tab: AiModelsTab, search: string = location.search): string {
  return AI_MODELS_PREFIX + "/" + tab + (search && search !== "?" ? search : "");
}
