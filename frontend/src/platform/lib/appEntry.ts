// How an app card resolves, links to and opens its app — shared by every
// surface that renders one (the /apps hub's preview cards, the card context
// menu) so they can never disagree about what clicking a card does.
//
// They did disagree. Each carried its own inline copy of the rule, and Home's
// Recent row had already drifted from the hub's card. The rule now: an app card
// opens its FOLDER IN THE FILE EXPLORER, as a plain listing — no `_mode` — and
// only an app whose entry is a lone non-page file opens that file instead.
//
// It used to open the folder in the `app` view (`?_mode=app`) under a builder
// route of its own, /apps/<tag>/<name>. Both are gone. The route was a second
// namespace for a folder the explorer already addresses, and pinning the mode
// picked the view on the user's behalf: the explorer's mode switcher offers
// `app` for exactly these folders (templates/app/condition.py), so landing on
// the listing keeps the app one click away while leaving every other thing you
// might want to do with the folder — read a file, open a chat, see its history
// — equally reachable.
//
// OPENING an app is not BUILDING one: the `claude` view (the app beside a
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
import { navigate, urlForFsPath } from "./router";

// The file this card is about, tolerating a backend that predates `entry`.
// `entry` is "the file a card opens and previews"; `entry_html` is the narrower
// "that entry is a renderable page". They are the same file for a workspace app.
export function entryOf(app: AppInfo): string | null {
  return app.entry ?? app.entry_html;
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
  // A page entry means the folder is the subject — the app view, the chat and
  // the history all rediscover the entry from the folder, so the folder is what
  // the card opens, in the explorer's own listing.
  if (app.entry_html) return { path: app.path, opts: { isDir: true } };
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
// Nothing rides in the query string: the destination takes its own default view
// (a folder's listing), and the params navigate() would otherwise carry are
// in-session layout the user toggled, which a new tab does not inherit. Built
// through the router's own codec, so an app path with a space, a `#` or a
// non-ASCII name encodes exactly as in-app navigation encodes it.
export function hrefFor(app: AppInfo): string {
  return urlForFsPath(openTargetFor(app).path);
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
