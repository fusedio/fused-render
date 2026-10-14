// GIT WORKTREES FOR A FOLDER CHAT (SPEC W-1/W-4). Pure TS, no React.
//
// fused-render owns the worktree, not the CLI's `--worktree` flag: the server
// runs `git worktree add` under `<repo>/.claude/worktrees/<name>`, and the chat
// then simply OPENS that folder as its target — so the session store, the Tasks
// rows, the queue keys and resume-by-`_file` all work as they do for any
// folder. Transport: POST /api/claude/worktrees `{action, file, ...}`, the same
// headers and error envelope as the agent route (`agent.ts`'s `post`).
import { post, type RunOpts } from "./agent";

/** One `git worktree list --porcelain` entry, as the server reports it. */
export interface WorktreeRow {
  path: string;
  branch: string;
  head: string;
  /** Basename for a managed worktree, else the path's last segment. */
  name: string;
  /** Under `<root>/.claude/worktrees/` — the only ones we may remove. */
  managed: boolean;
  is_main: boolean;
  /** `null` when `git status` ran out of time. */
  dirty: boolean | null;
  /** THE CHAT'S TARGET MAPPED INTO THIS WORKTREE — the same file or subfolder
   *  under this checkout, and whether it is on disk here (a file added on one
   *  branch only is not). The current row maps to the target itself. Absent
   *  on an older server, which knew folder targets only. */
  mapped?: { path: string; exists: boolean };
}

export interface WorktreeList {
  /** The main repository root. */
  root: string;
  /** The worktree the chat's file is in (or the root). */
  current: string;
  branch_current: string;
  /** The target's path under `current` — "" for a folder-root target. Absent
   *  on an older server. */
  relative?: string;
  worktrees: WorktreeRow[];
}

export type WorktreeError = { error: string };

const URL = "/api/claude/worktrees";

export function listWorktrees(file: string, opts: RunOpts = {}): Promise<WorktreeList | WorktreeError> {
  return post<WorktreeList | WorktreeError>(URL, { action: "list", file }, opts);
}

export function createWorktree(
  file: string,
  name: string,
  base?: string,
  opts: RunOpts = {},
): Promise<{ path: string; branch: string } | WorktreeError> {
  return post(URL, { action: "create", file, name, ...(base ? { base } : {}) }, opts);
}

export function removeWorktree(
  file: string,
  name: string,
  force?: boolean,
  opts: RunOpts = {},
): Promise<{ removed: string } | WorktreeError> {
  return post(URL, { action: "remove", file, name, ...(force ? { force: true } : {}) }, opts);
}

export function isWorktreeError(v: unknown): v is WorktreeError {
  return !!v && typeof v === "object" && typeof (v as WorktreeError).error === "string" && !!(v as WorktreeError).error;
}

// ---- the menu's model ------------------------------------------------------

/** The server's own name rule (`worktrees.py`): 1-64 of `[A-Za-z0-9._-]`, and
 *  never `.` or `..`. Checked here too so a bad name is refused before a trip. */
export function validWorktreeName(name: string): boolean {
  return /^[A-Za-z0-9._-]{1,64}$/.test(name) && name !== "." && name !== "..";
}

const pad = (n: number) => String(n).padStart(2, "0");

/** The "New worktree" field's default: `claude-<yymmdd>-<hhmm>`, local time. */
export function defaultWorktreeName(at: Date = new Date()): string {
  return (
    "claude-" +
    pad(at.getFullYear() % 100) +
    pad(at.getMonth() + 1) +
    pad(at.getDate()) +
    "-" +
    pad(at.getHours()) +
    pad(at.getMinutes())
  );
}

const trim = (p: string) => (p || "").replace(/[\\/]+$/, "");

/** The worktree row `list.current` names, if any. */
export function currentWorktree(list: WorktreeList): WorktreeRow | null {
  const cur = trim(list.current || list.root);
  return list.worktrees.find((w) => trim(w.path) === cur) ?? null;
}

/** What the trigger says: the current worktree's name — or, on the MAIN
 *  checkout, its branch (a repo folder's name says nothing about where you are). */
export function triggerLabel(list: WorktreeList): string {
  const cur = currentWorktree(list);
  if (!cur) return list.branch_current || "";
  if (cur.is_main) return cur.branch || list.branch_current || cur.name;
  return cur.name;
}

/** WHERE A SWITCH TO THIS WORKTREE LANDS: the chat's own file (or subfolder)
 *  in that checkout when it exists there, else the checkout's root. */
export function switchTarget(row: Pick<WorktreeRow, "path" | "mapped">): string {
  return row.mapped?.exists ? row.mapped.path : row.path;
}

export interface WorktreeMenuRow {
  path: string;
  /** Where a switch lands (`switchTarget`). */
  target: string;
  /** The chat is on a file/subfolder that is NOT on this worktree's branch, so
   *  a switch opens the worktree's folder instead — the row says so. */
  missing: boolean;
  name: string;
  branch: string;
  /** Known dirty — draws the dot. */
  dirty: boolean;
  /** Dirty OR unknown (`git status` timed out): removing it needs the force,
   *  and the confirm says it may lose changes. */
  maybeDirty: boolean;
  current: boolean;
  /** "Remove this worktree" is offered: managed and not the main checkout. */
  removable: boolean;
}

/** One row per worktree, main checkout first, then by name. */
export function worktreeRows(list: WorktreeList): WorktreeMenuRow[] {
  const cur = trim(list.current || list.root);
  const sub = !!list.relative;
  const rows = list.worktrees.map((w) => ({
    path: w.path,
    target: switchTarget(w),
    missing: sub && !!w.mapped && !w.mapped.exists,
    name: w.is_main ? w.name || w.branch : w.name,
    branch: w.branch,
    dirty: w.dirty === true,
    maybeDirty: w.dirty !== false,
    current: trim(w.path) === cur,
    removable: w.managed && !w.is_main,
    main: w.is_main,
  }));
  rows.sort((a, b) => (a.main === b.main ? a.name.localeCompare(b.name) : a.main ? -1 : 1));
  return rows.map(({ main: _main, ...row }) => row);
}

/** `/worktree [name]`: no name opens the menu; a valid name creates-or-switches;
 *  anything else is refused with the reason. */
export type WorktreeCommand = { open: true } | { name: string } | { error: string };

export function parseWorktreeCommand(arg: string): WorktreeCommand {
  const name = (arg || "").trim();
  if (!name) return { open: true };
  if (!validWorktreeName(name)) {
    return { error: "A worktree name is 1-64 letters, digits, dots, dashes or underscores" };
  }
  return { name };
}

/**
 * CREATE-OR-SWITCH: the path of the worktree called `name` — an existing one
 * when the list has it, else a fresh one. `{error}` on any refusal.
 */
export async function resolveWorktree(
  file: string,
  name: string,
  deps: { list?: typeof listWorktrees; create?: typeof createWorktree } = {},
): Promise<{ path: string; current?: boolean } | WorktreeError> {
  const list = await (deps.list ?? listWorktrees)(file);
  if (isWorktreeError(list)) return list;
  // A MANAGED worktree of that name first: the repo folder's own name (the
  // main checkout) or a hand-made worktree must not shadow `.claude/worktrees/<name>`.
  const hit =
    list.worktrees.find((w) => w.managed && w.name === name) ??
    list.worktrees.find((w) => w.name === name);
  // THE ONE ALREADY OPEN is not a switch: navigating would only start a fresh
  // chat in the same folder, so the caller opens the menu instead.
  if (hit) {
    return trim(hit.path) === trim(list.current || list.root)
      ? { path: switchTarget(hit), current: true }
      : { path: switchTarget(hit) };
  }
  return createAndTarget(file, name, deps);
}

/**
 * CREATE, THEN LAND ON THE CHAT'S OWN TARGET in the new checkout: the list is
 * read again so the new row's `mapped` says where the same file is (a new
 * worktree is a full checkout, so it usually is). A failed re-read falls back
 * to the new worktree's root.
 */
export async function createAndTarget(
  file: string,
  name: string,
  deps: { list?: typeof listWorktrees; create?: typeof createWorktree } = {},
): Promise<{ path: string } | WorktreeError> {
  const made = await (deps.create ?? createWorktree)(file, name);
  if (isWorktreeError(made)) return made;
  try {
    const list = await (deps.list ?? listWorktrees)(file);
    if (!isWorktreeError(list)) {
      const row = list.worktrees.find((w) => trim(w.path) === trim(made.path));
      if (row) return { path: switchTarget(row) };
    }
  } catch {
    /* the root is still a right answer */
  }
  return { path: made.path };
}

/** The server refused a remove because the worktree has (or may have) changes
 *  (`worktrees.py::remove_worktree`): the next press may force it. */
export function isDirtyRefusal(error: string): boolean {
  return /uncommitted changes|could not tell whether this worktree has changes/i.test(error || "");
}

export type DirtyState = "clean" | "dirty" | "unknown";

/** A row's dirt as the remove confirm sees it. */
export function dirtyState(row: Pick<WorktreeMenuRow, "dirty" | "maybeDirty">): DirtyState {
  return row.dirty ? "dirty" : row.maybeDirty ? "unknown" : "clean";
}

/** The remove button's words at each step. Anything but "clean" means the
 *  confirming press FORCES the remove. */
export function removeLabel(confirming: boolean, state: DirtyState): string {
  if (!confirming) return "Remove this worktree";
  if (state === "dirty") return "Has uncommitted changes. Remove anyway";
  if (state === "unknown") return "May have uncommitted changes. Remove anyway";
  return "Confirm remove";
}
