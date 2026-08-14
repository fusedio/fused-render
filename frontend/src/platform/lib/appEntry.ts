// How an app card resolves, links to and opens its app — shared by every
// surface that renders one (the /apps hub's preview cards, the card context
// menu) so they can never disagree about what clicking a card does.
//
// They did disagree. Each carried its own inline copy of the rule, and Home's
// Recent row had already drifted from the hub's card. The rule now: an app card
// opens the app's ENTRY PAGE — the html FILE itself, in the explorer's ordinary
// file view — and only an app with no page at all opens its folder as a listing.
//
// That is a REVERSAL, recorded as D269 and made on the owner's explicit
// instruction: "a folder that has a top-level .html is an app, and every surface
// that shows or opens that folder should show/open THAT PAGE." For one release
// the card opened the FOLDER as a plain listing, because the destination it used
// to have was an `app` view (`?_mode=app`) under a builder route of its own,
// /apps/<tag>/<name>; the route went first (D262) and the app view itself next
// (D264), and with no "open as an app" left, the folder's listing looked like
// the only destination there was. It was not: the entry page is an ordinary
// FILE, and the explorer has always been able to open one. So the card lands on
// the page, and D269 brings back NONE of the machinery D262/D264 removed — no
// builder route, no `app` template, no `_mode`.
//
// The page is what the card is a picture of, which is the argument in one line:
// a card renders that page (live, or the `preview.png` standing in for it) and
// its title is read out of that page's <title>. Clicking a picture of a page and
// arriving at a file listing is the card not keeping its own promise — and the
// listing is one click away from the page (the crumb bar), where the page was
// several from the listing.
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
import { postAppOpen, type AppInfo } from "./api";
import { navigate, urlForFsPath } from "./router";

// The ONE ordering every app grid uses (/home's strip, the /apps hub):
// recently-OPENED desc; an app never opened falls back to its modified time,
// and one with neither sinks to the end. Name breaks ties so the order is
// stable. Lives here so the two surfaces can't drift.
export function sortApps(apps: AppInfo[]): AppInfo[] {
  const byName = (a: AppInfo, b: AppInfo) =>
    (a.title || a.name).localeCompare(b.title || b.name) || a.name.localeCompare(b.name);
  const recency = (a: AppInfo) => a.opened_at ?? a.updated_at ?? 0;
  return apps.slice().sort((a, b) => recency(b) - recency(a) || byName(a, b));
}

// Record the open in the app recents store — what `opened_at` (and so the
// sort above) is fed by. Fire-and-forget: recording must never delay or fail
// the navigation itself.
function recordAppOpen(app: AppInfo): void {
  void postAppOpen(app.path, app.title).catch(() => undefined);
}

// The file this card is about, tolerating a backend that predates `entry`.
// `entry` is "the file a card opens and previews"; `entry_html` is the narrower
// "that entry is a renderable page". They are the same file for a workspace app.
export function entryOf(app: AppInfo): string | null {
  return app.entry ?? app.entry_html;
}

// Deliberately NO `mode`: `navigate`'s options carry one, and a card must never
// set it (D262/D269 — the card opens the entry page, and a page opens in its own
// default view like any other file; there is no app mode to ask for and the user
// picks any other view from the switcher). A field nothing writes is an
// invitation, so the type does not offer it.
export interface OpenTarget {
  path: string;
  opts?: { isDir?: boolean };
}

// Where a card goes when activated, AS A VALUE. Split out from openApp so the
// rule can be tested without touching `navigate`: mocking a module that half
// the shell imports is process-wide in bun, and it leaks into whichever suite
// runs next. A pure function needs no mock at all.
export function openTargetFor(app: AppInfo): OpenTarget {
  // THE ENTRY, WHATEVER KIND IT IS — a page or a lone non-page file. Both are
  // files in the explorer, so the two branches this function used to have
  // collapse into one: `entryOf` already prefers `entry` and falls back to
  // `entry_html` for an older server, and the only thing the page/non-page
  // distinction still decides is who may point /render at it (api.ts), which is
  // not a question about where a click lands.
  //
  // `isDir: false` is carried rather than omitted: the hint rides in
  // history.state so the destination paints the right scaffold before its ~1.6s
  // stat resolves (router's navHintIsDir), and a card KNOWS its entry is a file.
  // Omitting it would be "unknown", i.e. a blanker frame for no reason.
  const entry = entryOf(app);
  if (entry) return { path: entry, opts: { isDir: false } };
  // Nothing to open but the folder — the app has no top-level page (or the
  // server could not resolve one). Its listing is the honest destination: it is
  // what the user would see anyway on arriving, and it is where they can find
  // whatever the folder does hold.
  return { path: app.path, opts: { isDir: true } };
}

// The URL for that target — what the anchor's href carries, so the browser can
// open it in a new tab without the shell's help.
//
// Nothing rides in the query string: the destination takes its own default view
// (the page's own template for an entry page, a folder's listing for the
// entry-less fallback), and the params navigate() would otherwise carry are
// in-session layout the user toggled, which a new tab does not inherit. Built
// through the router's own codec, so an app path with a space, a `#` or a
// non-ASCII name encodes exactly as in-app navigation encodes it.
//
// KNOWN GAP, recorded so it is not "fixed" wrongly: a Windows UNC path
// (`\\NAS\share\notes`) does not survive this. The
// codec normalizes backslashes for DRIVE-LETTER paths only, because on POSIX a
// backslash is a legal filename character. Adding an unconditional
// `replace(/\\/g, "/")` here would be worse than the gap: `urlForFsPath` strips
// leading slashes and `rootedFsPath` restores exactly one, so the UNC `\\` would
// come back as `/NAS/share/notes` — a different, silently wrong path. Real
// support means teaching BOTH directions of the codec about UNC, which no
// surface of the explorer has today (such a folder cannot be browsed or
// bookmarked either); until then a UNC app path is uniformly unsupported
// rather than supported in one place.
export function hrefFor(app: AppInfo): string {
  return urlForFsPath(openTargetFor(app).path);
}

export function openApp(app: AppInfo): void {
  recordAppOpen(app);
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
  if (e.defaultPrevented) return;
  if (isBrowserHandledClick(e)) {
    // The browser owns the navigation (new tab/window via the href), but the
    // open still happened — record it so the recency sort sees it too. No
    // preventDefault: recording rides alongside the browser's own handling.
    recordAppOpen(app);
    return;
  }
  e.preventDefault();
  openApp(app);
}
