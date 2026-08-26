// The sidebar's "Current apps" section (D487): which workspace apps have work
// in flight, derived from the task pulse rather than from a poll of their own.
// Also the codec for the app page those rows open (D488): `/apps/<slug>`.
//
// An app is CURRENT while any task whose project sits under <fused_dir>/local/<slug>
// is not archived — upcoming, in progress, done-but-unfiled and failed all
// count, because the row's job is "this app is still on your desk", and only
// filing a task (archive) says it is not. Constrained to the workspace on the
// owner's instruction: an app elsewhere on disk is a project, not one of the
// apps this shell made.
//
// EVERY such app is rendered — the list is not capped. It was capped at five
// (the owner's own "render 5 max"), and the owner lifted that on 2026-08-26:
// the section is the answer to "which apps are on my desk", and a shortlist
// that silently hides the sixth answers it wrong.
//
// `currentApps` returns them in RECENCY order (newest task activity first,
// slug as the tiebreak). That is the SEED for the displayed order, not the
// displayed order itself — see the sequence layer at the bottom of this file.
import type { TaskPulseTask } from "@platform/lib/api";

export interface CurrentApp {
  /** The folder name under <fused_dir>/local — the row's label. */
  slug: string;
  /** Absolute app folder, forward-slash (server paths are canonical). */
  dir: string;
  /** Every non-archived task key under this app — what the cross archives. */
  taskKeys: string[];
  /** Newest `last_active` across those tasks — the sort key. */
  lastActive: number;
  /** Something is running right now — the row wears the running dot. */
  running: boolean;
}

/** The workspace apps root, with its trailing slash so `local-other` can never
 *  prefix-match. `fusedDir` is the server's `Config.fused_dir` (~/Fused unless
 *  FUSED_RENDER_DIR overrides it — which is why this is not built from `home`).
 *  Backslashes are folded: the config value is raw `os.path` (backslashed on
 *  Windows) while task paths arrive canonical forward-slash, and a prefix test
 *  between the two spellings would silently empty the section. */
export function localAppsRoot(fusedDir: string): string {
  const base = fusedDir.replace(/\\/g, "/").replace(/\/+$/, "");
  return `${base}/local/`;
}

/** The app folder a project path belongs to, or null when it is not under the
 *  workspace root. `…/local/foo/sub` → `…/local/foo`; a project that IS the
 *  root (a task on the local folder itself) is not an app. */
export function appDirOf(project: string, root: string): string | null {
  if (!project.startsWith(root)) return null;
  const rest = project.slice(root.length);
  const slug = rest.split("/")[0];
  if (!slug) return null;
  return root + slug;
}

/** Is `project` this app's folder or somewhere inside it — the scope test the
 *  app page's Tasks tab applies (a task on a subfolder still belongs to the
 *  app). Exact prefix on the folder boundary, so `foo` never claims `foo2`. */
export function isUnderDir(project: string, dir: string): boolean {
  return project === dir || project.startsWith(dir + "/");
}

export function currentApps(tasks: TaskPulseTask[], fusedDir: string): CurrentApp[] {
  if (!fusedDir) return [];
  const root = localAppsRoot(fusedDir);
  const byDir = new Map<string, CurrentApp>();
  for (const t of tasks) {
    if (t.status === "archived") continue;
    const dir = appDirOf(t.project || "", root);
    if (!dir) continue;
    let app = byDir.get(dir);
    if (!app) {
      app = {
        slug: dir.slice(root.length),
        dir,
        taskKeys: [],
        lastActive: 0,
        running: false,
      };
      byDir.set(dir, app);
    }
    app.taskKeys.push(t.key);
    app.lastActive = Math.max(app.lastActive, t.last_active || 0);
    if (t.status === "in_progress") app.running = true;
  }
  return [...byDir.values()].sort(
    (a, b) => b.lastActive - a.lastActive || a.slug.localeCompare(b.slug),
  );
}

// ---- the app page's address (D488) ------------------------------------------
//
// `/apps/<slug>/<tab>` — the slug being the folder name under <fused_dir>/local,
// the tab one of APP_PAGE_TABS, one path per tab the way /ai-models does it
// (D420). Bare `/apps/<slug>` redirects to the default tab (App.tsx, the same
// render-time replaceState as `/ai-models`). The tab first rode the query
// (`?tab=tasks`) so this page could never look like the retired two-level
// `/apps/<tag>/<name>` builder route (D262); the owner chose the path anyway
// (2026-08-26), so shell.py now serves two levels under /apps and a stale
// builder link lands here on the default tab like any unknown sub-path. The
// query is left to the tab's own params (`?view=` on Tasks, `?file=`/`?_mode=`
// on Files) and is carried across a tab switch untouched.

export const APP_PAGE_PREFIX = "/apps/";
/** Tab-strip order; the first is the default. THE list: the type below is
 *  derived from it, and AppPage.tsx's `TAB_DEFS` is a `Record` over that type,
 *  so adding a tab is one string here plus one entry there — the compiler
 *  refuses the second being forgotten. */
export const APP_PAGE_TABS = ["overview", "tasks", "files"] as const;
export type AppPageTab = (typeof APP_PAGE_TABS)[number];
export const DEFAULT_APP_PAGE_TAB: AppPageTab = APP_PAGE_TABS[0];

/** The page URL for an app slug and tab. */
export function appPageUrl(
  slug: string,
  tab: AppPageTab = DEFAULT_APP_PAGE_TAB,
): string {
  return APP_PAGE_PREFIX + encodeURIComponent(slug) + "/" + tab;
}

// The pathname split into its (raw) slug segment and whatever follows it.
function splitAppPath(
  pathname: string,
): { raw: string; rest: string | null } | null {
  if (!pathname.startsWith(APP_PAGE_PREFIX)) return null;
  const tail = pathname.slice(APP_PAGE_PREFIX.length);
  const cut = tail.indexOf("/");
  return cut < 0
    ? { raw: tail, rest: null }
    : { raw: tail.slice(0, cut), rest: tail.slice(cut + 1) };
}

/** The slug an app-page pathname names, or null for anything that is not one.
 *  Validated AFTER decoding: the slug becomes a folder name under the
 *  workspace, and the server route is a bare shell fallback, so this is the
 *  only guard against `..`, a separator, or an empty segment. A malformed
 *  percent-escape is likewise "not this page". Anything deeper than
 *  `/apps/<slug>/<tab>` is not this page either. */
export function slugFromAppPath(pathname: string): string | null {
  const parts = splitAppPath(pathname);
  if (!parts || !parts.raw) return null;
  if (parts.rest !== null && parts.rest.includes("/")) return null;
  let slug: string;
  try {
    slug = decodeURIComponent(parts.raw);
  } catch {
    return null;
  }
  if (!slug || slug === "." || slug === ".." || /[/\\]/.test(slug)) return null;
  return slug;
}

/** The tab a pathname names. Missing or unknown falls back to the default
 *  SILENTLY (the /ai-models posture): a stale link opens the page, not an
 *  error. App.tsx rewrites the bare-slug case so the default has an address. */
export function appPageTabFromPath(pathname: string): AppPageTab {
  const rest = splitAppPath(pathname)?.rest ?? null;
  return rest !== null && (APP_PAGE_TABS as readonly string[]).includes(rest)
    ? (rest as AppPageTab)
    : DEFAULT_APP_PAGE_TAB;
}

/** Is this pathname the bare `/apps/<slug>` (no tab segment) that App.tsx
 *  rewrites to the default tab? */
export function isBareAppPath(pathname: string): boolean {
  const parts = splitAppPath(pathname);
  return (
    parts !== null && parts.rest === null && slugFromAppPath(pathname) !== null
  );
}

// ---- the displayed order: a sequence per app --------------------------------
//
// Rows render by DESCENDING sequence number, and a sequence changes for exactly
// two reasons: an app that is not in the list gets one, and the user drags a
// row. Recency seeds the sequences and then stops mattering — a new task in an
// app already listed must NOT move its row, because this list is something the
// user reads and points at, and a list that reshuffles itself under the cursor
// every time an agent writes a line cannot be pointed at.
//
// The store is a plain Map held at MODULE level by the section, hydrated from
// and written back to localStorage (`ORDER_KEY` there) — so the order the user
// dragged survives a reload and the next launch, per machine. It was in-memory
// only for one commit; the owner asked for it on disk and picked localStorage
// over a server-side pref, which buys most of the value for a fraction of the
// work: a synchronous read means no load race against the pulse, and there is
// no endpoint, no file format and no multi-window write conflict to arbitrate.
// The cost is that it is per browser profile and does not follow the user to
// another machine.
//
// An app that LEAVES the list (every task archived) is FORGOTTEN — sequence and
// all — so if it comes back it comes back as new, at the top. Nothing removed is
// remembered; that is the owner's rule, restated by hand after a middle version
// tried remembering (2026-08-26). It also bounds the store by construction: what
// is held, and what is saved, is the apps on the desk and nothing else.
//
// The prune is guarded on a non-empty input so a pulse that has not loaded yet
// cannot wipe the order.
//
// Pruning makes assignment NON-MONOTONE, which is dangerous for state two tabs
// share — pruning per-tab against a per-tab live set is a write loop waiting to
// happen (Bugbot, 2026-08-26, on exactly that). What makes it safe here is that
// the tabs do not race to describe the world: only a DRAG writes to the store
// (see CurrentAppsSection), and a drag is one user gesture, not a poll. A pulse
// changes the order on screen and saves nothing, so there is no second writer to
// disagree with.

/** slug -> sequence. Higher sorts earlier. */
export type AppOrder = Map<string, number>;

/** Give every app in `apps` a sequence, and forget every app that is gone.
 *  `apps` must arrive in RECENCY order (what `currentApps` returns): fresh slugs
 *  are numbered from the oldest up, so the newest ends with the highest sequence
 *  and lands at the top. Idempotent — an app that already has a sequence keeps
 *  it, which is what makes this safe to call during a render.
 *
 *  The prune is why the store never needs a size limit: it holds the desk, and
 *  the desk is folders a person made by hand. */
export function assignSequences(order: AppOrder, apps: CurrentApp[]): void {
  if (!apps.length) return;
  const live = new Set(apps.map((a) => a.slug));
  for (const slug of [...order.keys()]) if (!live.has(slug)) order.delete(slug);
  const fresh = apps.filter((a) => !order.has(a.slug));
  if (!fresh.length) return;
  let next = 0;
  for (const seq of order.values()) next = Math.max(next, seq);
  for (let i = fresh.length - 1; i >= 0; i--) order.set(fresh[i].slug, ++next);
}

/** `apps` in display order. Sequences are assumed assigned; an app without one
 *  sorts last rather than throwing, so a caller that skipped `assignSequences`
 *  still gets a list. */
export function bySequence(apps: CurrentApp[], order: AppOrder): CurrentApp[] {
  return [...apps].sort((a, b) => (order.get(b.slug) ?? 0) - (order.get(a.slug) ?? 0));
}

/** Write `slugs` (display order, top first) into the store as the new order.
 *  Renumbers the whole visible list from `slugs.length` down to 1 in one go, so
 *  no drag can leave two rows sharing a sequence. */
export function reorderTo(order: AppOrder, slugs: string[]): void {
  let seq = slugs.length;
  for (const slug of slugs) order.set(slug, seq--);
}

/** The store as a display-ordered slug list — what a drag saves, and the input
 *  `reorderTo` takes back. Since the store is pruned to the apps on the desk,
 *  this is exactly the rows on screen: there is no remembered tail that could
 *  interleave itself into an order the user arranged by hand. */
export function orderedSlugs(order: AppOrder): string[] {
  return [...order.entries()].sort((a, b) => b[1] - a[1]).map(([slug]) => slug);
}

/** A saved order, read back. What came out of localStorage is a string written
 *  by SOMEONE ELSE (an older build, a hand-edited devtools row), so anything
 *  unreadable degrades to "no saved order" — the list seeds from recency, which
 *  is exactly where it started — rather than throwing inside a render. Non-slug
 *  entries are dropped and duplicates collapse to their first appearance: two
 *  rows sharing a sequence would make the display order depend on sort
 *  stability, and that is not a thing to leave to a corrupt row. */
export function parseSavedOrder(raw: string | null): string[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const slug of parsed) {
    if (typeof slug !== "string" || !slug || seen.has(slug)) continue;
    seen.add(slug);
    out.push(slug);
  }
  return out;
}

/** `slugs` with `from` lifted out and re-inserted at `to`'s slot — the list a
 *  drop produces. `below` puts it after the target rather than before. Returns
 *  the input untouched when the drag cannot move anything. */
export function moveSlug(slugs: string[], from: string, to: string, below: boolean): string[] {
  if (from === to) return slugs;
  const rest = slugs.filter((s) => s !== from);
  const at = rest.indexOf(to);
  if (at === -1) return slugs;
  rest.splice(at + (below ? 1 : 0), 0, from);
  return rest;
}
