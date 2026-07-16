// Shared file-action layer for the two views that host the Finder-style file
// context menu (views/Listing.tsx and views/Preview.tsx). Pure helpers plus
// the async flow pieces (duplicate-name resolution, trash-with-fallback,
// Open-With mode resolution, clipboard writes) live here so neither view has
// to copy-paste the bodies. Deliberately UI-free in the React sense — it builds
// plain MenuItem objects and returns data, but owns no component state; each
// view keeps its own menu/dialog/toast state and its own post-action behaviour
// (Listing re-anchors its selection + refetches; Preview navigates).
import { listDir, deleteEntry, statPath, resolveConditions } from "./api";
import type { TemplateEntry } from "./api";
import { getClipboard, setClipboard } from "./fs-clipboard";
import type { MenuItem } from "../components/ContextMenu";
import { KNOWN_SENTINEL_MODES, modeTitle, templateModeIcon } from "../components/ModeSwitcher";

// Windows fs paths are rooted at a drive letter ("C:/…"), not at "/" — mirrors
// router.ts's rootedFsPath / Breadcrumb's drive detection. The canonical drive
// root is "C:/" (colon + forward slash); a bare "C:" is cwd-relative to
// os.stat, so it must never be handed to the API as a directory path.
const DRIVE_RE = /^[A-Za-z]:/;

// Parent directory of an absolute path, in the shell's canonical forward-slash
// form. Root's parent is root — and on Windows, a drive root's ("C:/") parent
// is itself, same as POSIX "/".
export function dirname(p: string): string {
  const norm = p.replace(/\/+$/, "");
  const drive = DRIVE_RE.test(norm) ? norm.slice(0, 2) : null; // e.g. "C:"
  if (drive && norm.length === drive.length) return drive + "/"; // "C:" -> "C:/"
  const i = norm.lastIndexOf("/");
  if (drive) return i === drive.length ? drive + "/" : norm.slice(0, i); // "C:/item" -> "C:/"
  return i <= 0 ? "/" : norm.slice(0, i);
}

// Canonical directory form. A listing's `base` is `fsPath` with the trailing
// "/" stripped, so at the filesystem root it collapses to "" — the API rejects
// "" as a directory path and only accidentally survives string joins. Treat ""
// as "/" wherever a parent/target dir is derived. Same problem on Windows: a
// bare drive letter ("C:") strips to a cwd-relative path, not the drive root —
// canonicalize it to "C:/".
export function normDir(dir: string): string {
  if (dir === "") return "/";
  if (/^[A-Za-z]:$/.test(dir)) return dir + "/";
  return dir;
}

// Join a directory and a child name into a path, root-safe: at the filesystem
// root the dir is "/", and on Windows a drive root is "C:/" — in both cases a
// plain `dir + "/" + name` would yield a double slash. Everywhere else it's
// the ordinary concat.
export function join(dir: string, name: string): string {
  return dir.endsWith("/") ? dir + name : dir + "/" + name;
}

// Finder-style duplicate name: "report.csv" -> "report copy.csv" ->
// "report copy 2.csv". Directories (and extension-less / dotfile names) keep
// the whole name and just gain the " copy" suffix.
export function duplicateName(name: string, counter: number, isDir: boolean): string {
  const suffix = counter <= 1 ? " copy" : ` copy ${counter}`;
  const dot = name.lastIndexOf(".");
  if (!isDir && dot > 0) return name.slice(0, dot) + suffix + name.slice(dot);
  return name + suffix;
}

// First free "… copy[/ n]" destination path for a Duplicate of `name` into
// `parentDir`, chosen by listing the folder first so the copy never 409s on an
// existing name.
export async function freeDuplicatePath(
  parentDir: string,
  name: string,
  isDir: boolean
): Promise<string> {
  const dir = normDir(parentDir); // "" (root) would be rejected by listDir
  const { entries } = await listDir(dir);
  const taken = new Set(entries.map((e) => e.name));
  let i = 1;
  let candidate = duplicateName(name, i, isDir);
  while (taken.has(candidate)) candidate = duplicateName(name, ++i, isDir);
  return join(dir, candidate);
}

// Destination path for pasting `name` into `parentDir`: keeps the original
// name when free, otherwise falls back to the first free "… copy[/ n]" name
// (same dedupe as Duplicate) so a paste never 409s on an existing entry.
export async function freePastePath(parentDir: string, name: string, isDir: boolean): Promise<string> {
  const dir = normDir(parentDir);
  const { entries } = await listDir(dir);
  const taken = new Set(entries.map((e) => e.name));
  if (!taken.has(name)) return join(dir, name);
  let i = 1;
  let candidate = duplicateName(name, i, isDir);
  while (taken.has(candidate)) candidate = duplicateName(name, ++i, isDir);
  return join(dir, candidate);
}

// Write text to the system clipboard; resolves true on success, false when the
// Clipboard API is missing or the write is denied. Callers decide whether to
// toast (a failure stays silent — the path is still reachable via Reveal).
export async function copyToClipboard(text: string): Promise<boolean> {
  if (!navigator.clipboard) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

// After a successful delete/trash, drop the module clipboard if it points at
// the removed entry — either the exact path, or something inside it when a
// directory was deleted (prefix + separator). Otherwise a later Paste of that
// cut/copy would target a source that no longer exists.
export function clearClipboardIfDeleted(deleted: string): void {
  const clip = getClipboard();
  if (clip && (clip.path === deleted || clip.path.startsWith(deleted + "/"))) {
    setClipboard(null);
  }
}

// After a successful rename/move, repoint the module clipboard if it was
// referencing the moved entry — either the exact path (clip IS the renamed
// entry) or something inside it when a directory was renamed (prefix +
// separator, e.g. a file cut from inside a folder that then got renamed).
// Keeps the op (cut/copy) unchanged. Otherwise a later Paste would target a
// source path that no longer exists (or, worse, silently hit whatever now
// occupies the stale path). Mirrors clearClipboardIfDeleted above.
export function remapClipboardPath(oldPath: string, newPath: string): void {
  const clip = getClipboard();
  if (!clip) return;
  if (clip.path === oldPath) {
    setClipboard({ ...clip, path: newPath });
  } else if (clip.path.startsWith(oldPath + "/")) {
    setClipboard({ ...clip, path: newPath + clip.path.slice(oldPath.length) });
  }
}

// Outcome of a Move to Bin attempt. "unsupported" is the non-macOS 501 case
// where the caller should fall back to a hard-delete confirm; "error" is any
// other failure (surface it as a toast).
export type TrashOutcome =
  | { status: "trashed" }
  | { status: "unsupported" }
  | { status: "error"; message: string };

// Move to Bin: a recoverable delete (macOS Trash). Where the server can't trash
// (non-macOS → 501 "trash unsupported") this reports "unsupported" so the
// caller can fall back to the irreversible confirm-then-hard-delete flow.
export async function trashEntry(path: string, isDir: boolean): Promise<TrashOutcome> {
  try {
    await deleteEntry(path, isDir, true);
    return { status: "trashed" };
  } catch (e) {
    const message = (e as Error).message;
    if (message.includes("trash unsupported")) return { status: "unsupported" };
    return { status: "error", message };
  }
}

// Resolve the Open-With mode list for a path: stat's templates, sentinel- and
// gate-filtered (mirrors Preview's dispatch). Conditional templates whose
// condition.py verdict denies them are dropped; a failed gate fails closed
// (drops all conditionals), matching the shell's posture everywhere else.
export async function resolveOpenWithModes(path: string): Promise<TemplateEntry[]> {
  const s = await statPath(path);
  let filtered = s.templates.filter((t) => t.path !== null || KNOWN_SENTINEL_MODES.has(t.mode));
  if (filtered.some((t) => t.conditional)) {
    try {
      const r = await resolveConditions(path);
      filtered = filtered.filter((t) => !t.conditional || r.conditions[t.mode] === true);
    } catch {
      filtered = filtered.filter((t) => !t.conditional); // fail closed, like a broken gate
    }
  }
  return filtered;
}

// Build the Open-With submenu rows from a resolved mode list. `onSelect` gets
// the chosen mode and whether it's the default (the first unconditional entry,
// else the first) — Listing/Preview use that to set or delete `_mode`. The
// template-mode glyph fills the reserved icon column, matching the pane menu.
export function buildOpenWithItems(
  modes: TemplateEntry[],
  onSelect: (mode: string, isDefault: boolean) => void
): MenuItem[] {
  if (modes.length === 0) return [{ label: "No views available", disabled: true }];
  const def = modes.find((t) => !t.conditional) || modes[0];
  return modes.map((t) => ({
    label: modeTitle(t.mode),
    icon: templateModeIcon(t),
    onClick: () => onSelect(t.mode, t.mode === def.mode),
  }));
}
