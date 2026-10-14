// The /bots URL space: every page inside the Bots sub-app is a path, and this module is the ONE place that spells
// them (the ai_models/routes.ts posture — the shell's dispatcher, the sidebar, the Dock and the route test all ask
// here instead of carrying a second copy).
//
//   /bots                      the bare door: the page redirects to the selected bot once the list knows one
//                              (store.ts writeUrl, a replaceState; `?new=1` / `?phone=1` one-shots ride along)
//   /bots/<id>                 one bot's chat (ids are uuid4().hex[:8], bots/bot.py create)
//   /bots/tasks[?task=<key>]   the Tasks page: the shell's Tasks UI framed and filtered to builds (builds/TasksPage)
//   /bots/apps                 the Apps gallery (apps/AppsPanel)
//   /bots/apps/<folder>        one app full-size (apps/AppViewer); `folder` is the basename under the apps root
//   /bots/preferences          Preferences framed (components/PrefsPanel; the Fused Bot flavor, which has no sidebar)
//
// Until 2026-10-14 these were in-page state: `?bot=<id>` written with replaceState and a `ui.panel` slot in the
// store for the three overlays. The selected bot and the open page are now the URL and nothing else (I5 in the
// design page): the store reads `sel` from the path, a bot click is a pushState, Back steps between bots.
//
// A reserved first segment (`tasks`, `apps`, `preferences`) can never be a bot id: ids are eight hex characters,
// and a bot imported from FusedBot (its directory's basename) is still refused these names by the parser. An
// unknown shape (`/bots/<id>/x`, `/bots/tasks/x`) falls back to the nearest page rather than erroring — the same
// forgiving posture AI Models takes for an unknown tab.
//
// LEGACY: `/bots?bot=<id>` is rewritten to `/bots/<id>` for good by the platform router (router.ts
// rewriteLegacyUrl, at boot and inside navigateUrl — the platform may not import this module, so the rule lives
// there). The old shape sat in the native Dock, mac_window.show_bot and bot-written links, so a running older Fused
// Bot build and old bookmarks keep opening the right chat.

export const BOTS_PREFIX = "/bots";

/** First segments that are pages, not bot ids. */
export const RESERVED = ["tasks", "apps", "preferences"] as const;

export type BotsRoute =
  | { kind: "list" }
  | { kind: "bot"; id: string }
  | { kind: "tasks"; task: string | null }
  | { kind: "apps"; folder: string | null }
  | { kind: "prefs" };

/** Anywhere on the Bots page: the bare prefix or anything beneath it. */
export function isBotsPath(pathname: string): boolean {
  return pathname === BOTS_PREFIX || pathname.startsWith(BOTS_PREFIX + "/");
}

const ID_RE = /^[A-Za-z0-9_-]+$/;
const isReserved = (s: string): boolean => (RESERVED as readonly string[]).includes(s);
/** A bot id as the path may carry it: the id alphabet, and never a page name. */
export const isBotId = (s: string): boolean => ID_RE.test(s) && !isReserved(s);

const decode = (s: string): string => { try { return decodeURIComponent(s); } catch { return s; } };

/** The page a pathname (and its query) names. Never throws; an unrecognised shape lands on the nearest page. */
export function parseBotsPath(pathname: string, search: string = ""): BotsRoute {
  if (!isBotsPath(pathname)) return { kind: "list" };
  const segs = pathname.slice(BOTS_PREFIX.length).split("/").filter(Boolean);
  if (!segs.length) return { kind: "list" };
  const [head, second] = segs;
  if (head === "tasks") {
    let task: string | null = null;
    try { task = new URLSearchParams(search).get("task") || null; } catch { /* unreadable */ }
    return { kind: "tasks", task };
  }
  if (head === "apps") return { kind: "apps", folder: second ? decode(second) : null };
  if (head === "preferences") return { kind: "prefs" };
  if (isBotId(head)) return { kind: "bot", id: decode(head) };
  return { kind: "list" };
}

// Every href takes the current query (`search`) and KEEPS it: a side app's fused.params and the shell's `peek` live
// on the host URL whichever page is on top, and a hop that dropped them would blank "Copy state" and the app's next
// param sync. tasksHref owns the `task` key (set or dropped); nothing else here touches a key.
const withSearch = (path: string, search: string): string => path + (search && search !== "?" ? (search.startsWith("?") ? search : "?" + search) : "");
export const listHref = (search = ""): string => withSearch(BOTS_PREFIX, search);
export const botHref = (id: string, search = ""): string => withSearch(`${BOTS_PREFIX}/${encodeURIComponent(id)}`, search);
export const tasksHref = (task?: string | null, search = ""): string => {
  const q = new URLSearchParams(search);
  q.delete("task");
  if (task) q.set("task", task);
  return withSearch(`${BOTS_PREFIX}/tasks`, q.toString());
};
export const appsHref = (folder?: string | null, search = ""): string => withSearch(`${BOTS_PREFIX}/apps${folder ? "/" + encodeURIComponent(folder) : ""}`, search);
export const prefsHref = (search = ""): string => withSearch(`${BOTS_PREFIX}/preferences`, search);
/** The query minus the Tasks page's own `task` key: what a hop OFF that page carries. */
export function searchSansTask(search: string): string {
  const q = new URLSearchParams(search);
  q.delete("task");
  const s = q.toString();
  return s ? "?" + s : "";
}
