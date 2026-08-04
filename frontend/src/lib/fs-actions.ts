// Shared file-action layer for the two views that host the Finder-style file
// context menu (views/Listing.tsx and views/Preview.tsx). Pure helpers plus
// the async flow pieces (duplicate-name resolution, trash-with-fallback,
// Open-With mode resolution, clipboard writes) live here so neither view has
// to copy-paste the bodies. Deliberately UI-free in the React sense — it builds
// plain MenuItem objects and returns data, but owns no component state; each
// view keeps its own menu/dialog/toast state and its own post-action behaviour
// (Listing re-anchors its selection + refetches; Preview navigates).
import { listDir, deleteEntry, statPath, resolveConditions } from "./api";
import type { ArchiveFormat, TemplateEntry } from "./api";
import { getClipboard, setClipboard } from "./fs-clipboard";
import { dropRecentsFor } from "./recents";
import type { MenuEntry, MenuItem } from "../components/ContextMenu";
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

// Whether a path exists, via a stat probe. Used only to disambiguate a
// candidate name when the folder listing was TRUNCATED (the 10k server cap), so
// a colliding name PAST the cap — invisible to the listing — doesn't slip
// through and 409. A stat that fails for any reason (404 or a network blip) is
// treated as "free": the worst case falls back to the server's own 409, which
// is exactly the pre-fix behaviour.
async function pathExists(p: string): Promise<boolean> {
  try {
    await statPath(p);
    return true;
  } catch {
    return false;
  }
}

// Advance `candidate` past any name that exists on disk, probing one stat at a
// time (bounded). Only invoked for a truncated listing, where the in-page
// `taken` set is incomplete. `i` continues the "… copy N" counter.
async function firstFreeProbed(
  dir: string,
  name: string,
  isDir: boolean,
  i: number,
  candidate: string
): Promise<string> {
  for (let tries = 0; tries < 100 && (await pathExists(join(dir, candidate))); tries++) {
    candidate = duplicateName(name, ++i, isDir);
  }
  return candidate;
}

// First free "… copy[/ n]" destination path for a Duplicate of `name` into
// `parentDir`, chosen by listing the folder first so the copy never 409s on an
// existing name. When the listing is truncated (huge folder past the server
// cap), the chosen candidate is verified with a stat probe so a collision past
// the cap can't leak through as a bare 409.
export async function freeDuplicatePath(
  parentDir: string,
  name: string,
  isDir: boolean
): Promise<string> {
  const dir = normDir(parentDir); // "" (root) would be rejected by listDir
  const { entries, truncated } = await listDir(dir);
  const taken = new Set(entries.map((e) => e.name));
  let i = 1;
  let candidate = duplicateName(name, i, isDir);
  while (taken.has(candidate)) candidate = duplicateName(name, ++i, isDir);
  if (truncated) candidate = await firstFreeProbed(dir, name, isDir, i, candidate);
  return join(dir, candidate);
}

// Destination path for pasting `name` into `parentDir`: keeps the original
// name when free, otherwise falls back to the first free "… copy[/ n]" name
// (same dedupe as Duplicate) so a paste never 409s on an existing entry. As
// with Duplicate, a truncated listing verifies the choice with a stat probe.
export async function freePastePath(parentDir: string, name: string, isDir: boolean): Promise<string> {
  const dir = normDir(parentDir);
  const { entries, truncated } = await listDir(dir);
  const taken = new Set(entries.map((e) => e.name));
  if (!taken.has(name) && !(truncated && (await pathExists(join(dir, name))))) {
    return join(dir, name);
  }
  let i = 1;
  let candidate = duplicateName(name, i, isDir);
  while (taken.has(candidate)) candidate = duplicateName(name, ++i, isDir);
  if (truncated) candidate = await firstFreeProbed(dir, name, isDir, i, candidate);
  return join(dir, candidate);
}

// claude-cli:// deep link for a listing entry — Claude Code registers this
// scheme OS-wide (see templates_api.api_open_in_claude, which builds the same
// cwd shape for a template folder, but no starter q there). A dir opens with
// its own path as cwd; a file opens with its parent as cwd. Both prime a
// starter prompt telling Claude to load fused-render's skills first — a file's
// prompt names it in quotes (not @-mention: names with spaces don't parse as
// one mention) — with two trailing newlines so the user's actual ask lands on
// a fresh line below (not auto-sent — the user still hits enter).
export function claudeDeepLink(path: string, isDir: boolean, name: string, parentDir: string): string {
  if (isDir) {
    const q = "This is a fused-render project — load its skills first.\n\n";
    return "claude-cli://open?cwd=" + encodeURIComponent(path) + "&q=" + encodeURIComponent(q);
  }
  const q = `This is a fused-render project — load its skills first, then read "${name}".\n\n`;
  return "claude-cli://open?cwd=" + encodeURIComponent(normDir(parentDir)) + "&q=" + encodeURIComponent(q);
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

// Drop every path that lives INSIDE another path of the same set, keeping the
// outermost ancestors (input order preserved).
// Needed because a search result list is a flat, recursive walk of the subtree:
// one shift-click / Cmd+A can select a folder AND entries inside it. A batch op
// that then walks the set entry by entry breaks on the descendants — the move or
// delete of the ancestor already took them, so the next call hits a path that no
// longer exists and reports a failure for work that actually succeeded.
// Acting on the ancestor alone is also the correct intent: moving/copying/
// removing a folder carries its contents along.
// Prefix + "/" is the same containment test clearClipboardIfDeleted uses; the
// self-comparison guard keeps a path from pruning itself.
export function pruneDescendantPaths(paths: string[]): string[] {
  return paths.filter((p) => !paths.some((other) => other !== p && p.startsWith(other + "/")));
}

// The ONE call every delete/trash site makes with the path that went away, so
// the shell's references to it cannot rot. Two of them today:
//
//   * the clipboard, if a cut/copy pointed at it (a later Paste would target a
//     source that no longer exists), and
//   * Recents, whose row would otherwise sit in the sidebar pointing at a
//     deleted file until some later navigation refreshed the list (RC-7 hides
//     it on the next GET, but a delete triggers no GET).
//
// One function rather than two adjacent calls at four sites: the next thing that
// needs to hear about a delete gets added here, not hunted for across the views.
export function notePathDeleted(deleted: string): void {
  clearClipboardIfDeleted(deleted);
  dropRecentsFor(deleted);
}

// Drop the module clipboard if it points at the removed entry — either the exact
// path, or something inside it when a directory was deleted (prefix + separator).
// A multi-entry clipboard only drops the paths that were removed; it clears
// entirely once nothing it referenced survives (the empty-list state is spelled
// null — see the Clipboard invariant).
function clearClipboardIfDeleted(deleted: string): void {
  const clip = getClipboard();
  if (!clip) return;
  const kept = clip.paths.filter((p) => p !== deleted && !p.startsWith(deleted + "/"));
  if (kept.length === clip.paths.length) return;
  // Not mirrored to the OS: this is repairing our own reference after a
  // delete, not the user copying something. See setClipboard's mirrorToOs.
  setClipboard(kept.length ? { ...clip, paths: kept } : null, false);
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
  let changed = false;
  const paths = clip.paths.map((p) => {
    if (p === oldPath) {
      changed = true;
      return newPath;
    }
    if (p.startsWith(oldPath + "/")) {
      changed = true;
      return newPath + p.slice(oldPath.length);
    }
    return p;
  });
  // Not mirrored, for the same reason as clearClipboardIfDeleted: a rename is
  // not a copy gesture, and the OS clipboard belongs to whoever last wrote it.
  if (changed) setClipboard({ ...clip, paths }, false);
}

// Turn a raw fs-action failure into a human sentence for the toast. The server
// speaks in terse wire strings ("conflict", "readonly", …) the QA screenshots
// caught leaking straight to users; every one of those is mapped here (never
// server-side — tests and the API contract pin the wire strings). `ctx.verb`
// is the past-tense-friendly action word ("rename", "paste", "create") and
// `ctx.name` the entry it acted on, so the sentence reads naturally. Anything
// unrecognized (a network blip, an unexpected 500) keeps its original text so
// nothing is silently swallowed.
export function friendlyFsError(err: unknown, ctx: { verb: string; name: string }): string {
  const { verb, name } = ctx;
  const raw = err instanceof Error ? err.message : String(err);
  const msg = raw.toLowerCase();
  const status = err && typeof err === "object" ? (err as { status?: number }).status : undefined;

  // Read-only target (403). The server's bare "readonly" for both write and
  // delete/rename/copy guards.
  if (msg.includes("readonly")) return `"${name}" is read-only — ${verb} isn't allowed here.`;

  // 409 "conflict". For a delete it can only mean a non-empty directory (its
  // contents block the removal — the server's "directory not empty" case);
  // every other verb's conflict is a destination name that's already taken.
  if (msg.includes("conflict")) {
    return verb === "delete"
      ? `"${name}" isn't empty — delete its contents first.`
      : `Couldn't ${verb} "${name}" — something with that name already exists.`;
  }

  // Compress on a repository that has nothing committed yet — neither
  // `git bundle --all` nor `git archive HEAD` has anything to write.
  if (msg.includes("has no commits"))
    return `"${name}" has no commits yet — there's nothing for git to archive.`;

  // Compress across a mount: reading the source would mean walking the whole
  // remote prefix, so the server refuses rather than hanging the mount.
  if (msg.includes("compress unsupported"))
    return `"${name}" is on a mounted location — compressing there isn't supported.`;

  // The containing folder was removed out from under the action.
  if (msg.includes("parent directory does not exist")) return "That folder no longer exists.";

  // Source gone (404 / "no such file or directory").
  if (msg.includes("no such file") || status === 404)
    return `"${name}" no longer exists — it may have been moved or deleted.`;

  // Trash move failed after we'd already committed to the recoverable path —
  // reassure that nothing was hard-deleted.
  if (msg.includes("cannot move to trash"))
    return `Couldn't move "${name}" to the Bin. Nothing was deleted.`;

  // Network / unknown: keep the original message so it isn't hidden.
  return `Couldn't ${verb} "${name}". ${raw}`;
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

// -- Compress ---------------------------------------------------------------

// The archive a Compress produces, in menu order. `ext` is appended whole, so
// a two-part extension (".tar.gz") stays intact.
const COMPRESS_FORMATS: { format: ArchiveFormat; ext: string; label: string; gitOnly: boolean }[] = [
  { format: "zip", ext: ".zip", label: "Compressed (.zip)", gitOnly: false },
  // Full history, clonable — the archive you send someone so they get the repo.
  { format: "git-bundle", ext: ".bundle", label: "Git bundle (.bundle)", gitOnly: true },
  // Tracked files at HEAD, no history — the archive you ship as a release.
  { format: "git-archive", ext: ".tar.gz", label: "Git archive of HEAD (.tar.gz)", gitOnly: true },
];

// Finder's archive naming: the first one takes the folder's own name
// ("myrepo.zip"), and a clash appends a number BEFORE the extension
// ("myrepo 2.zip"). Note this is not duplicateName's " copy" convention — an
// archive of a folder isn't a copy of it, and Finder names the two differently.
// The folder name is never split on ".", so "my.app" stays "my.app 2.zip".
export function archiveName(folder: string, counter: number, ext: string): string {
  return counter <= 1 ? folder + ext : `${folder} ${counter}${ext}`;
}

// First free archive path for compressing `folderName` in `parentDir`, chosen
// by listing the folder first so Compress never 409s on a name that's already
// there. Mirrors freeDuplicatePath, truncated-listing stat probe included.
export async function freeArchivePath(
  parentDir: string,
  folderName: string,
  ext: string
): Promise<string> {
  const dir = normDir(parentDir);
  const { entries, truncated } = await listDir(dir);
  const taken = new Set(entries.map((e) => e.name));
  let i = 1;
  let candidate = archiveName(folderName, i, ext);
  while (taken.has(candidate)) candidate = archiveName(folderName, ++i, ext);
  while (truncated && (await pathExists(join(dir, candidate)))) {
    if (i > 100) break; // bounded like firstFreeProbed; the server's 409 backstops us
    candidate = archiveName(folderName, ++i, ext);
  }
  return join(dir, candidate);
}

// Build the Compress submenu. The two git formats archive the WHOLE repository
// (`bundle --all`, `archive HEAD`), so they appear only when the folder is a
// repo root — `isRepoRoot` comes from the lazy gitRepoInfo probe, which is why
// this submenu is loaded on hover rather than built on every right-click.
// Returns MenuEntry[] (not MenuItem[]) for the separator; no entry carries a
// submenu of its own, since ContextMenu renders exactly one level.
export function buildCompressItems(
  isRepoRoot: boolean,
  onSelect: (format: ArchiveFormat, ext: string) => void
): MenuEntry[] {
  const row = (f: (typeof COMPRESS_FORMATS)[number]): MenuItem => ({
    label: f.label,
    onClick: () => onSelect(f.format, f.ext),
  });
  const items: MenuEntry[] = COMPRESS_FORMATS.filter((f) => !f.gitOnly).map(row);
  if (isRepoRoot) items.push("separator", ...COMPRESS_FORMATS.filter((f) => f.gitOnly).map(row));
  return items;
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
