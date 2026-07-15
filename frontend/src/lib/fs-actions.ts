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

// Parent directory of an absolute path, in the shell's canonical forward-slash
// form. Root's parent is root.
export function dirname(p: string): string {
  const norm = p.replace(/\/+$/, "");
  const i = norm.lastIndexOf("/");
  return i <= 0 ? "/" : norm.slice(0, i);
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
  const { entries } = await listDir(parentDir);
  const taken = new Set(entries.map((e) => e.name));
  let i = 1;
  let candidate = duplicateName(name, i, isDir);
  while (taken.has(candidate)) candidate = duplicateName(name, ++i, isDir);
  return parentDir + "/" + candidate;
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
