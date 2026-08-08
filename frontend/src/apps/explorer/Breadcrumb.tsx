// Crumb bar, in three zones left to right:
//
//   path    the "Open sidebar" button (only while the sidebar is collapsed),
//           the ★ bookmark button, then the crumbs (or the editable path field)
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
import React, { useEffect, useRef, useState } from "react";
import { requestCloneApp } from "@platform/cloud/cloneApp";
import { navigate, navigateUrl, urlForFsPath, currentUrl, IS_EMBED } from "@platform/lib/router";
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
import {
  useUrlVersion,
  useBookmarksVersion,
  notifyBookmarksChanged,
  useSidebarState,
  useOwnsSidebarToggle,
} from "@platform/lib/hooks";
import { toggleSidebarCollapsed } from "@platform/lib/sidebarstate";
import { urlScheme, isCloudScheme, fileUrlToPath } from "@platform/lib/path-url";
import { resolveCloudUrl } from "@platform/lib/api";
import { pushToast } from "@platform/lib/toast";
import { copyToClipboard } from "@platform/lib/clipboard";
import { encodePaneSegment, splitShellSearch } from "@platform/lib/layout-codec";
import { panelUrl } from "@apps/explorer/Panel";
import { SplitRightIcon, SplitDownIcon } from "@platform/ui/SplitIcons";
import { OverflowMenu } from "@apps/explorer/BarMenu";

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

// Sidebar-panel glyph: the frame with its left rail, the standard "there is a
// panel over there" mark. Drawn inline like every other topbar icon.
function SidebarPanelIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M9 4v16" />
    </svg>
  );
}

// Leftmost control in the path zone, and ONLY while the sidebar is collapsed:
// the sidebar's own collapse button is gone with it, so this is what brings it
// back. It replaces the floating bubble that used to hang off the collapsed
// strip half-off-screen. Never in an embed (or a panel pane bar, which renders
// its own chrome and has no sidebar to open).
function OpenSidebarButton() {
  const { collapsed } = useSidebarState();
  // Claim the reopen control for this route for as long as this bar is
  // mounted — not just while collapsed — so SidebarFrame's collapsed strip
  // never renders a competing one here. Embeds have no sidebar at all, so
  // they claim nothing.
  useOwnsSidebarToggle(!IS_EMBED);
  if (IS_EMBED || !collapsed) return null;
  return (
    <button
      type="button"
      className="bar-ctl bar-ctl-icon"
      aria-label="Open sidebar"
      title="Open sidebar"
      onClick={toggleSidebarCollapsed}
    >
      <SidebarPanelIcon />
    </button>
  );
}

// Browsers block file:// navigation from http pages, so revealing in the OS
// file manager goes through the server (POST /api/fs/reveal). X-Fused forces
// a CORS preflight so a foreign page can't fire this blind (D3 guard).
function revealInFileManager(path: string): void {
  fetch("/api/fs/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Fused": "1" },
    body: JSON.stringify({ path }),
  });
}

const FILE_MANAGER = navigator.userAgent.includes("Windows") ? "File Explorer" : "Finder";

// The bar's low-frequency one-shot actions. Both used to be (or wanted to be)
// glyphs in the crumb strip; neither is worth a permanent slot beside the
// splits, which is exactly what an overflow menu is for.
function LayoutOverflow({ fsPath }: { fsPath: string }) {
  const copyPath = async () => {
    if (await copyToClipboard(fsPath)) pushToast({ msg: "Path copied", tone: "info" });
    else pushToast({ msg: "Couldn't copy the path", tone: "error" });
  };
  return (
    <OverflowMenu
      items={[
        { label: "Open in " + FILE_MANAGER, onClick: () => revealInFileManager(fsPath) },
        { label: "Copy path", onClick: () => void copyPath() },
      ]}
    />
  );
}

// Split entry buttons + the overflow: the bar's layout zone, in the same
// position in every state so the hand learns where layout lives.
function LayoutZone({ fsPath }: { fsPath: string }) {
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
        <LayoutOverflow fsPath={fsPath} />
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
function TopbarActionsSlot() {
  return <div id="topbar-mode-slot" className="crumb-actions preview-actions" />;
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

// Carry the active `_mode` (e.g. a folder viewed as "graph") across top-bar
// navigation so moving between folders preserves the chosen view. Other query
// params are dropped — a fresh path is a fresh view — except the listing's
// sticky `preview` (pane visibility), which navigate() itself carries for
// directory targets; the `_mode` branch here must carry it the same way or a
// breadcrumb hop out of a moded folder would silently close the pane. An
// unknown `_mode` on the target silently falls back to its default
// (Preview.activeTemplate).
function navigatePreservingMode(target: string): void {
  const url = new URLSearchParams(location.search);
  const mode = url.get("_mode");
  if (mode) {
    const params = new URLSearchParams({ _mode: mode });
    const preview = url.get("preview");
    if (preview !== null) {
      params.set("preview", preview);
      const panelMode = url.get("_panelMode");
      if (panelMode !== null) params.set("_panelMode", panelMode);
    }
    navigateUrl(urlForFsPath(target, "?" + params.toString()));
  } else {
    navigate(target, { isDir: true }); // breadcrumb targets are always dirs
  }
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
  const pieces: React.ReactNode[] = [
    <a
      key="root"
      href="#"
      className={"path-crumb" + (parts.length === 0 ? " last" : "")}
      onClick={(e) => {
        e.preventDefault();
        navigatePreservingMode(underHome ? home : "/");
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
          className={cls}
          title={part}
          onClick={(e) => {
            e.preventDefault();
            navigatePreservingMode(target);
          }}
        >
          {part}
        </a>
      );
    }
  });

  return (
    <>
      <OpenSidebarButton />
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
      <OpenSidebarButton />
      <BookmarkStar id="bookmark-btn" name={label} />
      <div className="crumbs">
        <span className="current">{label}</span>
      </div>
      <UpdateBookmarkButton />
      <TopbarActionsSlot />
    </>
  );
}
