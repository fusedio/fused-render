// The sidebar's "Current apps" section (D487): which workspace apps have work
// in flight, derived from the task pulse rather than from a poll of their own.
//
// An app is CURRENT while any task whose project sits under ~/Fused/local/<slug>
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
  /** The folder name under Fused/local — the row's label. */
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

/** The workspace apps root for a home dir, with its trailing slash so
 *  `Fused/localother` can never prefix-match. Backslashes are folded: `home`
 *  is the server's raw `expanduser("~")` (backslashed on Windows) while task
 *  paths arrive canonical forward-slash, and a prefix test between the two
 *  spellings would silently empty the section. */
export function localAppsRoot(home: string): string {
  const base = home.replace(/\\/g, "/").replace(/\/+$/, "");
  return `${base}/Fused/local/`;
}

/** The app folder a project path belongs to, or null when it is not under the
 *  workspace root. `~/Fused/local/foo/sub` → `~/Fused/local/foo`; a project that
 *  IS the root (a task on the local folder itself) is not an app. */
export function appDirOf(project: string, root: string): string | null {
  if (!project.startsWith(root)) return null;
  const rest = project.slice(root.length);
  const slug = rest.split("/")[0];
  if (!slug) return null;
  return root + slug;
}

export function currentApps(
  tasks: TaskPulseTask[],
  home: string,
  limit: number = CURRENT_APPS_LIMIT,
): CurrentApp[] {
  if (!home) return [];
  const root = localAppsRoot(home);
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
  return [...byDir.values()]
    .sort((a, b) => b.lastActive - a.lastActive || a.slug.localeCompare(b.slug))
    .slice(0, limit);
}
