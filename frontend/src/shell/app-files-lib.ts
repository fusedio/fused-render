// Pure helpers for the app page's Files tab (shell/AppFiles.tsx): the tree
// built from a walk, the `?file=` guard, and the template list / iframe URL
// rules borrowed from the explorer. No DOM here, so it loads anywhere
// (a bare test runtime included).
import type { TemplateEntry, WalkEntry } from "@platform/lib/api";
import { partitionModes } from "@platform/lib/mode-visibility";
import { snapshotFrameSrc, type ResolvedSnapshot } from "@platform/lib/snapshot-param";

// The same set ModeSwitcher.tsx exports — repeated here rather than imported
// because that module is JSX with a fetch-backed icon renderer, and this file
// is meant to load in a bare test runtime. Two sentinels, unlikely to grow.
const KNOWN_SENTINEL_MODES: ReadonlySet<string> = new Set(["_render", "_listing"]);

export interface TreeNode {
  name: string;
  rel: string;
  isDir: boolean;
  /** Bytes for a file; null for a folder (walk convention). */
  size: number | null;
  mtime: number | null;
  children: TreeNode[];
}

/** Files under a node, recursively — the count a folder row wears. */
export function fileCount(node: TreeNode): number {
  let n = 0;
  for (const c of node.children) n += c.isDir ? fileCount(c) : 1;
  return n;
}

/** Assemble walk entries (posix `rel`, any order) into a tree — folders first,
 *  then files, both alphabetical, case-insensitive. A file whose parent folder
 *  the walk did not emit (a capped walk can do that) gets an implied folder so
 *  nothing is dropped on the floor. */
export function buildTree(entries: WalkEntry[]): TreeNode[] {
  const root: TreeNode = { name: "", rel: "", isDir: true, size: null, mtime: null, children: [] };
  const byRel = new Map<string, TreeNode>([["", root]]);
  const ensureDir = (rel: string): TreeNode => {
    const have = byRel.get(rel);
    if (have) return have;
    const cut = rel.lastIndexOf("/");
    const parent = ensureDir(cut < 0 ? "" : rel.slice(0, cut));
    const node: TreeNode = {
      name: rel.slice(cut + 1),
      rel,
      isDir: true,
      size: null,
      mtime: null,
      children: [],
    };
    parent.children.push(node);
    byRel.set(rel, node);
    return node;
  };
  for (const e of entries) {
    if (e.is_dir) {
      ensureDir(e.rel);
      continue;
    }
    const cut = e.rel.lastIndexOf("/");
    const parent = ensureDir(cut < 0 ? "" : e.rel.slice(0, cut));
    parent.children.push({
      name: e.rel.slice(cut + 1),
      rel: e.rel,
      isDir: false,
      size: e.size,
      mtime: e.mtime,
      children: [],
    });
  }
  const sort = (nodes: TreeNode[]) => {
    nodes.sort(
      (a, b) =>
        Number(b.isDir) - Number(a.isDir) ||
        a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
    );
    for (const n of nodes) if (n.isDir) sort(n.children);
  };
  sort(root.children);
  return root.children;
}

/** A `?file=` value fit to join onto the app folder: relative, posix, no empty
 *  or dot segments. Anything else is "no selection". */
export function safeRel(raw: string | null): string | null {
  if (!raw) return null;
  if (raw.startsWith("/") || raw.includes("\\")) return null;
  const parts = raw.split("/");
  if (parts.some((p) => !p || p === "." || p === "..")) return null;
  return raw;
}

/** Every ancestor folder of a rel path — what must be open to see it. */
export function ancestorsOf(rel: string): string[] {
  const out: string[] = [];
  const parts = rel.split("/");
  for (let i = 1; i < parts.length; i++) out.push(parts.slice(0, i).join("/"));
  return out;
}

/** The content templates on offer for a file: the server's list minus unknown
 *  sentinels (Preview does the same), minus the companions (claude/git/mcp —
 *  they talk ABOUT a file, and this pane shows one), minus `_listing` (a
 *  folder's view, never a file's). */
export function contentTemplates(templates: TemplateEntry[]): TemplateEntry[] {
  return partitionModes(
    templates.filter((t) => t.path !== null || KNOWN_SENTINEL_MODES.has(t.mode)),
  ).content.filter((t) => t.mode !== "_listing");
}

/** Whether the Files tab's right pane should show a loading skeleton rather
 *  than "Pick a file to see it here." — a file IS selected (`rel`, the
 *  `?file=` value) but the effective read target has not resolved yet
 *  (`file` is null while a snapshot is pending — see AppFiles.tsx's own
 *  `effectiveDir`/`file` comments). Code review finding 2, second round:
 *  before this existed, AppFiles.tsx used `!file` alone to decide between
 *  the file view and the blank "nothing selected" state, so a pending
 *  resolve read identically to no selection at all even with a real
 *  `?file=` on the URL — and stayed that way forever if the resolve then
 *  failed (finding 1). Extracted as its own pure function (rather than left
 *  as an inline expression in AppFiles.tsx, which has no render-test
 *  precedent) so this exact gate has a direct regression test. */
export function isAwaitingFile(rel: string | null, file: string | null): boolean {
  return rel !== null && file === null;
}

/** The iframe URL for a file in a template — Preview.tsx's shape (No
 *  `_preview`, a real open (D301), and no `_remote`, the workspace is local),
 *  routed through the shared `snapshotFrameSrc` (platform/lib/snapshot-param.ts)
 *  rather than composed by hand. Code review finding 2: the hand-rolled
 *  version passed `file` (already rewritten onto the extracted tree by this
 *  component's own `effectiveDir`, see AppFiles.tsx) straight into the src
 *  with no `_snapshot`/`_snapshot_dir`/`_snapshot_app` alongside it — the
 *  framed runtime then had no snapshot awareness of its own (the same gap
 *  Preview.tsx's `_render` sentinel comment on `snapParams` describes), and
 *  an editor template's write gate stayed silently open under a snapshotted
 *  file instead of refusing with the snapshot message.
 *
 *  `snap`/`sha` are AppPage's own resolution (AppPage.tsx passes
 *  `snapshot.snap`/`snapshot.sha` straight through) — `file` is already
 *  resolved against `snap` by the caller (`effectiveDir + "/" + rel`), so the
 *  rewrite `snapshotFrameSrc` performs internally is a no-op here (the path
 *  is already outside `snap.app_dir`); what this call adds is the three
 *  params, and — a caller that reaches this function should already have
 *  gated on `pending` (AppFiles.tsx's `file` is null while pending, so this
 *  is never actually called in that window) — the shared pending check as a
 *  second line of defense. */
export function renderSrc(
  file: string,
  t: TemplateEntry,
  snap: ResolvedSnapshot | null,
  sha: string | null,
): string | null {
  if (t.mode === "_render") return snapshotFrameSrc({ snap, sha, path: file });
  return snapshotFrameSrc({
    snap,
    sha,
    path: t.path as string,
    // `t.path` is the TEMPLATE's own file, never the subject (`file`, already
    // resolved by the caller and carried instead via `_file` below) — same
    // reasoning as Preview.tsx's own non-`_render` branch (code review
    // finding 4, second round).
    rewritePath: false,
    extra: `&_file=${encodeURIComponent(file)}`,
  });
}
