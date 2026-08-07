// How an app card resolves, links to and opens its app — shared by every
// surface that renders one (the /apps hub's preview cards, the builder
// sidebar's recents) so they can never disagree about what clicking a card does.
//
// They did disagree. Each carried its own inline copy of the rule, and Home's
// Recent row had already drifted from the hub's card. The rule: an app with a
// page entry opens its FOLDER in the `app` view — the app itself, full-bleed,
// for USING it — and one without a resolvable entry falls back to the plain
// folder listing so a card is never dead.
//
// OPENING an app is not BUILDING one: the `claude_split` view (the app beside a
// Claude chat) is where a new app is created and iterated on, and the create
// path still lands there (HomeHero). Everything in this module is the open path.
//
// The cards are ANCHORS, not buttons, so the browser's own "open in a new tab"
// gestures work on them: middle-click, Cmd/Ctrl-click, and the context menu's
// Open in New Tab. That needs two things to stay in lockstep — the href and the
// click handler — which is the second reason this module exists: `hrefFor` and
// `openTargetFor` resolve the same target, so a new tab and a left click can't
// land in different places.
import type { AppInfo } from "./api";
import { APP_ROUTE_PREFIX, navigate, navigateUrl, urlForFsPath } from "./router";

// The builder route for an app — /apps/<tag>/<name>, straight from the
// AppInfo identity (no fs-path round trip needed).
export function appRouteUrl(app: Pick<AppInfo, "tag" | "name">): string {
  return APP_ROUTE_PREFIX + encodeURIComponent(app.tag) + "/" + encodeURIComponent(app.name);
}

// The file this card is about, tolerating a backend that predates `entry`.
// `entry` is "the file a card opens and previews"; `entry_html` is the narrower
// "that entry is a renderable page". They are the same file for a workspace app.
export function entryOf(app: AppInfo): string | null {
  return app.entry ?? app.entry_html;
}

// Whether opening this app means opening its FOLDER in an app view, which is
// what a page entry earns: those templates rediscover the entry from the folder
// and want an index.html or exactly one top-level .html there.
function opensAsProject(app: AppInfo): boolean {
  return Boolean(app.entry_html);
}

// The virtual tag for registry-backed linked apps (fused_render/linked_apps.py).
// Their folders live OUTSIDE the workspace, so the builder's pretty route
// (/apps/<tag>/<name>) can never resolve them — fsPathFromAppRoute is a pure
// codec against fused_dir. A linked app therefore opens through the explorer
// URL of its real folder (still `_mode=app`, the same full-bleed view).
export const LINKED_TAG = "linked";

function usesBuilderRoute(app: AppInfo): boolean {
  return opensAsProject(app) && app.tag !== LINKED_TAG;
}

export interface OpenTarget {
  path: string;
  opts?: { isDir?: boolean; mode?: string };
}

// The template an app folder opens in: the app itself, full-bleed
// (fused_render/templates/app). Set EXPLICITLY rather than left to the default —
// with `_mode` absent, Preview's defaultTemplate picks the first UNCONDITIONAL
// entry, which for a directory is `_listing`, i.e. the folder's file list.
export const APP_OPEN_MODE = "app";

// Where a card goes when activated, AS A VALUE. Split out from openApp so the
// rule can be tested without touching `navigate`: mocking a module that half
// the shell imports is process-wide in bun, and it leaks into whichever suite
// runs next. A pure function needs no mock at all.
export function openTargetFor(app: AppInfo): OpenTarget {
  if (opensAsProject(app)) {
    return { path: app.path, opts: { isDir: true, mode: APP_OPEN_MODE } };
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
// plain app view); the `preview` param navigate() keeps sticky across
// folder navigation deliberately does NOT, since it is in-session layout the
// user toggled and a new tab is a fresh session. Built through the router's own
// codec, so an app path with a space, a `#` or a non-ASCII name encodes exactly
// as in-app navigation encodes it.
export function hrefFor(app: AppInfo): string {
  // A project open lands in the BUILDER namespace (/apps/<tag>/<name>) — the
  // app under the builder's own sidebar, with the header's mode switcher pinned
  // to the app modes; the fallbacks stay explorer URLs (folder / single file).
  if (usesBuilderRoute(app)) return appRouteUrl(app) + "?_mode=" + APP_OPEN_MODE;
  const { path, opts } = openTargetFor(app);
  const search = opts?.mode ? "?_mode=" + encodeURIComponent(opts.mode) : "";
  return urlForFsPath(path, search);
}

export function openApp(app: AppInfo): void {
  if (usesBuilderRoute(app)) {
    // navigateUrl, not navigate: the builder URL is already fully formed
    // (navigate speaks fs paths and would re-encode into the explorer prefix).
    navigateUrl(hrefFor(app), { isDir: true });
    return;
  }
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
