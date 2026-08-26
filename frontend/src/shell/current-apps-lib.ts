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
// Ordering is by the newest task activity per app, capped at five — the sidebar
// is a shortlist, and the Tasks page is where the long tail lives. The cap and
// the order are this module's assumptions, not the owner's words; recorded so a
// later change knows it is free to move them.
import type { TaskPulseTask } from "@platform/lib/api";

export const CURRENT_APPS_LIMIT = 5;

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

export function currentApps(
  tasks: TaskPulseTask[],
  fusedDir: string,
  limit: number = CURRENT_APPS_LIMIT,
): CurrentApp[] {
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
  return [...byDir.values()]
    .sort((a, b) => b.lastActive - a.lastActive || a.slug.localeCompare(b.slug))
    .slice(0, limit);
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
// query is left to the tab's own params (`?view=` on Tasks) and is carried
// across a tab switch untouched.

export const APP_PAGE_PREFIX = "/apps/";
/** Tab-strip order; the first is the default. THE list: the type below is
 *  derived from it, and AppPage.tsx's `TAB_DEFS` is a `Record` over that type,
 *  so adding a tab is one string here plus one entry there — the compiler
 *  refuses the second being forgotten. */
export const APP_PAGE_TABS = ["overview", "tasks"] as const;
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
