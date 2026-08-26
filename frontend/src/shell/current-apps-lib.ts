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
      app = { slug: dir.slice(root.length), dir, taskKeys: [], lastActive: 0, running: false };
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
// `/apps/<slug>` — ONE level under the hub, the slug being the folder name
// under <fused_dir>/local. Exactly one level: the retired `/apps/<tag>/<name>`
// builder route (D262) was two, and shell.py deliberately 404s that shape so a
// stale deep link fails loudly; the page's tab therefore rides the QUERY
// (`?tab=tasks`) rather than a second path segment. tasks-lib.viewUrl keeps
// foreign params, so the Tasks tab's own `?view=` toggle leaves `tab` alone.

export const APP_PAGE_PREFIX = "/apps/";
export const APP_PAGE_TAB_PARAM = "tab";
export type AppPageTab = "overview" | "tasks";

/** The page URL for an app slug. */
export function appPageUrl(slug: string, tab: AppPageTab = "overview"): string {
  const q = tab === "tasks" ? `?${APP_PAGE_TAB_PARAM}=tasks` : "";
  return APP_PAGE_PREFIX + encodeURIComponent(slug) + q;
}

/** The slug an app-page pathname names, or null for anything that is not one.
 *  Validated AFTER decoding: the slug becomes a folder name under the
 *  workspace, and the server route is a bare shell fallback, so this is the
 *  only guard against `..`, a separator, or an empty segment. A malformed
 *  percent-escape is likewise "not this page". */
export function slugFromAppPath(pathname: string): string | null {
  if (!pathname.startsWith(APP_PAGE_PREFIX)) return null;
  const raw = pathname.slice(APP_PAGE_PREFIX.length);
  if (!raw || raw.includes("/")) return null;
  let slug: string;
  try {
    slug = decodeURIComponent(raw);
  } catch {
    return null;
  }
  if (!slug || slug === "." || slug === ".." || /[/\\]/.test(slug)) return null;
  return slug;
}

/** Which tab a search string asks for; anything but `tasks` is the overview. */
export function appPageTab(search: string): AppPageTab {
  const raw = search.startsWith("?") ? search.slice(1) : search;
  return new URLSearchParams(raw).get(APP_PAGE_TAB_PARAM) === "tasks" ? "tasks" : "overview";
}

/** `search` with the tab written in — `overview` is the absence of the param,
 *  so the bare `/apps/<slug>` stays the page's address. Other params survive. */
export function withAppPageTab(search: string, tab: AppPageTab): string {
  const raw = search.startsWith("?") ? search.slice(1) : search;
  const q = new URLSearchParams(raw);
  if (tab === "overview") q.delete(APP_PAGE_TAB_PARAM);
  else q.set(APP_PAGE_TAB_PARAM, tab);
  const rest = q.toString();
  return rest ? `?${rest}` : "";
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
// The store is a plain Map held at MODULE level by the section — not state, not
// localStorage: the owner asked for "in memory only", so a reload starts over
// from recency by construction and there is nothing to migrate or expire. It
// survives navigation because the shell routes by pushState (the sidebar
// remounts, the module does not).
//
// An app that LEAVES the list (every task archived) loses its sequence, so if it
// returns it returns as new, at the top. "Already exists in the list" is the
// owner's own test for what must not move, and an app with nothing on the desk
// is not in the list. The prune is guarded on a non-empty input so a pulse that
// has not loaded yet cannot wipe the order.

/** slug -> sequence. Higher sorts earlier. */
export type AppOrder = Map<string, number>;

/** Give every app in `apps` a sequence, and forget apps that are gone.
 *  `apps` must arrive in RECENCY order (what `currentApps` returns): fresh
 *  slugs are numbered from the oldest up, so the newest ends with the highest
 *  sequence and lands at the top. Idempotent — an app that already has a
 *  sequence keeps it, which is what makes this safe to call during a render. */
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
