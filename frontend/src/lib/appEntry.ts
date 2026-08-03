// How an app card resolves, opens and previews its entry — shared by the two
// card components (AppCard on Home, AppPreviewCard in the /apps hub) so they
// can never disagree about what clicking a card does.
//
// Two sources feed the listing (D205) and they differ in ways that matter here:
//
//   * a WORKSPACE app with a page entry opens its FOLDER in the claude_split
//     view (the app beside a Claude chat) — unchanged, that's what one is for;
//   * a CLAUDE SCIENCE artifact opens the FILE, whatever its type, letting the
//     shell's template registry dispatch it (.png -> image, .csv -> duckdb,
//     .html -> the rendered page). Never the folder-with-a-chat, for two
//     independent reasons. It would break: claude_split rediscovers the entry
//     from the folder and wants exactly one top-level .html (templates/
//     claude_split/app.py), while an artifact folder holds one file per
//     VERSION — so the second save of a report leaves the left pane with no
//     entry at all, even though the listing already resolved entry_html to the
//     newest one. And it would be wrong even if it worked: that chat would run
//     cwd'd inside ~/.claude-science, a store this app only ever reads.
//   * anything else with an entry opens the file; no entry at all falls back to
//     the plain folder listing.
import type { AppInfo } from "./api";
import { navigate } from "./router";

// Image types the thumbnail can render directly through /api/fs/raw. Kept to
// what a browser paints in an <img> without help — anything else falls back to
// the monogram rather than risking an empty box.
const IMAGE_SUFFIXES = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp"];

// The file this card is about, tolerating a backend that predates `entry`.
export function entryOf(app: AppInfo): string | null {
  return app.entry ?? app.entry_html;
}

export function isImageEntry(path: string | null): boolean {
  if (!path) return false;
  const lower = path.toLowerCase();
  return IMAGE_SUFFIXES.some((suffix) => lower.endsWith(suffix));
}

// The extension a thumbnail can label itself with when it has nothing to show —
// "CSV" says what the card holds, where the first letter of its filename says
// nothing. Null when there's no usable extension (bare name, dotfile, or
// something too long to be one), leaving the monogram as the fallback.
//
// The 8-char ceiling is set by the longest extension that actually turns up
// here rather than by taste: `parquet` and `geojson` are everyday files in this
// app, and a limit that quietly dropped them would leave exactly the cards that
// most need a label without one.
const EXT_MAX = 8;

export function extLabel(path: string | null): string | null {
  if (!path) return null;
  const base = path.slice(path.lastIndexOf("/") + 1);
  const dot = base.lastIndexOf(".");
  if (dot <= 0) return null; // no dot, or a dotfile whose "extension" is its name
  const ext = base.slice(dot + 1);
  return ext.length >= 1 && ext.length <= EXT_MAX && /^[a-z0-9]+$/i.test(ext)
    ? ext.toUpperCase()
    : null;
}

// Bytes for an image entry. /api/fs/raw serves any absolute path with a
// content-type guessed from its name; it sends `nosniff` and downgrades
// scriptable types, but only for document loads — an <img> is not one, so an
// image still arrives as an image (server/proxy.py _harden_raw).
export function rawUrl(path: string): string {
  return `/api/fs/raw?path=${encodeURIComponent(path)}`;
}

// Whether opening this app means opening its folder beside a Claude chat.
// A page entry is necessary but not sufficient: the folder has to be one the
// user owns and claude_split can resolve, which a read-only, version-stacked
// artifact directory is not (see the header).
function opensAsProject(app: AppInfo): boolean {
  return Boolean(app.entry_html) && app.source !== "claude-science";
}

export interface OpenTarget {
  path: string;
  opts?: { isDir?: boolean; mode?: string };
}

// Where a card goes when clicked, as a value. Split out from openApp so the
// rule can be tested without touching `navigate`: this module already imports
// router, and mocking a module that half the shell imports is process-wide in
// bun — it leaked into another file's suite and broke it. A pure function needs
// no mock at all.
export function openTargetFor(app: AppInfo): OpenTarget {
  if (opensAsProject(app)) {
    return { path: app.path, opts: { isDir: true, mode: "claude_split" } };
  }
  const entry = entryOf(app);
  // The artifact itself. Its folder would be a listing of one opaque file — for
  // a Claude Science artifact, a directory named after a UUID.
  if (entry) return { path: entry };
  return { path: app.path, opts: { isDir: true } };
}

export function openApp(app: AppInfo): void {
  const { path, opts } = openTargetFor(app);
  navigate(path, opts);
}
