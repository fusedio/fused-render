// The file-explorer walkthrough — replay only (`autoStart: false`). It was the
// whole first-run tour once; the three surface tours own that moment now, and
// this is what is left of it: four controls a plain folder view always has,
// walked left to right along its own crumb bar and then across to the pane.
//
// Every step is one short phrase. A replay is read in a second or skipped, so
// the copy names the control's job with a concrete verb and stops there — the
// long explanations the older steps carried (split panes, right-click menus)
// were paragraphs nobody finishes on a tour they asked to see again.
//
// Two steps LEFT this file rather than being shortened. "Side by side" described
// the pane as a preview split, which it is not any more (listing/pane-side.ts:
// the column is a companion, not a second copy of the row), and
// `.listing-head-menu` no longer exists in any markup — its actions are the
// bar's right-click menu now (topbar-menu.ts), so the step was pointing at
// nothing and presentSteps was silently dropping it.
import type { Tour } from "./registry";

export const explorerTour: Tour = {
  id: "explorer",
  title: "File explorer",
  // FOLDER VIEWS only, not "/explorer" itself: that route is the launcher page
  // (recents/sessions/repos) with none of this tour's chrome, and a matches()
  // that claimed it made a replay asked for there find no targets and silently
  // do nothing.
  matches: (pathname) => pathname.startsWith("/explorer/view/"),
  // A REAL folder — the user's home directory, asked of the server, since no
  // fixed path exists to write down. /explorer can't be the start: see above.
  // Imported dynamically INSIDE the function: the api module (and the router,
  // which reads location at module init) must not ride along into every
  // DOM-free import of the registry — this file's whole layer is loadable in a
  // bare test runtime, and only this one code path may fetch.
  startPath: async () => {
    const { getConfig } = await import("@platform/lib/api");
    // The router's own codec, not an inlined split-on-"/": config's `home` is
    // raw os.path.expanduser("~"), backslash-separated on Windows, and
    // viewUrlForFsPath is where that is already handled.
    const { viewUrlForFsPath } = await import("@platform/lib/router");
    return viewUrlForFsPath((await getConfig()).home);
  },
  autoStart: false,
  steps: () => [
    // The bar's own box, not `.crumbs` inside it: over a folder the bar portals
    // into the listing's column (listing/folder-chrome.ts) and carries the
    // arrows, the star and the search row with it, so `#breadcrumb` is the strip
    // the next two steps are both inside of.
    {
      element: "#breadcrumb",
      popover: {
        title: "Your location",
        description: "Every view lives in the URL.",
      },
    },
    // The ★ at the tail of the path (Breadcrumb.tsx's BookmarkStar, which takes
    // the id as a prop so a split panel's per-pane copies stay anonymous — this
    // is the one that is rendered with it).
    {
      element: "#bookmark-btn",
      popover: {
        title: "Bookmark",
        description: "Bookmark this view for one-click return.",
      },
    },
    {
      element: ".listing-search",
      popover: {
        title: "Search",
        description: "Search files in this folder and below.",
      },
    },
    // The pane's mode pill, in the pane's own header strip
    // (ListingPreviewPane.tsx). Scoped to `.pane-header` because `.mode-menu` is
    // the shared switcher chip (BarMenu.tsx) a file preview also uses in the
    // crumb bar; over a folder that bar's mode slot is empty and this is the only
    // one on screen. An absent `_side` means the pane is OPEN on the leading
    // companion — Claude — so this is what a plain folder view shows; a pane the
    // user has shut has no header at all and presentSteps drops the step.
    {
      element: ".pane-header .mode-menu",
      popover: {
        title: "Ask AI",
        description: "Ask AI to do something with these files.",
      },
    },
  ],
};
