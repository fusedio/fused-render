// How an app card resolves, links to and opens its app — shared by the three
// places that render one (Home's Recent rows, Home's app tiles, the /apps hub's
// preview cards) so they can never disagree about what clicking a card does.
//
// They did disagree. Each carried its own inline copy of the rule, and Home's
// Recent row had already drifted from the hub's card. The rule itself is
// unchanged: an app with a page entry opens its FOLDER in the claude_split view
// (the app beside a Claude chat) — that is what an app is for — and one without
// a resolvable entry falls back to the plain folder listing so a card is never
// dead.
//
// The cards are ANCHORS, not buttons, so the browser's own "open in a new tab"
// gestures work on them: middle-click, Cmd/Ctrl-click, and the context menu's
// Open in New Tab. That needs two things to stay in lockstep — the href and the
// click handler — which is the second reason this module exists: `hrefFor` and
// `openTargetFor` resolve the same target, so a new tab and a left click can't
// land in different places.
import type { AppInfo } from "./api";
import { navigate, urlForFsPath } from "./router";

// The file this card is about, tolerating a backend that predates `entry`.
// `entry` is "the file a card opens and previews"; `entry_html` is the narrower
// "that entry is a renderable page". They are the same file for a workspace app.
export function entryOf(app: AppInfo): string | null {
  return app.entry ?? app.entry_html;
}

// Sources whose card is a FOLDER the user owns — the only ones a page entry
// may open as a project. An allowlist rather than the blocklist it began as:
// when claude-session/claude-upload arrived (cards whose `path` IS the file,
// not a folder), a blocklist would have opened claude_split on a file path —
// the same class of breakage the claude-science exclusion already fixed once.
// A new source now defaults to the safe behaviour (open the file) until it is
// added here deliberately.
const PROJECT_SOURCES = new Set(["workspace", "claude-code"]);

// Whether opening this app means opening its folder beside a Claude chat, which
// is what a page entry earns: claude_split rediscovers the entry from the folder
// and wants exactly one top-level .html there.
//
// A page entry is necessary but not sufficient. A Claude Science artifact opens
// the FILE even when it is a page, for two independent reasons: that folder
// holds one file per VERSION, so claude_split's "exactly one top-level .html"
// resolves to nothing the moment a report is saved twice; and the chat would
// run cwd'd inside ~/.claude-science, a store this app only ever reads. A
// claude-session or claude-upload card is a single file outright.
function opensAsProject(app: AppInfo): boolean {
  return Boolean(app.entry_html) && (!app.source || PROJECT_SOURCES.has(app.source));
}

// Image types the thumbnail can paint directly through /api/fs/raw. Kept to
// what a browser renders in an <img> unaided — anything else falls back to the
// monogram rather than risking an empty box.
const IMAGE_SUFFIXES = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp"];

export function isImageEntry(path: string | null): boolean {
  if (!path) return false;
  const lower = path.toLowerCase();
  return IMAGE_SUFFIXES.some((suffix) => lower.endsWith(suffix));
}

// The extension a thumbnail can label itself with when it has nothing to show —
// "CSV" says what the card holds where the first letter of its filename says
// nothing. Null when there is no usable extension (bare name, dotfile, or one
// too long to be one), leaving the monogram as the fallback.
//
// The ceiling is set by the longest extension that actually turns up here
// rather than by taste: `parquet` and `geojson` are everyday files in this app,
// and a limit that quietly dropped them would leave exactly the cards that most
// need a label without one.
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

export interface OpenTarget {
  path: string;
  opts?: { isDir?: boolean; mode?: string };
}

// Where a card goes when activated, AS A VALUE. Split out from openApp so the
// rule can be tested without touching `navigate`: mocking a module that half
// the shell imports is process-wide in bun, and it leaks into whichever suite
// runs next. A pure function needs no mock at all.
export function openTargetFor(app: AppInfo): OpenTarget {
  if (opensAsProject(app)) {
    return { path: app.path, opts: { isDir: true, mode: "claude_split" } };
  }
  // A single file entry that isn't a page opens as the file. No workspace app
  // reaches this today (its entry is its .html or it has none), but the branch
  // is the contract `entry` exists for, and it keeps the fallback below meaning
  // only "nothing to open".
  const entry = entryOf(app);
  if (entry) return { path: entry };
  return { path: app.path, opts: { isDir: true } };
}

// The URL for that target — what the anchor's href carries, so the browser can
// open it in a new tab without the shell's help.
//
// `_mode` rides along because it selects the destination's template (the
// claude_split split view); the `preview` param navigate() keeps sticky across
// folder navigation deliberately does NOT, since it is in-session layout the
// user toggled and a new tab is a fresh session. Built through the router's own
// codec, so an app path with a space, a `#` or a non-ASCII name encodes exactly
// as in-app navigation encodes it.
export function hrefFor(app: AppInfo): string {
  const { path, opts } = openTargetFor(app);
  const search = opts?.mode ? "?_mode=" + encodeURIComponent(opts.mode) : "";
  return urlForFsPath(path, search);
}

export function openApp(app: AppInfo): void {
  const { path, opts } = openTargetFor(app);
  navigate(path, opts);
}

// Structural, like platform.ts's ModifierEvent: a React MouseEvent satisfies it,
// and so does a plain object in a test.
export interface CardClickEvent {
  button: number;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  defaultPrevented: boolean;
  preventDefault(): void;
}

// Whether the BROWSER should handle this click on a card, rather than the shell
// intercepting it for an in-app navigation. True for every gesture that means
// "somewhere other than this tab": middle (or any non-primary) button, and any
// modifier — Cmd/Ctrl for a new tab, Shift for a new window, Alt for download.
//
// Deliberately NOT `isMod()`, the one exception to platform.ts's rule that
// modifier tests go through it. `isMod` is exclusive by design (Meta on macOS,
// Ctrl elsewhere) because a SHORTCUT must not fire on the wrong platform's
// chord. This is not a shortcut: it asks whether the browser has already
// claimed the click, and the browser's own rule is any-modifier. Gating on
// `isMod` would swallow Shift-click everywhere and Ctrl-click on macOS — where
// it opens the context menu — and the card would fight the browser.
export function isBrowserHandledClick(e: CardClickEvent): boolean {
  return e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey;
}

// The click handler every app card shares. Left click with no modifier is an
// in-app navigation (no page reload, no lost state); everything else is left to
// the href, which is why the anchor must always carry one.
export function onAppCardClick(e: CardClickEvent, app: AppInfo): void {
  if (e.defaultPrevented || isBrowserHandledClick(e)) return;
  e.preventDefault();
  openApp(app);
}
