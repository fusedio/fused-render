// Crumb bar, in three zones left to right:
//
//   path    ★ bookmark button, then the crumbs (or the editable path field)
//   mode    the `#topbar-mode-slot` portal target — Preview renders the view's
//           conditional primary action and the shared mode control into it
//   layout  a hairline rule, then split-right / split-down / `···`
//
// The Finder and split glyphs used to live INSIDE the crumb strip, welded to
// the last path segment: the path zone was not only the path, and "open in
// Finder" (rare) sat at the same weight as the splits (frequent). Reveal and
// "Copy path" moved into the `···` overflow; the splits form the layout group.
//
// Rendered by every view: path crumbs for listing/preview, a static label for
// the layout modes (LM-11 / TM-9 — ★/update still operate on currentUrl()).
import React, { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { requestCloneApp } from "@platform/cloud/cloneApp";
import { navigate, navigateUrl, currentUrl, IS_EMBED } from "@platform/lib/router";
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
import { encodePaneSegment, splitShellSearch } from "@platform/lib/layout-codec";
import { panelUrl } from "@apps/explorer/Panel";
import { SplitRightIcon, SplitDownIcon } from "@platform/ui/SplitIcons";
import { PathOverflow } from "@apps/explorer/BarMenu";
import { springDisarms } from "@apps/explorer/listing/drag-drop";
import { cameFromSelParam } from "@apps/explorer/listing/selection";
import {
  folderChromeClaimed,
  folderChromeSlot,
  subscribeFolderChrome,
} from "@apps/explorer/listing/folder-chrome";
import { publishTopbarSlot, retractTopbarSlot } from "@apps/explorer/topbar-slot";
import { registerSpring, SPRING_ATTR } from "@apps/explorer/listing/row-drag";

// How long a file drag has to hover a crumb before the listing follows it.
// Spring-loading is the standard file-manager answer to "the folder I want to
// drop into isn't on screen": hold over an ancestor and it opens, with the drag
// still in your hand.
//
// 700ms is the dwell that separates "hovering here" from "passing over on the
// way somewhere else" — a crumb strip is a dense row of small targets, and a
// shorter dwell navigates out from under a drag that was only crossing it.
const SPRING_LOAD_MS = 700;

// A crumb is spring-loaded NAVIGATION, never a drop target. Dropping ON a path
// segment would be a second, invisible way to move files — one that gives no
// listing to see the result in and no way to change your mind about which of
// several ancestors you meant. So a crumb carries `data-spring-target` and NOT
// the `data-fs-drop-path` a target declares itself with: the drag keeps its
// refused cursor over the strip, the listing navigates underneath, and the drop
// happens in the folder like any other.
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
  const springProps = (target: string) => ({ [SPRING_ATTR]: target });

  return { springProps, armedTarget };
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

// Split entry buttons + the path overflow: the bar's layout zone, in the same
// position in every state so the hand learns where layout lives — EXCEPT over
// a folder, where the listing underneath claims the zone and renders the `···`
// in its own search row instead (listing/folder-chrome.ts says which state we
// are in; Listing.tsx renders the other half). Nothing here for a folder at
// all: the splits are the pair that make least sense over a view that already
// IS a split, and with them gone the rule and the hairline would be a zone
// with nothing in it.
function LayoutZone({ fsPath }: { fsPath: string }) {
  const claimed = useSyncExternalStore(subscribeFolderChrome, folderChromeClaimed, () => false);
  if (claimed) return null;
  return (
    <>
      <span className="bar-rule" aria-hidden="true" />
      <div className="bar-zone">
        <button
          type="button"
          id="split-right-btn"
          className="bar-ctl bar-ctl-icon"
          title="Open this view in panel mode, split right"
          aria-label="Split right"
          onClick={() => enterPanel(fsPath, "row")}
        >
          <SplitRightIcon />
        </button>
        <button
          type="button"
          id="split-down-btn"
          className="bar-ctl bar-ctl-icon"
          title="Open this view in panel mode, split down"
          aria-label="Split down"
          onClick={() => enterPanel(fsPath, "col")}
        >
          <SplitDownIcon />
        </button>
        <PathOverflow fsPath={fsPath} />
      </div>
    </>
  );
}

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

// Split entry (LM-10): two panes side by side (`dir` "row", `,` in the codec)
// or stacked ("col", `;`), both showing the current view — entering split mode
// with a single pane looked like nothing happened. The current view's WHOLE
// query goes pane-local, inside each `_layout` segment (LM-3/D72): nothing is
// promoted to the top-level pool — global params exist only when the user
// hand-types them on the shell URL. Read via splitShellSearch, not raw
// URLSearchParams (D51): a stray `_layout=(…)` span carries literal `&` that
// would parse as junk keys; the codec read excludes the span, so it is
// dropped — the strict-read semantics.
function enterPanel(fsPath: string, dir: "row" | "col"): void {
  const { params } = splitShellSearch(location.search);
  const paneQ = params.toString();
  const seg = encodePaneSegment(fsPath, paneQ ? "?" + paneQ : "");
  navigateUrl(panelUrl(seg + (dir === "row" ? "," : ";") + seg, null));
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
    <div id="breadcrumb">
      <Breadcrumb {...props} />
    </div>
  );
  return slot ? createPortal(bar, slot) : bar;
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
  const crumbsRef = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState(false);
  const { springProps, armedTarget } = useSpringLoadedCrumbs();

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
      {...springProps(rootTarget)}
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
        <span key={target} className={cls} title={part}>
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
        // A click on the strip itself (whitespace right of the crumbs), not on
        // a crumb or the reveal button, switches to the editable path.
        <div
          className="crumbs"
          ref={crumbsRef}
          onWheel={onWheel}
          onClick={(e) => {
            if (e.target === e.currentTarget) setEditing(true);
          }}
        >
          {pieces}
        </div>
      )}
      <UpdateBookmarkButton />
      <TopbarActionsSlot />
      <LayoutZone fsPath={fsPath} />
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
