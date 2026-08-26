// Pure helpers for the app page's Files tab (shell/AppFiles.tsx): the tree
// built from a walk, the `?file=` guard, and the template list / iframe URL
// rules borrowed from the explorer. No DOM here, so it loads anywhere
// (a bare test runtime included).
import type { TemplateEntry, WalkEntry } from "@platform/lib/api";
import { partitionModes } from "@platform/lib/mode-visibility";

// The same set ModeSwitcher.tsx exports — repeated here rather than imported
// because that module is JSX with a fetch-backed icon renderer, and this file
// is meant to load in a bare test runtime. Two sentinels, unlikely to grow.
const KNOWN_SENTINEL_MODES: ReadonlySet<string> = new Set(["_render", "_listing"]);

export interface TreeNode {
  name: string;
  rel: string;
  isDir: boolean;
  children: TreeNode[];
}

/** Assemble walk entries (posix `rel`, any order) into a tree — folders first,
 *  then files, both alphabetical, case-insensitive. A file whose parent folder
 *  the walk did not emit (a capped walk can do that) gets an implied folder so
 *  nothing is dropped on the floor. */
export function buildTree(entries: WalkEntry[]): TreeNode[] {
  const root: TreeNode = { name: "", rel: "", isDir: true, children: [] };
  const byRel = new Map<string, TreeNode>([["", root]]);
  const ensureDir = (rel: string): TreeNode => {
    const have = byRel.get(rel);
    if (have) return have;
    const cut = rel.lastIndexOf("/");
    const parent = ensureDir(cut < 0 ? "" : rel.slice(0, cut));
    const node: TreeNode = { name: rel.slice(cut + 1), rel, isDir: true, children: [] };
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
    parent.children.push({ name: e.rel.slice(cut + 1), rel: e.rel, isDir: false, children: [] });
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

/** The iframe URL for a file in a template — Preview.tsx's shape. No
 *  `_preview` (a real open, D301) and no `_remote` (the workspace is local). */
export function renderSrc(file: string, t: TemplateEntry): string {
  if (t.mode === "_render") return `/render?path=${encodeURIComponent(file)}`;
  return (
    `/render?path=${encodeURIComponent(t.path as string)}` +
    `&_file=${encodeURIComponent(file)}`
  );
}
