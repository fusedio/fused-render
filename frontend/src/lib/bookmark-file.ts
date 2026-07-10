// "Save to disk" for a bookmark (SB-8, D98): compute the portable
// `<name>.bookmark` JSON file — where it goes and what it holds — from the
// bookmark's verbatim shell url. Pure logic, no DOM, no fetch; the Sidebar
// hands the result to POST /api/bookmarks/export (api.ts exportBookmarkFile),
// which only validates and writes.
//
// Format v1: `{version, name, icon?, kind, path?, search}`. A single-view
// bookmark saves into its target's own directory with `path` relative to it;
// a panel/tab bookmark saves into the deepest common ancestor directory of
// all `_layout` leaves, each leaf path rewritten relative to that dir
// (grammar, nesting, per-leaf queries and global params untouched). Every
// leaf sits under the save dir, so relative paths never contain `..`.
//
// The inverse direction (SB-9, D99) lives here too: bookmarkOpenUrl() takes a
// parsed `.bookmark` + the file's own directory and absolutizes every relative
// path back, producing the shell URL the `_bookmark` sentinel redirects to.
import { rootedFsPath, urlForFsPath, VIEW_PREFIX, EMBED_PREFIX } from "./router";
import {
  splitShellSearch,
  parseLayout,
  flattenToLeaves,
  encodeNode,
  buildSentinelUrl,
} from "./layout-codec";
import { sanitizeBookmarkStem, type Bookmark } from "./bookmarks";

export interface BookmarkSaveTarget {
  dir: string; // absolute directory the .bookmark file lands in
  filename: string; // `<sanitized name>.bookmark`
  content: string; // the file's exact bytes (2-space JSON + trailing newline)
}

const SENTINELS = ["_panel", "_tab"] as const;

// An absolute fs path split into its root ("" for POSIX, "C:" for a Windows
// drive) plus path segments; null for anything relative. Distinct roots never
// share an ancestor (different drive letters), matching rootedFsPath's two
// absolute shapes.
interface AbsPath {
  root: string;
  segs: string[];
}

function splitAbs(p: string): AbsPath | null {
  if (/^[A-Za-z]:\//.test(p)) return { root: p.slice(0, 2), segs: p.slice(3).split("/").filter(Boolean) };
  if (p.startsWith("/")) return { root: "", segs: p.split("/").filter(Boolean) };
  return null;
}

function joinAbs(root: string, segs: string[]): string {
  return root + "/" + segs.join("/");
}

// Bookmark name -> filename stem: sanitizeBookmarkStem (lib/bookmarks.ts) —
// the same function D97 uniqueness keys on, so distinct names can never
// sanitize to the same filename. No collision handling needed here.
const sanitizeStem = sanitizeBookmarkStem;

// Decoded path segments of a /view/ or /embed/ url, or null for any other
// pathname (mirrors router.fsPathFromLocation, which only reads location).
function shellUrlSegments(pathname: string): string[] | null {
  for (const prefix of [VIEW_PREFIX, EMBED_PREFIX]) {
    if (pathname.startsWith(prefix)) {
      return pathname
        .slice(prefix.length)
        .split("/")
        .filter((s) => s.length > 0)
        .map(decodeURIComponent);
    }
  }
  return null;
}

// Where saving this bookmark would write, or null when it is not savable:
// no target / any layout leaf without an absolute fs path / no common
// ancestor (different drive roots) / a name that sanitizes to nothing.
export function bookmarkSaveTarget(b: Bookmark): BookmarkSaveTarget | null {
  const stem = sanitizeStem(b.name);
  if (!stem || stem === "." || stem === "..") return null;

  const qIdx = b.url.indexOf("?");
  const pathname = qIdx !== -1 ? b.url.slice(0, qIdx) : b.url;
  const search = qIdx !== -1 ? b.url.slice(qIdx + 1) : "";
  const segments = shellUrlSegments(pathname);
  if (!segments || segments.length === 0) return null;

  const record: Record<string, unknown> = { version: 1, name: b.name };
  if (b.icon) record.icon = b.icon;

  const sentinel = segments.length === 1 && (SENTINELS as readonly string[]).includes(segments[0]);
  let dir: string;
  if (!sentinel) {
    const abs = splitAbs(rootedFsPath(segments.join("/")));
    if (!abs || abs.segs.length === 0) return null; // the fs root has no containing dir
    dir = joinAbs(abs.root, abs.segs.slice(0, -1));
    record.kind = "single";
    record.path = abs.segs[abs.segs.length - 1];
    record.search = search;
  } else {
    const { layout, params } = splitShellSearch(search);
    if (layout === null) return null;
    const tree = parseLayout(layout);
    const leaves = flattenToLeaves(tree);
    if (leaves.length === 0) return null;
    const absLeaves: AbsPath[] = [];
    for (const l of leaves) {
      const abs = splitAbs(l.path);
      if (!abs || abs.segs.length === 0) return null;
      absLeaves.push(abs);
    }
    // Deepest common ancestor, path-segment-wise (never a string prefix —
    // `/a/bc` vs `/a/b` share `/a`), over the leaves' CONTAINING dirs so each
    // relative path keeps at least its final segment.
    const [first, ...rest] = absLeaves;
    let common = first.segs.slice(0, -1);
    for (const abs of rest) {
      if (abs.root !== first.root) return null; // different drives: no ancestor
      const limit = Math.min(common.length, abs.segs.length - 1);
      let i = 0;
      while (i < limit && common[i] === abs.segs[i]) i++;
      common = common.slice(0, i);
    }
    // Rewrite each leaf in place (flattenToLeaves returns the tree's own
    // nodes) and re-encode; buildSentinelUrl re-appends the globals and wraps
    // the layout last, exactly like the live shell.
    for (let i = 0; i < leaves.length; i++) {
      leaves[i].path = absLeaves[i].segs.slice(common.length).join("/");
    }
    dir = joinAbs(first.root, common);
    record.kind = segments[0] === "_panel" ? "panel" : "tab";
    record.search = buildSentinelUrl("", encodeNode(tree), params).slice(1); // drop the "?" it prefixes
  }

  return {
    dir,
    filename: stem + ".bookmark",
    content: JSON.stringify(record, null, 2) + "\n",
  };
}

// --- Open direction (SB-9, D99) ---------------------------------------------

// A path from a `.bookmark` file made absolute against the file's own dir.
// Already-absolute paths pass through unchanged (defensive — v1 writers only
// emit relative ones, but a hand-edited file shouldn't double-prefix).
function absolutize(dir: string, path: string): string {
  if (splitAbs(path)) return path;
  return dir.replace(/\/+$/, "") + "/" + path;
}

// The shell URL a parsed `.bookmark` opens at, its relative paths resolved
// against `dir` (the file's own directory). Throws with a readable message on
// any malformed record — the caller (views/BookmarkOpen.tsx) renders it.
export function bookmarkOpenUrl(dir: string, bookmark: Record<string, unknown>): string {
  const kind = bookmark.kind;
  const search = typeof bookmark.search === "string" ? bookmark.search : "";
  if (kind === "single") {
    if (typeof bookmark.path !== "string" || !bookmark.path) {
      throw new Error("malformed bookmark: kind 'single' without a path");
    }
    return urlForFsPath(absolutize(dir, bookmark.path), search ? "?" + search : "");
  }
  if (kind !== "panel" && kind !== "tab") {
    throw new Error(`malformed bookmark: unknown kind ${JSON.stringify(kind)}`);
  }
  const { layout, params } = splitShellSearch(search);
  if (layout === null) {
    throw new Error(`malformed bookmark: kind '${kind}' without a _layout`);
  }
  const tree = parseLayout(layout);
  const leaves = flattenToLeaves(tree);
  if (leaves.length === 0) throw new Error("malformed bookmark: empty layout");
  // Rewrite in place (flattenToLeaves returns the tree's own nodes), the
  // exact inverse of the relativize loop in bookmarkSaveTarget above.
  for (const l of leaves) l.path = absolutize(dir, l.path);
  const sentinel = kind === "panel" ? "_panel" : "_tab";
  return buildSentinelUrl(VIEW_PREFIX + sentinel, encodeNode(tree), params);
}
