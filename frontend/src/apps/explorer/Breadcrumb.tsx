// Crumb bar, in three zones left to right:
//
//   path    ★ bookmark button, then the crumbs (or the editable path field)
//   mode    the `#topbar-mode-slot` portal target — Preview renders the view's
//           conditional primary action, the shared mode control and the preview
//           sidebar's toggle into it
//   search  over a FOLDER only: the listing's search row portals in here, so
//           its column has one header strip instead of two (search-slot.ts)
//
// The Finder and split glyphs used to live INSIDE the crumb strip, welded to
// the last path segment: the path zone was not only the path, and "open in
// Finder" (rare) sat at the same weight as the splits (frequent). They then
// separated — reveal/copy into a `···` overflow, the splits into a layout zone
// of their own at the bar's far right — converged again as NAMED ITEMS in one
// path `⋮` (see LayoutZone's epitaph below), and have now left the bar's markup
// altogether: they are items in its RIGHT-CLICK menu, which is the view's own
// (onBarContextMenu / topbar-menu.ts).
//
// Rendered by every view: path crumbs for listing/preview, a static label for
// the layout modes (LM-11 / TM-9 — ★/update still operate on currentUrl()).
import React, { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { requestCloneApp } from "@platform/cloud/cloneApp";
import { navigate, currentUrl, IS_EMBED } from "@platform/lib/router";
import { basename } from "@platform/lib/format";
import { isMod } from "@platform/lib/platform";
import {
  addBookmark,
  allBookmarks,
  deleteBookmark,
  updateBookmarkUrl,
  armBookmark,
  disarmBookmark,
  getArmedBookmark,
  sameSearch,
  splitBookmarkUrl,
} from "@platform/lib/bookmarks";
import { useUrlVersion, useBookmarksVersion, notifyBookmarksChanged } from "@platform/lib/hooks";
import { urlScheme, isCloudScheme, fileUrlToPath } from "@platform/lib/path-url";
import { resolveCloudUrl } from "@platform/lib/api";
import { pushToast } from "@platform/lib/toast";
import { openTopbarMenu } from "@apps/explorer/topbar-menu";
import { springDisarms } from "@apps/explorer/listing/drag-drop";
import { cameFromSelParam } from "@apps/explorer/listing/selection";
import {
  folderChromeClaimed,
  folderChromeSlot,
  subscribeFolderChrome,
} from "@apps/explorer/listing/folder-chrome";
import { publishTopbarSlot, retractTopbarSlot } from "@apps/explorer/topbar-slot";
import { publishSearchSlot, retractSearchSlot } from "@apps/explorer/search-slot";
import {
  refreshDropTarget,
  registerSpring,
  DROP_ANNOUNCE_ATTR,
  DROP_DIR_ATTR,
  DROP_PATH_ATTR,
  SPRING_ATTR,
} from "@apps/explorer/listing/row-drag";

// How long a file drag has to hover a crumb before the listing follows it.
// Spring-loading is the standard file-manager answer to "the folder I want to
// drop into isn't on screen": hold over an ancestor and it opens, with the drag
// still in your hand.
//
// 700ms is the dwell that separates "hovering here" from "passing over on the
// way somewhere else" — a crumb strip is a dense row of small targets, and a
// shorter dwell navigates out from under a drag that was only crossing it.
const SPRING_LOAD_MS = 700;

// A crumb is BOTH a spring-loaded navigation and a real drop target: hold and
// the ancestor opens with the drag still in your hand, release and the entries
// move into it.
//
// It used to be spring-load only — no `data-fs-drop-path` — on the argument that
// dropping ON a path segment gives no listing to see the result in and no way to
// choose between adjacent ancestors. The second half of that is answered by the
// per-crumb highlight every other drop target already gets (row-drag paints
// `drop-into` / `drop-reject` on whatever the pointer is over, from the same
// dropIsValid verdict), and the first by `data-fs-drop-announce`: the
// destination is off screen, so the move toasts, exactly as a drop onto a
// sidebar bookmark does.
//
// What the old arrangement actually produced was WORSE than either reading: with
// no drop host under the pointer, row-drag put `fs-drag-refused` on the body, so
// the strip wore the NO-DROP cursor for the whole hover — while hovering there
// was doing something useful — and a release did nothing at all. One hover, two
// meanings, painted as a rejection. The refusals are truthful now instead of
// universal: dropping on the crumb of the folder you are already in is
// "already-there" (drag-drop's dropIsValid, which has the crumb cases tested).
//
// `data-fs-drop-dir` is "1" with no stat probe — a path segment the listing is
// inside is a directory by construction.
//
// THE HARD PART IS NOT THE ATTRIBUTES, IT IS THAT THE SPRING-LOAD DESTROYS WHAT
// THE DROP DEPENDS ON. Navigating mid-drag replaces the hovered crumb (it becomes
// the current-folder crumb), re-lays out the strip under a pointer that has not
// moved, re-renders away the imperatively painted drop ring, and unmounts the
// listing whose registered mover the drop was going to use. A drag only
// re-resolves its target on pointer MOVEMENT, so none of that was noticed. Three
// things answer it together, and they only work as a set:
//
//   • every crumb is a drop target, the current-folder one included — that is the
//     crumb the pointer lands on after a spring-load (see dropProps);
//   • the strip tells the drag when it has changed, via row-drag's
//     refreshDropTarget, from an effect on fsPath and armedTarget — so the spot,
//     the cursor and the ring are rebuilt against the live DOM, and what is
//     painted is what a release will use, because both come from that one hit
//     test;
//   • row-drag resolves the performer as target listing → origin listing → the
//     listing that is actually on screen (its liveMover), because after a
//     spring-load the first two are gone.
//
// Enter and leave used to be the DOM's own `dragenter`/`dragleave`. The row
// drag is pointer-driven now (listing/row-drag.ts), so they arrive as calls
// instead — in the same order, deliberately, because the disarm rule below is
// written against that order.
function useSpringLoadedCrumbs() {
  // The crumb currently being dwelt on: its target path (for the armed
  // highlight) and the timer that will navigate there.
  const armed = useRef<{ target: string; timer: number } | null>(null);
  const [armedTarget, setArmedTarget] = useState<string | null>(null);

  const disarm = () => {
    if (armed.current) clearTimeout(armed.current.timer);
    armed.current = null;
    setArmedTarget(null);
  };

  // Cancel on unmount — including the unmount the navigation ITSELF causes, so
  // a fired timer can't leave a stale armed crumb behind in the new view — and
  // on `end`, which is the one end-of-drag every path reaches (a drop in the
  // listing, a release on nothing, a pointercancel, an Escape). A leave alone
  // would miss the drag that ENDS while the cursor is still over the crumb,
  // leaving a timer to navigate a second after the user let go.
  useEffect(() => {
    const off = registerSpring({
      enter: (target) => {
        if (armed.current?.target === target) return;
        disarm();
        armed.current = {
          target,
          timer: window.setTimeout(() => {
            armed.current = null;
            setArmedTarget(null);
            // The drag survives this: it is owned by a module, not by the
            // Listing the navigation is about to remount — which is exactly why
            // both the gesture (row-drag.ts) and the dragged entries
            // (drag-drop.ts) live outside the component tree.
            navigate(target, { isDir: true });
          }, SPRING_LOAD_MS),
        };
        setArmedTarget(target);
      },
      // Only the crumb that is actually armed may cancel it. Cancelling on any
      // leave reads as obviously right and disables the whole feature: the
      // crumb being ENTERED is entered before the one being left is left, so
      // dragging along the strip armed the new crumb and then immediately
      // killed it (see springDisarms, where the ordering is written down and
      // tested).
      leave: (target) => {
        if (springDisarms(target, armed.current?.target ?? null)) disarm();
      },
      end: () => disarm(),
    });
    return () => {
      off();
      disarm();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A crumb declares itself to the drag by attribute, so a crumb re-rendered
  // mid-drag (which the spring-load's own navigation guarantees) is still the
  // same target to a hit test that reads the DOM.
  //
  // EVERY crumb is a drop target, including the one for the folder currently
  // being listed. That last one is not decoration: after a spring-load the crumb
  // under the pointer IS the current-folder crumb, so leaving it out meant the
  // headline gesture — hold to open, release to move in — failed in its
  // commonest path, with the no-drop cursor on the crumb the user was aiming at
  // and a release that moved nothing. What it is NOT is a spring target: the
  // listing is already there, so navigating would be a pointless remount.
  // (Dropping the rows you are looking at into the folder they are already in is
  // refused as "already-there" by dropIsValid, as it is for the listing
  // background; after a spring-load the dragged rows come from a deeper folder,
  // so the same crumb is a real move.)
  const dropProps = (target: string) => ({
    [DROP_PATH_ATTR]: target,
    [DROP_DIR_ATTR]: "1",
    // Empty string, not "1": the attribute's PRESENCE is the signal
    // (row-drag reads hasAttribute), and "" keeps it out of the rendered value.
    [DROP_ANNOUNCE_ATTR]: "",
  });
  // Both declarations at once, in one spread, so an ancestor crumb can never be
  // one without the other (see the header: a spring target that is not a drop
  // target is what painted the refused cursor over the whole strip).
  const springProps = (target: string) => ({ [SPRING_ATTR]: target, ...dropProps(target) });

  return { springProps, dropProps, armedTarget };
}

// "Update bookmark" visibility (D38). The check has side effects (a pathname
// change or a deleted bookmark disarms permanently), so it runs in an effect,
// re-evaluated on every URL or bookmark-store change — the React equivalent
// of the vanilla syncUpdateButton() wired to fused:urlchange.
function useUpdateButton(urlVersion: number, bookmarksVersion: number): boolean {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    // Embed pages (layout panes included) share the tab's sessionStorage.
    // Their breadcrumb is hidden chrome (D39) — if this ran there, the pane's
    // /embed pathname would never match the armed url and the pathname check
    // below would permanently disarm the bookmark for the whole tab.
    if (IS_EMBED) return;

    const armed = getArmedBookmark();
    if (!armed) return setVisible(false);

    // allBookmarks(), not loadBookmarks(): the armed bookmark may live inside
    // a folder (D44) — the top-level list alone would misread it as deleted.
    const bookmark = allBookmarks().find((b) => b.id === armed.id);
    if (!bookmark) {
      disarmBookmark(); // bookmark deleted out from under us
      return setVisible(false);
    }

    const { pathname: armedPathname, search: armedSearch } = splitBookmarkUrl(armed.url);

    if (location.pathname !== armedPathname) {
      disarmBookmark(); // page change = permanent disarm
      return setVisible(false);
    }
    setVisible(!sameSearch(location.search, armedSearch));
  }, [urlVersion, bookmarksVersion]);
  return visible;
}

// 14px, not the bars' usual 16px: this glyph's neighbour is 12px monospace
// crumb text, not another control, and at 16px it read as an oversized
// ornament rather than a sibling of the path. The button keeps its 24px box
// (padding absorbs the 2px), so the hit area and the hover pill are unchanged.
// The remaining half-pixel of optical correction — a five-pointed star's mass
// sits below its box centre — is a CSS nudge on .bookmark-star-btn svg.
function StarIcon({ filled }: { filled: boolean }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill={filled ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinejoin="round"
    >
      <path d="M8 1.8l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.6l-3.8 2 .7-4.3-3.1-3 4.3-.6z" />
    </svg>
  );
}

// Portal target for the FOLDER view's search row, at the bar's right end.
//
// Rendered only while a folder holds the chrome claim: a file view's bar has
// no search box, and an empty div would still eat the bar's `gap`. The listing
// portals its own `.listing-search` in here — box, sort chip and the path
// `···` — so the left column has ONE strip, matching the preview pane's one
// strip across the divider (search-slot.ts).
function FolderSearchSlot() {
  const claimed = useSyncExternalStore(subscribeFolderChrome, folderChromeClaimed, () => false);
  const ref = useRef<HTMLDivElement>(null);
  // A layout effect, and the cleanup is identity-checked (node-slot.ts): the
  // bar's own relocation into the listing column rebuilds this node, and the
  // outgoing one is retracted after the incoming one has published.
  useLayoutEffect(() => {
    const el = ref.current;
    if (el) publishSearchSlot(el);
    return () => retractSearchSlot(el);
  }, [claimed]);
  if (!claimed) return null;
  return <div className="crumb-search-slot" ref={ref} />;
}

// The bar's LAYOUT ZONE is gone, and with it the hairline that made it read as
// a zone. It held exactly two controls — split-right and split-down, for a file
// preview only (a folder never had them: the splits make least sense over a view
// that already IS a split) — as unlabelled filled-rectangle glyphs pinned to the
// far right of the window, an inch of empty bar away from the path they act on.
// Both are named items in the bar's right-click menu now, beside "Copy Path"
// and "Reveal in Finder" — first in a path `⋮`, then (with the button itself)
// in the menu the view publishes for this bar. They still call `enterPanel`
// (lib/split-actions.ts, lifted out of this file once the listing's own `⋮`
// became a second caller).
//
// Which state we are in — file or folder — is no longer this file's question at
// all: whichever view owns the bar publishes its own menu (topbar-menu.ts), so
// the folder/file difference lives with the folder and the file.

// A pending copy/cut is shown ONLY on the affected rows (Listing.tsx marks them
// with a badge and edge bar). There is deliberately no global chip here: the
// chrome-level readout lingered in the corner with no way to dismiss it, and
// the row marking is where the user is already looking.

// ★ bookmark button, leftmost in the bar. Filled (foreground, not accent —
// see explorer.css) only when a bookmark matches the current view exactly
// (same pathname AND
// same params, via sameSearch) — a param change empties the star, so the user
// can save the changed view as a new bookmark. Clicking a filled star deletes
// the matching bookmark (toggle), disarming it if it was armed. `name` is the
// default bookmark name.
//
// Exported for Panel: panel mode dropped its 48px "Panel" title row, and the
// star (which bookmarks the whole `_layout` URL) moved into the pane bars.
// Every pane renders one and they all reflect the same layout bookmark —
// deliberately, since they all describe the same URL.
//
// Which is exactly why `id` is a prop and not baked in: the bar renders ONE
// star and passes the id, a split panel renders one per pane and passes none.
// A hardcoded id would emit duplicate `#bookmark-btn` nodes in a split.
export function BookmarkStar({ name, id }: { name: string; id?: string }) {
  useUrlVersion();
  useBookmarksVersion();
  const matchesCurrent = (b: { url: string }) => {
    const { pathname, search } = splitBookmarkUrl(b.url);
    return pathname === location.pathname && sameSearch(search, location.search);
  };
  const existing = allBookmarks().find(matchesCurrent);
  const starred = existing !== undefined;

  const onBookmark = async () => {
    if (existing) {
      await deleteBookmark(existing.id);
      const armed = getArmedBookmark();
      if (armed && armed.id === existing.id) disarmBookmark();
    } else {
      await addBookmark(name, currentUrl());
    }
    notifyBookmarksChanged();
  };

  return (
    <button
      id={id}
      className={"bookmark-star-btn" + (starred ? " active" : "")}
      title={starred ? "Remove bookmark" : "Bookmark this view"}
      onClick={onBookmark}
    >
      <StarIcon filled={starred} />
    </button>
  );
}

// "Update bookmark" text button, after the crumbs strip, before the actions
// slot. Visible only when the armed bookmark's params have drifted (D38).
// Exported for Panel, which lost the title row that used to carry it — it
// renders in the FIRST pane's bar only (unlike the star, this one is wide
// enough that one per pane would be noise).
export function UpdateBookmarkButton() {
  const urlVersion = useUrlVersion();
  const bookmarksVersion = useBookmarksVersion();
  const showUpdate = useUpdateButton(urlVersion, bookmarksVersion);
  if (!showUpdate) return null;

  const onUpdate = async () => {
    const armed = getArmedBookmark();
    if (!armed) return;
    const url = currentUrl();
    await updateBookmarkUrl(armed.id, url);
    armBookmark(armed.id, url); // re-arm against the newly saved url
    notifyBookmarksChanged();
  };

  return (
    <button
      id="update-bookmark-btn"
      className="star-btn starred"
      title="Update bookmark to current params"
      onClick={onUpdate}
    >
      Update bookmark
    </button>
  );
}

// Portal target for the view's header actions (mode switcher, deploy, "Open as
// app") — Preview renders into this via TopbarActions. Carries
// .preview-actions so the existing switcher/button styling applies unchanged.
// The node is published rather than looked up by id, because the bar relocates
// under a folder view and the lookup would go stale (topbar-slot.ts).
function TopbarActionsSlot() {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el) publishTopbarSlot(el);
    return () => retractTopbarSlot(el);
  }, []);
  return <div ref={ref} id="topbar-mode-slot" className="crumb-actions preview-actions" />;
}

// Open a URL typed/pasted into the path bar. Every failure is an error toast
// carrying the reason — the path bar has already closed by now, so a silent
// no-op would read as "Enter did nothing". A cloud URL keeps its trailing
// slash (that is a prefix the mount may cover, not path noise); the server
// strips it when it resolves.
async function openUrl(url: string, scheme: string): Promise<void> {
  if (scheme === "file") {
    try {
      navigate(fileUrlToPath(url));
    } catch (e) {
      pushToast({ msg: (e as Error).message, tone: "error" });
    }
    return;
  }
  if (isCloudScheme(scheme)) {
    try {
      const { path } = await resolveCloudUrl(url);
      navigate(path);
    } catch (e) {
      // The server's message names what's missing ("no mount covers
      // s3://<bucket> — add one from the Mounts page in the sidebar").
      pushToast({ msg: (e as Error).message, tone: "error" });
    }
    return;
  }
  if (scheme === "https") {
    // A deployed Fused Render page (SPEC §35). The path bar is where a user naturally
    // pastes a link someone sent them, and "Can't open https:// URLs in the explorer" was
    // both true and useless. The flow's own confirm step vets the URL and reports why if it
    // is not a clonable page, so this hands the link over rather than pre-judging it here —
    // one place decides what a clone URL is.
    requestCloneApp(url);
    return;
  }
  pushToast({ msg: `Can't open ${scheme}:// URLs in the explorer`, tone: "error" });
}

// The crumb bar AND its box, in whichever column it belongs to.
//
// Normally that is shell level: `#breadcrumb` is a child of `#main`, above
// `#content`, spanning the window. Over a FOLDER the listing publishes a slot
// in its own left column (listing/folder-chrome.ts) and the whole bar portals
// into it — so the bar ends at the split divider and the preview pane on the
// right starts at the very top of the window, its own header the first thing
// in the column. Nothing about the bar's markup or styling changes; only where
// it hangs.
//
// A portal, not a prop, for the reason the claim store exists at all: whether
// the content resolves to a listing is decided several levels below the bar,
// after this component has rendered.
export function BreadcrumbBar(props: {
  fsPath: string;
  home?: string;
  renderedTitle?: string | null;
}) {
  const slot = useSyncExternalStore(subscribeFolderChrome, folderChromeSlot, () => null);
  const bar = (
    <div id="breadcrumb" onContextMenu={onBarContextMenu}>
      <Breadcrumb {...props} />
    </div>
  );
  return slot ? createPortal(bar, slot) : bar;
}

// RIGHT-CLICK ANYWHERE ON THE BAR — the ★, the crumbs, the free space, the
// search area — opens the menu of the view underneath it, at the cursor: the
// folder's own actions over a listing, the open file's over a preview. The bar
// does not build either list; it asks whoever owns it (topbar-menu.ts, where the
// argument for that is written down).
//
// Which is also why this is attached HERE, on the bar's box, rather than on the
// pieces inside it: the whole strip answers, including the parts that are not
// the path, so there is nowhere on it that a right-click does nothing.
//
// TEXT FIELDS KEEP THE NATIVE MENU. The path field and the search box are the
// two places on this bar where the browser's own copy/paste/spelling menu is the
// useful one, and replacing it with a menu of file actions would take away the
// only way to paste a path with the mouse.
//
// With no owner (a static label bar, a view still statting) the event is left
// alone rather than swallowed: the platform menu is a better answer than none.
function onBarContextMenu(e: React.MouseEvent): void {
  const target = e.target as HTMLElement | null;
  if (target?.closest("input, textarea")) return;
  if (!openTopbarMenu(e.clientX, e.clientY)) return;
  e.preventDefault();
}

// The bar's DEAD SPACE is the path field's target, the way a browser's location
// bar works: the strip's own whitespace, the vertical padding above and below the
// crumbs, and the gap between the path and the search box (which the search row's
// auto margin took over when it moved up here — see .crumb-search-slot in
// explorer.css, where that loss of reach is noted).
//
// Everything that is its own control is excluded: the ★, the crumb links, the
// kebabs, the search box and the mode/actions cluster (`.crumb-actions`, which
// also covers the mode dropdown's popup, a DOM child of the bar). A click on
// those is a click on them.
//
// Listed as a selector tested with `closest` rather than as "is this the bar
// itself?" because the dead space is not one element: it is the bar's own
// background, the strip's padding, and the empty parts of the containers that
// portal into it.
const BAR_EDIT_EXCLUDE =
  "a, button, input, textarea, select, .crumb-actions, .crumb-search-slot," +
  " .listing-search, .bar-overflow, .bar-menu-popup, .context-menu";

function barClickEntersEdit(target: HTMLElement | null): boolean {
  if (!target) return false;
  // Only this bar's own clicks — the listener is document-level (see the effect).
  if (!target.closest("#breadcrumb")) return false;
  return target.closest(BAR_EDIT_EXCLUDE) === null;
}

export function Breadcrumb({
  fsPath,
  home,
  renderedTitle,
}: {
  fsPath: string;
  home?: string;
  // The previewed page's own <title>, when known (see StatView) — preferred
  // over the file's basename for the default bookmark name (and, via
  // Recents, for its sidebar row) so "My DB app" beats "index.html".
  renderedTitle?: string | null;
}) {
  const crumbsRef = useRef<HTMLDivElement | null>(null);
  const [editing, setEditing] = useState(false);
  // Read by the always-on click-away listener, which must not rebind on every toggle.
  const editingRef = useRef(false);
  editingRef.current = editing;
  // Set by the press that dismissed the path field, read (and cleared) by the
  // click that follows it — the two always-on listeners below share it.
  const closedByClickAwayRef = useRef(false);
  const { springProps, dropProps, armedTarget } = useSpringLoadedCrumbs();

  // Keep the tail of a long path in view on every path change (same as the
  // panel path bar, Panel.tsx). The strip hides its scrollbar (shell.css), so
  // without this the current folder could sit scrolled off the right edge.
  // Also re-pins when edit mode closes: the strip remounts fresh (scrollLeft
  // 0) after the input swap, with fsPath unchanged; the effect no-ops while
  // editing (ref is null).
  useEffect(() => {
    const el = crumbsRef.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [fsPath, editing]);

  // THE STRIP CHANGED UNDER A POINTER THAT DID NOT MOVE — tell the drag.
  //
  // row-drag only re-resolves its target on pointer MOVEMENT, and a spring-load is
  // the one moment when the DOM moves instead: the hovered crumb is replaced by the
  // current-folder one, the strip re-lays out, and the armed highlight's re-render
  // rewrites `className` over the drop ring row-drag painted imperatively. Left
  // unsaid, the drag holds a spot pointing at a detached element, keeps the no-drop
  // cursor, and releases into whatever a fresh hit test happens to find — which is
  // a silent wrong destination. refreshDropTarget no-ops off-drag, so both hooks
  // below cost a boolean check the rest of the time.
  //
  // THE RENDER IS NOT ENOUGH ON ITS OWN, which is why the observer exists. Chrome
  // portals into the crumb bar ASYNCHRONOUSLY after a navigation — Preview's topbar
  // slot, and the open-as-app button, which only appears once the new listing's
  // /api/fs/list has resolved and reported a single app. Each one narrows the strip
  // and re-triggers the tail pin above, sliding crumbs under a cursor that never
  // moved, with no dependency here changing and no render of this component at all.
  // A window resize does the same. So the geometry itself is watched rather than
  // the React inputs that were supposed to predict it: a ResizeObserver for the
  // width, and `scroll` for the tail pin's own scrollLeft write.
  useEffect(() => {
    const el = crumbsRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => refreshDropTarget());
    observer.observe(el);
    el.addEventListener("scroll", refreshDropTarget);
    return () => {
      observer.disconnect();
      el.removeEventListener("scroll", refreshDropTarget);
    };
    // `editing` swaps the strip for the path input and back, so the element this
    // is bound to is a different node either side of it.
  }, [editing]);

  // The synchronous half, kept as well as the observer: replacing the hovered
  // crumb with a same-width one changes what is under the pointer without
  // resizing anything, and `armedTarget` is the className rewrite, which changes
  // no geometry at all. `editing` is here for the same reason it is on the pin.
  useEffect(() => {
    refreshDropTarget();
  }, [fsPath, armedTarget, editing]);

  // CLICK-TO-EDIT, over the whole bar. It used to stop at the crumb strip's own
  // box, on the argument that "a click meant for the chrome kept turning into an
  // open path field" — but the chrome is all real controls, every one of them
  // excluded by name (BAR_EDIT_EXCLUDE), and what the narrow target actually cost
  // was the obvious gesture: clicking the empty stretch of a path bar to type a
  // path. The strip stopped growing when the search row moved into the bar
  // (explorer.css), so most of that empty stretch is not even the strip's any
  // more.
  //
  // Document-level, because the box this fires for — `#breadcrumb` — is rendered
  // by BreadcrumbBar ABOVE this component, so a React handler here would never
  // see a click that landed on the bar's own background. One listener for the
  // whole bar, in place of the strip's own onClick.
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (e.button !== 0) return; // left click only
      // The pointerdown that closed the field is followed by this click; without
      // the guard, clicking the bar's background to DISMISS the field reopened
      // it, discarding whatever had been typed.
      if (closedByClickAwayRef.current) return;
      if (editingRef.current) return;
      if (!barClickEntersEdit(e.target as HTMLElement | null)) return;
      setEditing(true);
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);

  // Click-away, closing the path field. `onBlur` alone is not enough: whether
  // a mouse-down on non-focusable chrome (the bar's own background, a gap in
  // the search row) moves focus at all is browser-dependent, so the field could
  // sit there open and focused-looking under a click that clearly landed
  // outside it. A pointerdown listener answers the question directly.
  //
  // The `···` is the one exception: it belongs to the path, and its button
  // deliberately doesn't take focus (BarMenu.tsx), so its menu opens over an
  // open field rather than closing it out from under the pointer.
  //
  // Bound once for the bar's lifetime, not per edit session (editingRef keeps
  // it reading fresh state without rebinding).
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      // Cleared on every press, set only by the close below: the click that
      // follows a dismissing press must not be read as "enter edit mode" (see
      // the click-to-edit effect above). Pointerdown always precedes its click.
      closedByClickAwayRef.current = false;
      if (!editingRef.current) return;
      const t = e.target as HTMLElement | null;
      if (!t) return;
      if (t.classList?.contains("crumb-edit")) return;
      if (t.closest?.(".bar-overflow, .bar-menu-popup")) return;
      setEditing(false);
      closedByClickAwayRef.current = true;
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, []);

  // Ctrl/Cmd+L jumps into the editable path (like a browser's location bar).
  // Skip when focus is already in a text field so it never hijacks typing.
  // NOTE: Chrome/Firefox route Ctrl/Cmd+L to their own address bar before the
  // page sees it, so this only lands in app-mode/standalone windows (D: see
  // plan). Registered document-level, cleaned up on unmount (Listing.tsx).
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!isMod(e) || e.key.toLowerCase() !== "l") return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      e.preventDefault();
      setEditing(true);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  // Map a plain mouse wheel's vertical delta onto horizontal scroll so the
  // scrollbar-less strip is still wheel-scrollable (touchpad horizontal pans
  // already work via the native overflow-x).
  const onWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (e.deltaY === 0) return;
    e.currentTarget.scrollLeft += e.deltaY;
  };

  // Strictly below home only — home itself shows its full path, not a lone "~".
  const underHome = home !== undefined && fsPath.startsWith(home + "/");
  const rest = underHome ? fsPath.slice(home.length) : fsPath;
  const parts = rest.split("/").filter((s) => s.length > 0);

  // Edit mode seeds the same "~"-contracted path the crumbs display; Enter
  // expands a leading "~" back to the real home before navigating.
  const displayPath = underHome ? "~" + rest : fsPath;
  const submitEdit = (raw: string) => {
    let path = raw.trim();
    // A pasted URL, not a path. Handled before any path munging — "~"
    // expansion and trailing-slash trimming are path grammar, and a URL's
    // trailing slash is part of the key (see openUrl).
    const scheme = urlScheme(path);
    if (scheme) {
      setEditing(false);
      void openUrl(path, scheme);
      return;
    }
    if (home !== undefined) {
      if (path === "~") path = home;
      else if (path.startsWith("~/")) path = home + path.slice(1);
    }
    if (path.length > 1) path = path.replace(/\/+$/, ""); // drop trailing slash, keep lone "/"
    setEditing(false);
    // No isDir hint — a typed path's kind is unknown; the destination view's
    // stat/error handling covers a bad path (see plan: no pre-validation).
    if (path) navigate(path);
  };
  const rootTarget = underHome ? (home as string) : "/";
  const pieces: React.ReactNode[] = [
    <a
      key="root"
      href="#"
      className={
        "path-crumb" +
        (parts.length === 0 ? " last" : "") +
        (armedTarget === rootTarget ? " spring-armed" : "")
      }
      // Drop-only when the listing is already AT the root: this crumb is then the
      // current folder, and springing into the folder you are looking at would be
      // a pointless remount. An ancestor gets both roles.
      {...(parts.length === 0 ? dropProps(rootTarget) : springProps(rootTarget))}
      onClick={(e) => {
        e.preventDefault();
        // A top-bar hop always lands on the plain listing: `_mode` is dropped
        // (a full-screen `claude` folder view reopening as another folder's
        // `claude` view read as "nothing happened"). navigate() still carries
        // the sticky `preview` onto directory targets, so pane visibility
        // survives the hop. Breadcrumb targets are always dirs.
        //
        // `sel` lands the ancestor with the child we came out of highlighted
        // and scrolled to — the file-manager rule, shared with the keyboard's
        // go-up chord (listing/useListingShortcuts) through one pure decision.
        const target = underHome ? (home as string) : "/";
        navigate(target, { isDir: true, sel: cameFromSelParam(target, fsPath) });
      }}
    >
      {underHome ? "~" : "/"}
    </a>,
  ];
  // A Windows path's first segment is the drive ("C:"); its crumb must target
  // "C:/" (bare "C:" is cwd-relative to os.stat) and later segments append
  // without re-rooting at "/".
  const isDrive = !underHome && /^[A-Za-z]:$/.test(parts[0] || "");
  let acc = underHome ? home : "";
  parts.forEach((part, i) => {
    if (i === 0 && isDrive) acc = part + "/";
    else acc = acc + (acc.endsWith("/") ? "" : "/") + part;
    const target = acc;
    const isLast = i === parts.length - 1;
    // GNOME path-bar compression (nautilus-pathbar.c): a crumb may flex-shrink
    // (ellipsizing under space pressure, .shrink in shell.css) only when its
    // name is longer than 1.5x its min-width floor — 7ch for ancestors, 4x
    // that for the current dir so the tail compresses last. Shorter names
    // never shrink; the floors guarantee real overflow, so the strip scrolls
    // once everything is compressed.
    const shrink = part.length > (isLast ? 28 : 7) * 1.5;
    const cls = "path-crumb" + (isLast ? " last" : "") + (shrink ? " shrink" : "");
    // Separator only between segments (root already carries the leading
    // slash) — matches the panel path bar's tight `/Users/name/...` format.
    // The "~" crumb carries no slash, so its first segment needs one too.
    if (i > 0 || underHome) pieces.push(<span key={"sep" + i} className="path-crumb-sep">/</span>);
    if (isLast) {
      pieces.push(
        // Not a link (you are already here) and not a spring target, but a DROP
        // target like every other crumb — after a spring-load this is the crumb
        // the pointer is sitting on, so it is the one that has to accept the
        // release (see dropProps).
        <span key={target} className={cls} title={part} {...dropProps(target)}>
          {part}
        </span>
      );
    } else {
      pieces.push(
        <a
          key={target}
          href="#"
          className={cls + (armedTarget === target ? " spring-armed" : "")}
          title={part}
          {...springProps(target)}
          onClick={(e) => {
            e.preventDefault();
            // Plain listing, no `_mode`; `sel` highlights the child we came
            // out of (see the root crumb above).
            navigate(target, { isDir: true, sel: cameFromSelParam(target, fsPath) });
          }}
        >
          {part}
        </a>
      );
    }
  });

  return (
    <>
      <BookmarkStar id="bookmark-btn" name={renderedTitle || basename(fsPath)} />
      {editing ? (
        <input
          className="crumb-edit"
          defaultValue={displayPath}
          spellCheck={false}
          autoFocus
          onFocus={(e) => e.target.select()}
          onKeyDown={(e) => {
            if (e.key !== "Enter" && e.key !== "Escape") return;
            e.preventDefault();
            // Both keys are ours alone. Without this the document-level
            // listing handler still sees them: committing unmounts this input
            // synchronously (React flushes a discrete update before the
            // keydown finishes bubbling), focus falls back to <body>, and the
            // listing reads that as "Enter with nothing focused" — opening the
            // lead row on top of wherever the typed path went. Escape would
            // likewise clear the selection behind us.
            e.stopPropagation();
            if (e.key === "Enter") submitEdit(e.currentTarget.value);
            else setEditing(false); // discard, no navigation
          }}
          onBlur={() => setEditing(false)} // a stray click cancels rather than commits
        />
      ) : (
        // Click-to-edit — the "/" separators, the current folder's own (unlinked)
        // crumb, the strip's whitespace and the bar's dead space around it — is
        // the document-level listener above, not an onClick here: the bar's own
        // background is rendered a level up (BreadcrumbBar) and a handler on this
        // div could never see it. One rule for the whole bar, one exclusion list.
        <div className="crumbs" ref={crumbsRef} onWheel={onWheel}>
          {pieces}
        </div>
      )}
      {/* THE PATH `⋮` IS GONE, both of them. Over a FOLDER the listing took its
          actions into the right end of its own column header (Listing.tsx), where
          they sit with the folder's other operations instead of being split
          between a path menu and a right-click. Over a FILE, which is where this
          bar kept one — "Open in Finder", "Copy path", the two splits — the whole
          set is now a right-click on the bar (onBarContextMenu above), together
          with the two things the button never offered: renaming the open file and
          handing it to Claude Code. A menu with no button is a menu nobody finds,
          which is the argument the `⋮` was kept on; what settled it is that the
          bar had no right-click AT ALL, so the gesture people try first did
          nothing, and a four-item dropdown was standing in for it three pixels
          from the path. The path field's own affordance is the same bet. */}
      <UpdateBookmarkButton />
      <TopbarActionsSlot />
      <FolderSearchSlot />
    </>
  );
}

export function StaticBreadcrumb({ label }: { label: string }) {
  return (
    <>
      <BookmarkStar id="bookmark-btn" name={label} />
      <div className="crumbs">
        <span className="current">{label}</span>
      </div>
      <UpdateBookmarkButton />
      <TopbarActionsSlot />
    </>
  );
}
