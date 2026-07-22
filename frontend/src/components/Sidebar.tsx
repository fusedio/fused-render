// Sidebar UI: brand, Fused-dir entry, bookmark rows with hover card + inline rename.
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { navigate, navigateUrl, currentUrl, rootedFsPath, VIEW_PREFIX } from "../lib/router";
// Folder-as-tabs entry (TM-8): composeFolderTabsUrl builds the `/view/_tab` url
// from a folder's children. This sidebar -> views/Tabs.jsx import is the
// documented acyclic exception (Tabs.jsx never imports back), mirroring
// Breadcrumb.jsx -> views/Panel.jsx.
import { composeFolderTabsUrl } from "../views/Tabs";
import {
  loadBookmarks,
  isFolder,
  deleteBookmark,
  deleteFolder,
  renameBookmark,
  moveItem,
  createFolderWith,
  toggleFolder,
  isDescendant,
  armBookmark,
  disarmBookmark,
  getArmedBookmark,
  getArmedBookmarkFor,
  setBookmarkIcon,
  sameSearch,
  splitBookmarkUrl,
  isBookmarkMissing,
} from "../lib/bookmarks";
import { bookmarkSaveTarget } from "../lib/bookmark-file";
import { exportBookmarkFile, getConfig, statPath } from "../lib/api";
import IconPicker from "./IconPicker";
import { FolderIcon, LearnIcon } from "./FileIcons";
import type { Bookmark, BookmarkFolder, BookmarkItem } from "../lib/bookmarks";
import { loadRecents, displayRecents, setRecentsCollapsed } from "../lib/recents";
import {
  loadSidebarState,
  saveSidebarState,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_MAX_WIDTH,
} from "../lib/sidebarstate";
import { basename } from "../lib/format";
import {
  useUrlVersion,
  useBookmarksVersion,
  notifyBookmarksChanged,
  useRecentsVersion,
  useArmedVersion,
} from "../lib/hooks";
import type { Config } from "../lib/api";
import { splitShellSearch } from "../lib/layout-codec";
import { fuzzyMatch, highlightSegments } from "../lib/fuzzy";
import type { FuzzyResult } from "../lib/fuzzy";
import { useAccountLoggedIn } from "../lib/account";
import { useDeployEnabled } from "../lib/prefs";

// The fs path a bookmark targets, decoded from its /view/ url (same rule as
// the hover card). Used for search matching and the tooltip.
function bookmarkFsPath(url: string): string {
  const qIdx = url.indexOf("?");
  const pathname = qIdx !== -1 ? url.slice(0, qIdx) : url;
  return pathname.startsWith(VIEW_PREFIX)
    ? rootedFsPath(pathname.slice(VIEW_PREFIX.length).split("/").map(decodeURIComponent).join("/"))
    : pathname;
}

function renderHighlight(text: string, positions: number[]) {
  return highlightSegments(text, positions).map((seg, i) =>
    seg.match ? (
      <mark key={i} className="search-mark">
        {seg.text}
      </mark>
    ) : (
      <span key={i}>{seg.text}</span>
    )
  );
}

// Folder shape drawn inline so it inherits currentColor — an emoji folder
// ignores the theme and looks heavy at this size.
const FOLDER_ICON = (
  <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
    <path d="M1.5 4A1.5 1.5 0 0 1 3 2.5h3.1c.4 0 .78.16 1.06.44l.8.8c.1.1.22.16.35.16H13A1.5 1.5 0 0 1 14.5 5.4V12A1.5 1.5 0 0 1 13 13.5H3A1.5 1.5 0 0 1 1.5 12V4z" />
  </svg>
);

// Hover card content: target fs path + saved params. The saved search is
// split via splitShellSearch so the literal `&` inside the `_layout=(...)`
// span doesn't leak bogus param rows (D51).
function TooltipContent({ bookmark, missing }: { bookmark: Bookmark; missing: boolean }) {
  const qIdx = bookmark.url.indexOf("?");
  const search = qIdx !== -1 ? bookmark.url.slice(qIdx) : "";
  const fsPath = bookmarkFsPath(bookmark.url);

  const { layout, params: rest } = splitShellSearch(search);
  const params: [string, string][] = [...rest];
  if (layout !== null) params.push(["_layout", "(" + layout + ")"]);
  return (
    <>
      <div className="tip-path">{fsPath}</div>
      {missing && <div className="tip-missing">⚠ File not found — the target was moved or deleted</div>}
      {params.length ? (
        <div className="tip-params">
          {params.map(([k, v], i) => (
            <React.Fragment key={i}>
              <span className="tip-key">{k}</span>
              <span className="tip-val">{v}</span>
            </React.Fragment>
          ))}
        </div>
      ) : (
        <div className="tip-none">no params</div>
      )}
    </>
  );
}

// Inline rename input. Uncontrolled-feeling but React-controlled; a "settled"
// guard mirrors the vanilla one so blur-after-Enter doesn't double-commit.
interface RenameInputProps {
  initialName: string;
  onCommit: (value: string) => void;
  onCancel: () => void;
}

function RenameInput({ initialName, onCommit, onCancel }: RenameInputProps) {
  const [value, setValue] = useState(initialName);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const settledRef = useRef(false);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const commit = () => {
    if (settledRef.current) return;
    settledRef.current = true;
    onCommit(value);
  };
  const cancel = () => {
    if (settledRef.current) return;
    settledRef.current = true;
    onCancel();
  };

  return (
    <input
      ref={inputRef}
      type="text"
      className="bookmark-rename-input"
      value={value}
      onChange={(e: React.ChangeEvent<HTMLInputElement>) => setValue(e.target.value)}
      onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commit();
        } else if (e.key === "Escape") {
          e.preventDefault();
          cancel();
        }
      }}
      onBlur={commit}
    />
  );
}

interface DragProps {
  onDragStart: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragOver: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragLeave: (e: React.DragEvent<HTMLDivElement>) => void;
  onDrop: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragEnd: () => void;
}

interface BookmarkRowProps {
  b: Bookmark;
  child?: boolean;
  parentId?: string;
  active: boolean;
  dirty: boolean; // active via armed AND current params differ from saved -> "*" suffix
  missing: boolean; // target confirmed gone from disk (server's GET-time flag)
  isRenaming: boolean;
  justSaved: boolean; // transient ✓ on the save button after a successful export
  namePositions?: number[]; // search-match highlight positions in b.name
  onNameClick: (e: React.MouseEvent<HTMLAnchorElement>) => void;
  onSave: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onRename: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onDelete: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onCommitRename: (value: string) => void;
  onCancelRename: () => void;
  onMouseEnter: (e: React.MouseEvent<HTMLDivElement>) => void;
  onMouseLeave: () => void;
  onGlyphClick: (e: React.MouseEvent<HTMLSpanElement>) => void;
  registerRef: (el: HTMLDivElement | null) => void;
  dragProps: DragProps;
}

// Template for a bookmark row (top-level or, with child=true, inside a folder).
function BookmarkRow({ b, child, parentId, active, dirty, missing, isRenaming, justSaved, namePositions, onNameClick, onSave, onRename, onDelete, onCommitRename, onCancelRename, onMouseEnter, onMouseLeave, onGlyphClick, registerRef, dragProps }: BookmarkRowProps) {
  // Where "Save to disk" would write — shown on the button itself (title) so
  // the destination is visible before the click; null disables the button.
  const saveTarget = bookmarkSaveTarget(b);
  const savePath = saveTarget
    ? (saveTarget.dir.endsWith("/") ? saveTarget.dir : saveTarget.dir + "/") + saveTarget.filename
    : null;
  return (
    <div
      className={"bookmark-row" + (child ? " child-row" : "") + (active ? " active" : "") + (missing ? " missing" : "")}
      data-id={b.id}
      data-parent={child ? parentId : undefined}
      draggable="true"
      ref={registerRef}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      {...dragProps}
    >
      <span
        className={"bookmark-glyph" + (b.icon ? " custom-icon" : "")}
        title="Change icon"
        onClick={onGlyphClick}
      >
        {b.icon ?? "★"}
      </span>
      {isRenaming ? (
        <RenameInput initialName={b.name} onCommit={onCommitRename} onCancel={onCancelRename} />
      ) : (
        <a className="bookmark-name" href={b.url} draggable={false} onClick={onNameClick}>
          {namePositions && namePositions.length ? renderHighlight(b.name, namePositions) : b.name}
          {dirty && "*"}
        </a>
      )}
      {missing && (
        <span className="bookmark-missing-badge" title={`File not found: ${bookmarkFsPath(b.url)}`}>
          ⚠
        </span>
      )}
      <span className="bookmark-actions">
        <button
          className="icon-btn save-btn"
          title={savePath ? `Save to ${savePath}` : "Not savable: no common folder"}
          disabled={!savePath}
          onClick={onSave}
        >
          {justSaved ? "✓" : "💾︎"}
        </button>
        <button className="icon-btn rename-btn" title="Rename" onClick={onRename}>
          ✎
        </button>
        <button className="icon-btn delete-btn" title="Delete" onClick={onDelete}>
          ✕
        </button>
      </span>
    </div>
  );
}

interface FolderRowProps {
  folder: BookmarkFolder;
  child?: boolean;
  parentId?: string;
  activeHint: boolean;
  isRenaming: boolean;
  onGlyphClick: (e: React.MouseEvent<HTMLSpanElement>) => void;
  onRowClick: (e: React.MouseEvent<HTMLDivElement>) => void;
  onRename: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onDelete: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onCommitRename: (value: string) => void;
  onCancelRename: () => void;
  registerRef: (el: HTMLDivElement | null) => void;
  dragProps: DragProps;
}

// activeHint: folder is collapsed but holds the current view's bookmark —
// highlight the row so the selection isn't invisible while folded away.
function FolderRow({ folder, child, parentId, activeHint, isRenaming, onGlyphClick, onRowClick, onRename, onDelete, onCommitRename, onCancelRename, registerRef, dragProps }: FolderRowProps) {
  return (
    <div
      className={"bookmark-row folder-row" + (child ? " child-row" : "") + (folder.collapsed ? " collapsed" : "") + (activeHint ? " active" : "")}
      data-id={folder.id}
      data-parent={child ? parentId : undefined}
      draggable="true"
      ref={registerRef}
      onClick={onRowClick}
      {...dragProps}
    >
      <span className="bookmark-glyph folder-glyph" onClick={onGlyphClick}>
        {FOLDER_ICON}
      </span>
      {isRenaming ? (
        <RenameInput initialName={folder.name} onCommit={onCommitRename} onCancel={onCancelRename} />
      ) : (
        <span className="bookmark-name folder-name">{folder.name}</span>
      )}
      <span className="folder-count">{folder.children.length}</span>
      <span className="bookmark-actions">
        <button className="icon-btn rename-btn" title="Rename" onClick={onRename}>
          ✎
        </button>
        <button className="icon-btn delete-btn" title="Delete folder and contents" onClick={onDelete}>
          ✕
        </button>
      </span>
    </div>
  );
}

interface SidebarProps {
  config: Config;
}

interface HoverState {
  bookmark: Bookmark;
  rect: { top: number; right: number };
}

export default function Sidebar({ config }: SidebarProps) {
  // Re-render on any nav/url change (active-row highlight) and on every
  // bookmark-store mutation (this component is itself the primary subscriber
  // of the store it renders).
  useUrlVersion();
  useBookmarksVersion();
  useRecentsVersion();
  // Arm/disarm doesn't always coincide with a url or bookmark-store event —
  // the Breadcrumb's pathname-change disarm fires from an effect after this
  // component already rendered — so the armed store notifies separately.
  useArmedVersion();
  // Signed-in dot on the footer's Preferences entry (SPEC AC-1): shown only
  // once Deploy is enabled, since that's the only reason this app cares about
  // a Fused account at all — a dot for a feature the user hasn't turned on
  // would just be a mystery indicator.
  const accountLoggedIn = useAccountLoggedIn();
  const deployEnabled = useDeployEnabled();

  // BUGBOT: config (and its learn_mount_ready flag) is fetched exactly ONCE
  // at page load (main.tsx), well before the server's background automount
  // thread has finished attaching the learn mount — ensure_learn_mount now
  // force-detaches and remounts it on every startup, so the one-shot fetch
  // essentially always sees false and the Learn entry would never appear
  // for the whole session. Re-poll /api/config on a short bounded interval
  // (mirrors main.tsx's own bookmark-poll pattern) until it flips true;
  // capped at MAX_ATTEMPTS so a dev checkout with no bundled learn.zip
  // (never becomes ready) doesn't poll forever.
  //
  // BUGBOT: the bound must comfortably exceed attach_mount's own worst case
  // — up to ~10s for ensure_rcd to spawn/confirm the rclone daemon, plus a
  // full 60s mount/mount rc timeout (shell/mounts.py) — or a slow-but-
  // eventually-successful mount finishes after the poll gives up and the
  // entry never appears without a full page reload. 2s x 60 = 120s, safely
  // past that ~70s worst case with margin.
  //
  // BUGBOT: gating the poll on "only start if the INITIAL fetch saw false"
  // was itself racy — rcd survives server restarts, so the boot-time
  // /api/config fetch can catch a still-live mount from the PRIOR run and
  // report true, moments before ensure_learn_mount's own forced detach (see
  // its docstring) rips that very mount out from under it. Polling would
  // then never engage at all, and the entry would point at an empty
  // mountpoint for the remount window — or the whole session, if the
  // remount fails. So this always re-verifies via a live poll after mount,
  // regardless of the seeded initial value, and follows whatever the fresh
  // answer says (including back to not-ready, if the detach window is
  // caught mid-poll) rather than trusting the one-shot snapshot as final.
  const [learnMountReady, setLearnMountReady] = useState(config.learn_mount_ready);
  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    // BUGBOT: setInterval fires a new getConfig() every tick without
    // waiting for the previous one to settle, so responses can arrive
    // out of order (a slow earlier request resolving AFTER a faster later
    // one). Unconditionally applying whatever resolves most recently in
    // WALL-CLOCK order let a stale `false` from an earlier in-flight
    // request overwrite a `true` a later request already reported —
    // permanently, since that `true` had already cleared the interval.
    // latestRequestId tracks which tick's request is the newest ISSUED
    // one; only that request's response is applied, so a straggler from
    // an earlier tick is discarded as stale rather than overwriting it.
    let latestRequestId = 0;
    const MAX_ATTEMPTS = 60;
    const POLL_MS = 2000;
    const timer = window.setInterval(() => {
      attempts += 1;
      const requestId = ++latestRequestId;
      getConfig().then(
        (fresh) => {
          if (cancelled || requestId !== latestRequestId) return;
          setLearnMountReady(fresh.learn_mount_ready);
          if (fresh.learn_mount_ready || attempts >= MAX_ATTEMPTS) {
            window.clearInterval(timer);
          }
        },
        () => {
          if (cancelled || requestId !== latestRequestId) return;
          // Transient fetch failure — just try again next tick.
          if (attempts >= MAX_ATTEMPTS) window.clearInterval(timer);
        }
      );
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
    // Deliberately empty deps: run once on mount only. Depending on
    // learnMountReady here would restart the whole bounded poll window
    // from zero every time it changes.
  }, []);

  // Sidebar chrome: draggable width + collapsed flag, persisted once per
  // gesture (drag end / toggle), not per mousemove. Width lives in React
  // state — per-pointermove setState is fine (React 18 batches) and there is
  // no transition during a drag, so no jank.
  const [{ width: sidebarWidth, collapsed: sidebarCollapsed }, setSidebarState] =
    useState(loadSidebarState);
  // True only while the handle is captured — used to suppress the collapse
  // transition and text selection mid-drag.
  const [resizing, setResizing] = useState(false);
  const dragRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null);

  // Double-press-to-collapse is detected manually here: preventDefault on
  // pointerdown (needed to stop a text selection starting before the
  // body:has(.resizing) rule commits) suppresses the compatibility mouse
  // events that produce dblclick in several engines, so onDoubleClick on the
  // handle can't be relied on.
  const lastHandlePressRef = useRef<{ time: number; x: number } | null>(null);

  const onHandlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault(); // no text selection while dragging
    const last = lastHandlePressRef.current;
    if (last && e.timeStamp - last.time < 350 && Math.abs(e.clientX - last.x) < 5) {
      // Second press of a double-press: collapse instead of starting a drag.
      lastHandlePressRef.current = null;
      toggleSidebarCollapsed();
      return;
    }
    lastHandlePressRef.current = { time: e.timeStamp, x: e.clientX };
    dragRef.current = { pointerId: e.pointerId, startX: e.clientX, startWidth: sidebarWidth };
    e.currentTarget.setPointerCapture(e.pointerId);
    setResizing(true);
  };

  const onHandlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || e.pointerId !== drag.pointerId) return;
    // A real drag isn't the first half of a double-press.
    if (Math.abs(e.clientX - drag.startX) >= 5) lastHandlePressRef.current = null;
    const width = Math.min(
      SIDEBAR_MAX_WIDTH,
      Math.max(SIDEBAR_MIN_WIDTH, drag.startWidth + (e.clientX - drag.startX))
    );
    setSidebarState((s) => (s.width === width ? s : { ...s, width }));
  };

  const onHandlePointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || e.pointerId !== drag.pointerId) return;
    dragRef.current = null;
    setResizing(false);
    // Persist the final width (functional read — the last pointermove's
    // setState may not have committed yet).
    setSidebarState((s) => {
      saveSidebarState(s);
      return s;
    });
  };

  const toggleSidebarCollapsed = () => {
    // Collapsing unmounts the overlay surfaces but not their state — clear it
    // so expanding doesn't resurrect a stale-anchored icon picker, an
    // in-progress rename, or a tooltip.
    setIconPicker(null);
    setRenamingId(null);
    setHover(null);
    setSidebarState((s) => {
      const next = { ...s, collapsed: !s.collapsed };
      saveSidebarState(next);
      return next;
    });
  };

  const [renamingId, setRenamingId] = useState<string | null>(null);
  // Bookmark just exported to disk: its save button shows ✓ for a moment.
  const [savedId, setSavedId] = useState<string | null>(null);
  const savedTimer = useRef<number | null>(null);
  const [bmQuery, setBmQuery] = useState("");
  const [hover, setHover] = useState<HoverState | null>(null);
  // Icon picker: which bookmark's glyph was clicked + where to anchor it.
  const [iconPicker, setIconPicker] = useState<{ id: string; top: number; left: number } | null>(
    null
  );
  const tooltipRef = useRef<HTMLDivElement | null>(null);
  // id -> row DOM node, for imperative drag-class toggling (mirrors the
  // vanilla module's querySelectorAll(".bookmark-row") sweep on dragend).
  const rowRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  // Drag state lives in refs, not React state — it changes on every
  // dragover and must never trigger a re-render (that would fight the
  // imperative classList toggling below).
  const draggedIdRef = useRef<string | null>(null);
  const draggedIsFolderRef = useRef(false);

  // Recents (SPEC §29): last files opened. Display order is stable-slot
  // (RC-11) — a shown file keeps its row for the session, only a genuinely
  // new open moves anything — while the store underneath stays strict MRU.
  const { collapsed: recentsCollapsed } = loadRecents();
  const recents = displayRecents();

  const onRecentsHeadingClick = () => {
    // Persisted with the data itself (recents.json), like D44's folder
    // collapse; the store notifies, so no explicit re-render call here.
    void setRecentsCollapsed(!recentsCollapsed);
  };

  const onRecentClick = (e: React.MouseEvent<HTMLAnchorElement>, url: string) => {
    // Plain navigation to the stored url verbatim — query preserved
    // (navigateUrl, not navigate). href kept for middle-click/copy-link.
    // Opening a recent arms nothing — it is not a bookmark.
    e.preventDefault();
    navigateUrl(url);
  };

  const items = loadBookmarks(); // top-level items: bookmarks and folders
  // Folders at every depth, keyed by id — drop handlers resolve their
  // immediate-parent children arrays through this map.
  const folderById = new Map<string, BookmarkFolder>();
  const indexFolders = (list: BookmarkItem[]): void => {
    for (const it of list) {
      if (isFolder(it)) {
        folderById.set(it.id, it);
        indexFolders(it.children);
      }
    }
  };
  indexFolders(items);
  const topOrder = items.map((it) => it.id); // top-level display order

  // Bookmark search: a non-empty query flattens the tree to matching rows.
  // Matches a bookmark fuzzily on its name (or its folder's name — a folder
  // match pulls in all children), or on its target path as a contiguous
  // case-insensitive substring (fuzzy on a long path matched nearly anything).
  // Highlight positions come from the name match (a path-only or folder-name
  // hit shows the name unhighlighted). Ranked like the explorer search within
  // name matches: longest consecutive matched run first (a contiguous
  // substring hit beats a scattered subsequence one), then higher fuzzy score,
  // then alphabetical. Path-substring-only matches always rank below name
  // matches, alphabetically.
  const bq = bmQuery.trim();
  const bmSearching = bq !== "";
  const matched: { b: Bookmark; namePositions: number[] }[] = [];
  if (bmSearching) {
    const bqLower = bq.toLowerCase();
    const pathHit = (url: string) => bookmarkFsPath(url).toLowerCase().includes(bqLower);
    const ranked: { b: Bookmark; namePositions: number[]; nameHit: boolean; longestRun: number; score: number }[] = [];
    // The strength of a match across all name fields that hit, for ranking. A
    // folder name match contributes its own run/score to every child it pulls in.
    const rank = (folderM: FuzzyResult | null, ...ms: (FuzzyResult | null)[]) => {
      let longestRun = 0;
      let score = -Infinity;
      for (const m of [folderM, ...ms]) {
        if (!m) continue;
        if (m.longestRun > longestRun) longestRun = m.longestRun;
        if (m.score > score) score = m.score;
      }
      return { longestRun, score };
    };
    // Walk all depths; `folderM` carries the strongest match among the
    // bookmark's ancestor folder names (any matching ancestor pulls in its
    // whole subtree, same as the old one-level folder-match rule).
    const walk = (list: BookmarkItem[], folderM: FuzzyResult | null): void => {
      for (const it of list) {
        if (isFolder(it)) {
          const ownM = fuzzyMatch(bq, it.name);
          walk(it.children, ownM && (!folderM || ownM.score > folderM.score) ? ownM : folderM);
        } else {
          const nameM = fuzzyMatch(bq, it.name);
          if (folderM || nameM || pathHit(it.url)) {
            const { longestRun, score } = rank(folderM, nameM);
            ranked.push({ b: it, namePositions: nameM ? nameM.positions : [], nameHit: !!(folderM || nameM), longestRun, score });
          }
        }
      }
    };
    walk(items, null);
    ranked.sort((a, b) => {
      if (a.nameHit !== b.nameHit) return a.nameHit ? -1 : 1;
      if (b.longestRun !== a.longestRun) return b.longestRun - a.longestRun;
      if (b.score !== a.score) return b.score - a.score;
      return a.b.name.localeCompare(b.b.name, undefined, { sensitivity: "base" });
    });
    for (const { b, namePositions } of ranked) matched.push({ b, namePositions });
  }

  // Rows in search results are not reorderable; a no-op drag keeps the shared
  // BookmarkRow contract without letting a filtered view mutate the store order.
  const noDrag: DragProps = {
    onDragStart: (e) => e.preventDefault(),
    onDragOver: () => {},
    onDragLeave: () => {},
    onDrop: () => {},
    onDragEnd: () => {},
  };

  // Position the tooltip after its content has rendered, same timing as the
  // vanilla code reading tooltipEl.offsetHeight right after setting innerHTML.
  useLayoutEffect(() => {
    if (!hover || !tooltipRef.current) return;
    const el = tooltipRef.current;
    el.style.left = `${hover.rect.right + 8}px`;
    const top = Math.min(hover.rect.top, window.innerHeight - el.offsetHeight - 12);
    el.style.top = `${Math.max(8, top)}px`;
  }, [hover]);

  const hideTooltip = () => setHover(null);

  const registerRow = (id: string) => (el: HTMLDivElement | null) => {
    if (el) rowRefs.current.set(id, el);
    else rowRefs.current.delete(id);
  };

  const onFusedClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault();
    if (config && config.fused_dir) navigate(config.fused_dir, { isDir: true });
  };

  // D123: the bundled learn.zip is mounted read-only at `${mounts_root}/learn`
  // (LEARN_MOUNT_NAME in shell/mounts.py — always "learn"), so no separate
  // /api/mounts round trip is needed, same as the Fused entry above.
  const onLearnClick = async (e: React.MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault();
    if (!config || !config.mounts_root) return;
    const root = `${config.mounts_root.replace(/\/+$/, "")}/learn`;
    // Prefer the bundled index.html as the landing page when it exists;
    // fall back to the mount folder otherwise (older learn.zip builds).
    // The stat can be slow (mount-backed read); if the user navigated
    // elsewhere while it was in flight, don't yank them back.
    const before = currentUrl();
    let dest = root;
    let destIsDir = true;
    try {
      const st = await statPath(`${root}/index.html`);
      if (!st.is_dir) {
        dest = `${root}/index.html`;
        destIsDir = false;
      }
    } catch {
      // stat 404s (or the mount is briefly not attached) — open the folder.
    }
    if (currentUrl() === before) navigate(dest, { isDir: destIsDir });
  };

  // --- bookmark row handlers -------------------------------------------------

  const onBookmarkNameClick = (e: React.MouseEvent<HTMLAnchorElement>, b: Bookmark) => {
    // Open the bookmark and arm it for tracking. href is kept for
    // middle-click / copy-link, but a plain click routes in-shell.
    e.preventDefault();
    hideTooltip();
    armBookmark(b.id, b.url);
    navigateUrl(b.url);
  };

  const onDeleteBookmark = async (e: React.MouseEvent<HTMLButtonElement>, id: string) => {
    e.preventDefault();
    hideTooltip();
    const armed = getArmedBookmark();
    await deleteBookmark(id);
    if (armed && armed.id === id) {
      disarmBookmark();
      // No breadcrumb import (one-way dep rule); let main.jsx re-sync.
      window.dispatchEvent(new Event("fused:urlchange"));
    }
    notifyBookmarksChanged();
  };

  const onSaveBookmark = async (e: React.MouseEvent<HTMLButtonElement>, b: Bookmark) => {
    // Write the `<name>.bookmark` snapshot next to the bookmark's target(s)
    // (SB-8). The button is disabled when there is no save target, so a null
    // here is only a race with a concurrent rename — just do nothing.
    e.preventDefault();
    const target = bookmarkSaveTarget(b);
    if (!target) return;
    try {
      await exportBookmarkFile(target);
    } catch (err) {
      console.error("[fused] failed to save bookmark file:", err);
      return;
    }
    setSavedId(b.id);
    if (savedTimer.current !== null) window.clearTimeout(savedTimer.current);
    savedTimer.current = window.setTimeout(() => setSavedId(null), 1500);
  };

  const onRenameBookmark = (e: React.MouseEvent<HTMLButtonElement>, id: string) => {
    e.preventDefault();
    hideTooltip();
    setRenamingId(id);
  };

  const onRowMouseEnter = (e: React.MouseEvent<HTMLDivElement>, b: Bookmark) => {
    // No tooltip while renaming this row or while a drag is in progress.
    if (draggedIdRef.current !== null) return;
    if (renamingId === b.id) return;
    const rect = e.currentTarget.getBoundingClientRect();
    setHover({ bookmark: b, rect: { top: rect.top, right: rect.right } });
  };

  const onBookmarkGlyphClick = (e: React.MouseEvent<HTMLSpanElement>, id: string) => {
    e.preventDefault();
    e.stopPropagation();
    hideTooltip();
    const rect = e.currentTarget.getBoundingClientRect();
    setIconPicker((cur) => (cur?.id === id ? null : { id, top: rect.top, left: rect.left }));
  };

  const onPickIcon = async (icon: string | null) => {
    const target = iconPicker;
    setIconPicker(null);
    if (target) {
      await setBookmarkIcon(target.id, icon);
      notifyBookmarksChanged();
    }
  };

  const commitRename = async (id: string, value: string, fallbackName: string) => {
    setRenamingId(null);
    await renameBookmark(id, value.trim() || fallbackName);
    notifyBookmarksChanged();
  };
  const cancelRename = () => setRenamingId(null);

  // --- folder row handlers ----------------------------------------------------

  const onFolderGlyphClick = async (e: React.MouseEvent<HTMLSpanElement>, id: string) => {
    e.preventDefault();
    e.stopPropagation(); // don't also trigger the row's open handler
    await toggleFolder(id);
    notifyBookmarksChanged();
  };

  // Name or row click opens the folder as tabs, except over the glyph, the
  // action buttons, or the inline rename input. Opening arms nothing — a
  // folder is not a bookmark.
  const onFolderRowClick = async (e: React.MouseEvent<HTMLDivElement>, folder: BookmarkFolder) => {
    const target = e.target as HTMLElement;
    if (
      target.closest(".folder-glyph") ||
      target.closest(".bookmark-actions") ||
      target.closest(".bookmark-rename-input")
    ) {
      return;
    }
    e.preventDefault();
    // Tabs are bookmarks-only: direct bookmark children open as tabs, nested
    // folders are skipped (not flattened) — they open via their own row.
    const tabChildren = folder ? folder.children.filter((c): c is Bookmark => !isFolder(c)) : [];
    if (!folder || !tabChildren.length) {
      // Nothing to open as tabs, but still expand a collapsed folder so its
      // nested contents become reachable.
      if (folder && folder.collapsed && folder.children.length) {
        await toggleFolder(folder.id);
        notifyBookmarksChanged();
      }
      return;
    }
    if (folder.collapsed) await toggleFolder(folder.id); // expand only — never re-collapse
    // No notifyBookmarksChanged() here: navigateUrl re-renders the sidebar
    // via useUrlVersion (mirrors the vanilla route()-driven re-render).
    navigateUrl(composeFolderTabsUrl(tabChildren));
  };

  const onDeleteFolder = async (e: React.MouseEvent<HTMLButtonElement>, id: string, folder: BookmarkFolder) => {
    e.preventDefault();
    // Deleting a folder removes its children too; disarm if the armed
    // bookmark is one of them (mirrors the bookmark delete handler).
    const armed = getArmedBookmark();
    // Capture before the await: `folder` is the pre-delete render snapshot,
    // so its subtree is still walkable for the armed check.
    const holdsArmed = (list: BookmarkItem[]): boolean =>
      list.some((c) => (isFolder(c) ? holdsArmed(c.children) : c.id === armed?.id));
    await deleteFolder(id);
    if (armed && folder && holdsArmed(folder.children)) {
      disarmBookmark();
      window.dispatchEvent(new Event("fused:urlchange"));
    }
    notifyBookmarksChanged();
  };

  // --- drag & drop -------------------------------------------------------------

  // Compute the active drop zone for a row given the dragged item, or null
  // when the drag should be ignored entirely. Zones: "above" | "below" | "into".
  const dropZone = (
    e: React.DragEvent<HTMLDivElement>,
    row: HTMLDivElement,
    rowIsFolder: boolean
  ): "above" | "below" | "into" | null => {
    const rect = row.getBoundingClientRect();
    const y = e.clientY - rect.top;
    // "into": a folder row accepts anything at any depth (D121 nesting);
    // a bookmark onto a bookmark at any depth combines into a new subfolder
    // (dragged folders never combine — folders only nest via folder rows).
    const combine = rowIsFolder || !draggedIsFolderRef.current;
    if (combine) {
      if (y < rect.height * 0.25) return "above";
      if (y > rect.height * 0.75) return "below";
      return "into";
    }
    return y > rect.height / 2 ? "below" : "above";
  };

  // A folder must never land on itself or inside its own subtree — ignore
  // its own descendants' rows entirely (dragover gives no drop affordance).
  const overOwnSubtree = (rowId: string): boolean =>
    draggedIsFolderRef.current &&
    draggedIdRef.current !== null &&
    isDescendant(items, draggedIdRef.current, rowId);

  // Top-level reorder: move dragged to sit above/below the target row.
  const moveTopLevel = (targetId: string, below: boolean): Promise<void> => {
    let target = topOrder.indexOf(targetId) + (below ? 1 : 0);
    // Post-removal convention: a top-level dragged item earlier in the array
    // shifts every later index down by one. Items dragged out of a folder are
    // not in topOrder, so they need no adjustment.
    const from = topOrder.indexOf(draggedIdRef.current as string);
    if (from !== -1 && from < target) target -= 1;
    return moveItem(draggedIdRef.current as string, null, target);
  };

  const clearDragClasses = () => {
    rowRefs.current.forEach((r) => {
      r.classList.remove("dragging", "drag-above", "drag-below", "drag-into");
    });
  };

  const onRowDragStart = (e: React.DragEvent<HTMLDivElement>, id: string, rowIsFolder: boolean) => {
    const row = e.currentTarget;
    // No drag while renaming — let the input keep native text selection.
    if (row.querySelector(".bookmark-rename-input")) {
      e.preventDefault();
      return;
    }
    draggedIdRef.current = id;
    draggedIsFolderRef.current = rowIsFolder;
    row.classList.add("dragging");
    hideTooltip();
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", id); // Firefox needs data set to start a drag
  };

  const onRowDragOver = (
    e: React.DragEvent<HTMLDivElement>,
    id: string,
    rowIsFolder: boolean
  ) => {
    if (draggedIdRef.current === null || draggedIdRef.current === id) return;
    const row = e.currentTarget;
    if (overOwnSubtree(id)) {
      // No zone classes either — the whole subtree is a dead drop target.
      row.classList.remove("drag-above", "drag-below", "drag-into");
      return;
    }
    const zone = dropZone(e, row, rowIsFolder);
    if (zone === null) return;
    e.preventDefault(); // required to allow a drop
    e.dataTransfer.dropEffect = "move";
    row.classList.toggle("drag-above", zone === "above");
    row.classList.toggle("drag-below", zone === "below");
    row.classList.toggle("drag-into", zone === "into");
  };

  const onRowDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
    e.currentTarget.classList.remove("drag-above", "drag-below", "drag-into");
  };

  const onRowDrop = async (
    e: React.DragEvent<HTMLDivElement>,
    id: string,
    rowIsFolder: boolean,
    rowIsChild: boolean
  ) => {
    if (draggedIdRef.current === null || draggedIdRef.current === id) return;
    if (overOwnSubtree(id)) return; // moveItem's cycle guard is the backstop
    const draggedId = draggedIdRef.current;
    const row = e.currentTarget;
    const zone = dropZone(e, row, rowIsFolder);
    if (zone === null) return;
    e.preventDefault();
    const below = zone === "below";

    if (zone === "into" && !rowIsFolder) {
      // Bookmark onto a bookmark (any depth): make a folder of the two in the
      // target's slot, then immediately rename it. Reset drag state before the
      // await so a stale ref can't leak into a follow-up drag.
      draggedIdRef.current = null;
      draggedIsFolderRef.current = false;
      const folderId = await createFolderWith(id, draggedId);
      notifyBookmarksChanged();
      if (folderId) setRenamingId(folderId);
      return;
    }

    if (zone === "into" && rowIsFolder) {
      // Bookmark into a folder: append to its children.
      const folder = folderById.get(id);
      const inThisFolder = folder && folder.children.some((c) => c.id === draggedId);
      const targetIndex = (folder ? folder.children.length : 0) - (inThisFolder ? 1 : 0);
      await moveItem(draggedId, id, targetIndex);
    } else if (rowIsChild) {
      // Reorder within the target's folder.
      const parentId = row.getAttribute("data-parent");
      const folder = parentId ? folderById.get(parentId) : undefined;
      const childOrder = folder ? folder.children.map((c) => c.id) : [];
      let index = childOrder.indexOf(id) + (below ? 1 : 0);
      const from = childOrder.indexOf(draggedId);
      if (from !== -1 && from < index) index -= 1; // dragged in same folder, earlier
      await moveItem(draggedId, parentId, index);
    } else {
      // Top-level reorder (target is a top-level bookmark or a folder row).
      await moveTopLevel(id, below);
    }

    // Reset here, not just in dragend: the re-render triggered by
    // notifyBookmarksChanged() detaches the dragged row, and Chrome skips
    // dragend on a removed source element.
    draggedIdRef.current = null;
    draggedIsFolderRef.current = false;
    notifyBookmarksChanged();
  };

  const onRowDragEnd = () => {
    // Fires even on Escape-cancelled drags — the universal cleanup.
    draggedIdRef.current = null;
    draggedIsFolderRef.current = false;
    clearDragClasses();
  };

  const dragProps = (id: string, rowIsFolder: boolean, rowIsChild: boolean): DragProps => ({
    onDragStart: (e) => onRowDragStart(e, id, rowIsFolder),
    onDragOver: (e) => onRowDragOver(e, id, rowIsFolder),
    onDragLeave: onRowDragLeave,
    onDrop: (e) => onRowDrop(e, id, rowIsFolder, rowIsChild),
    onDragEnd: onRowDragEnd,
  });

  // Active row = the armed bookmark (the one being "followed"/edited — same
  // tracking the Update-bookmark button uses), regardless of live param
  // drift. Read through the pathname gate: an armed entry whose page the user
  // has left counts as not-armed here, so the highlight falls back to the
  // exact-url match immediately instead of waiting on (or, for routes without
  // CrumbActions, forever missing) the Breadcrumb's disarm effect. With
  // nothing armed, exact-url match still highlights a pasted/hand-typed url
  // (matching never arms).
  const armed = getArmedBookmarkFor(location.pathname);
  const rowActive = (b: Bookmark): boolean =>
    armed ? armed.id === b.id : b.url === currentUrl();
  // Dirty = the armed row's current params differ from its saved url — the
  // exact visibility condition of the Update-bookmark button (Breadcrumb).
  // Pathname already matches (the gate above), so only the search differs.
  const rowDirty = (b: Bookmark): boolean =>
    !!armed &&
    armed.id === b.id &&
    !sameSearch(location.search, splitBookmarkUrl(armed.url).search);

  // True when the active bookmark lives anywhere in this subtree — keeps the
  // collapsed-folder active hint visible at any nesting depth.
  const subtreeHoldsActive = (list: BookmarkItem[]): boolean =>
    list.some((c) => (isFolder(c) ? subtreeHoldsActive(c.children) : rowActive(c)));

  // Recursive tree render (D121). parentId is the immediate parent's id
  // (null at top level); rows inside any folder carry it via data-parent so
  // drop handlers can resolve the right children array. Indentation comes
  // free from nesting .folder-children (its margin+rail compound per level).
  const renderItems = (list: BookmarkItem[], parentId: string | null): React.ReactNode =>
    list.map((it) => {
      const child = parentId !== null;
      if (isFolder(it)) {
        const activeHint = it.collapsed && subtreeHoldsActive(it.children);
        return (
          <React.Fragment key={it.id}>
            <FolderRow
              folder={it}
              child={child}
              parentId={parentId ?? undefined}
              activeHint={activeHint}
              isRenaming={renamingId === it.id}
              registerRef={registerRow(it.id)}
              onGlyphClick={(e) => onFolderGlyphClick(e, it.id)}
              onRowClick={(e) => onFolderRowClick(e, it)}
              onRename={(e) => {
                e.preventDefault();
                setRenamingId(it.id);
              }}
              onDelete={(e) => onDeleteFolder(e, it.id, it)}
              onCommitRename={(value) => commitRename(it.id, value, it.name)}
              onCancelRename={cancelRename}
              dragProps={dragProps(it.id, true, child)}
            />
            {!it.collapsed && (
              <div className="folder-children">{renderItems(it.children, it.id)}</div>
            )}
          </React.Fragment>
        );
      }
      return (
        <BookmarkRow
          key={it.id}
          b={it}
          child={child}
          parentId={parentId ?? undefined}
          active={rowActive(it)}
          dirty={rowDirty(it)}
          missing={isBookmarkMissing(it.id)}
          isRenaming={renamingId === it.id}
          justSaved={savedId === it.id}
          registerRef={registerRow(it.id)}
          onNameClick={(e) => onBookmarkNameClick(e, it)}
          onSave={(e) => onSaveBookmark(e, it)}
          onRename={(e) => onRenameBookmark(e, it.id)}
          onDelete={(e) => onDeleteBookmark(e, it.id)}
          onCommitRename={(value) => commitRename(it.id, value, it.name)}
          onCancelRename={cancelRename}
          onMouseEnter={(e) => onRowMouseEnter(e, it)}
          onMouseLeave={hideTooltip}
          onGlyphClick={(e) => onBookmarkGlyphClick(e, it.id)}
          dragProps={dragProps(it.id, false, child)}
        />
      );
    });

  if (sidebarCollapsed) {
    // Collapsed: the whole sidebar shrinks to a slim strip that expands it
    // back. Still the same #sidebar node, so the <=700px media hide applies.
    return (
      <nav id="sidebar" className="sidebar-collapsed">
        <button
          type="button"
          className="sidebar-expand-strip"
          aria-label="Expand sidebar"
          title="Expand sidebar"
          onClick={toggleSidebarCollapsed}
        >
          {/* Bubble protruding into the content area — the visible half of
              the affordance; the whole strip is still the click target. */}
          <span className="sidebar-expand-bubble" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="m9 18 6-6-6-6" />
            </svg>
          </span>
        </button>
      </nav>
    );
  }

  return (
    <nav id="sidebar" style={{ flexBasis: sidebarWidth, width: sidebarWidth }}>
      <div className="sidebar-brand">
        {/* Fused cube mark (brand asset logo-black-bg-transparent.svg), stroke
            follows .logo's color so it stays on the accent token. */}
        <span className="logo">
          <svg width="20" height="20" viewBox="0 0 233 233" fill="none" aria-hidden="true">
            <path
              d="M43.916 84.6995L80.0899 105.742M43.916 84.6995L80.0899 64.13M43.916 84.6995V126.548M80.0899 105.742L114.383 125.69C115.548 126.368 116.264 127.613 116.264 128.96V162.056C116.264 164.973 113.101 166.793 110.579 165.326L43.916 126.548M80.0899 105.742V182.862C80.0899 185.779 76.9269 187.598 74.405 186.131L45.7968 169.49C44.6324 168.813 43.916 167.567 43.916 166.22V126.548M80.0899 105.742L152.674 64.13M80.0899 64.13L114.4 44.6204C115.556 43.9629 116.973 43.961 118.131 44.6152L152.674 64.13M80.0899 64.13L150.785 104.659C151.955 105.329 153.392 105.327 154.559 104.652L183.353 88.0121C185.887 86.5475 185.869 82.883 183.321 81.4432L152.674 64.13"
              stroke="currentColor"
              strokeWidth="12"
            />
          </svg>
        </span>{" "}
        <span className="brand-title">fused-render</span>
        <span className="brand-version">v{config.version}</span>
        <button
          type="button"
          className="icon-btn sidebar-collapse-btn"
          aria-label="Collapse sidebar"
          title="Collapse sidebar"
          onClick={toggleSidebarCollapsed}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="m15 18-6-6 6-6" />
          </svg>
        </button>
      </div>
      <div className="sidebar-section">
        <a href="#" id="fused-link" className="sidebar-item" onClick={onFusedClick}>
          <span className="icon"><FolderIcon /></span> Fused
        </a>
        {learnMountReady && (
          <a href="#" id="learn-link" className="sidebar-item" onClick={onLearnClick}>
            <span className="icon"><LearnIcon /></span> Learn
          </a>
        )}
      </div>
      <div className="sidebar-section sidebar-bookmarks">
        <div className="sidebar-heading">Bookmarks</div>
        {items.length === 0 ? (
          <div className="sidebar-empty">No bookmarks yet</div>
        ) : (
          <>
            <div className="bookmark-search">
              <input
                type="search"
                className="bookmark-search-input"
                placeholder="Search bookmarks…"
                value={bmQuery}
                onChange={(e) => setBmQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    e.preventDefault();
                    setBmQuery("");
                    e.currentTarget.blur();
                  }
                }}
              />
            </div>
            {bmSearching ? (
              matched.length ? (
                matched.map(({ b, namePositions }) => (
                  <BookmarkRow
                    key={b.id}
                    b={b}
                    namePositions={namePositions}
                    active={rowActive(b)}
                    dirty={rowDirty(b)}
                    missing={isBookmarkMissing(b.id)}
                    isRenaming={renamingId === b.id}
                    justSaved={savedId === b.id}
                    registerRef={() => {}}
                    onNameClick={(e) => onBookmarkNameClick(e, b)}
                    onSave={(e) => onSaveBookmark(e, b)}
                    onRename={(e) => onRenameBookmark(e, b.id)}
                    onDelete={(e) => onDeleteBookmark(e, b.id)}
                    onCommitRename={(value) => commitRename(b.id, value, b.name)}
                    onCancelRename={cancelRename}
                    onMouseEnter={(e) => onRowMouseEnter(e, b)}
                    onMouseLeave={hideTooltip}
                    onGlyphClick={(e) => onBookmarkGlyphClick(e, b.id)}
                    dragProps={noDrag}
                  />
                ))
              ) : (
                <div className="sidebar-empty">No matches</div>
              )
            ) : (
              renderItems(items, null)
            )}
          </>
        )}
      </div>
      {/* Recents (SPEC §29) — below the bookmark tree, above the pinned
          footer. Rows reuse the bookmark row classes so the section reads as
          a native sibling: same height, padding, glyph slot, ellipsis and
          hover treatment. Heading click toggles the fold; the count pill
          carries the collapsed signal (no chevron — D44). */}
      {recents.length > 0 && (
        <div className="sidebar-section sidebar-recents">
          <div
            className="sidebar-heading recents-heading"
            title={recentsCollapsed ? "Show recents" : "Hide recents"}
            onClick={onRecentsHeadingClick}
          >
            Recents
            {recentsCollapsed && <span className="recents-count">{recents.length}</span>}
          </div>
          {!recentsCollapsed &&
            recents.map((r) => {
              const fsPath = bookmarkFsPath(r.url);
              return (
                <a
                  // Keyed by fs path, not url: the url mutates on every live
                  // param write, and a key change would remount (flash) the row.
                  key={fsPath}
                  // No active/selected state on recents rows (owner call —
                  // unlike bookmark rows): the section is a jump list, not a
                  // location indicator.
                  className="bookmark-row recent-row"
                  href={r.url}
                  title={fsPath}
                  onClick={(e) => onRecentClick(e, r.url)}
                >
                  <span className="bookmark-glyph recent-glyph" aria-hidden="true">
                    {/* Clock in the star-glyph slot, inline so it follows
                        currentColor like the folder icon. */}
                    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                      <circle cx="8" cy="8" r="6.2" />
                      <path d="M8 4.8V8l2.3 1.6" />
                    </svg>
                  </span>
                  <span className="bookmark-name">{r.title || basename(fsPath)}</span>
                </a>
              );
            })}
        </div>
      )}
      {/* Preferences entry (SPEC §20) — pinned to the sidebar's bottom edge
          (margin-top: auto), deliberately unobtrusive: a muted gear row that
          navigates to the /view/_prefs sentinel. Three equal columns —
          Templates, Mounts, Preferences. */}
      <div className="sidebar-footer">
        <button
          type="button"
          title="Templates"
          aria-label="Templates"
          className={
            "sidebar-item prefs-link" + (location.pathname === "/view/_templates" ? " active" : "")
          }
          onClick={() => navigateUrl("/view/_templates")}
        >
          <span className="icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="3" y="3" width="7" height="7" rx="1" />
              <rect x="14" y="3" width="7" height="7" rx="1" />
              <rect x="3" y="14" width="7" height="7" rx="1" />
              <rect x="14" y="14" width="7" height="7" rx="1" />
            </svg>
          </span>
          <span className="prefs-label">Templates</span>
        </button>
        {/* PROTOTYPE: mounts entry — remote mounts, /view/_mounts. */}
        <button
          type="button"
          title="Mounts"
          aria-label="Mounts"
          className={
            "sidebar-item prefs-link" + (location.pathname === "/view/_mounts" ? " active" : "")
          }
          onClick={() => navigateUrl("/view/_mounts")}
        >
          <span className="icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M17.5 19a4.5 4.5 0 1 0-.9-8.9 6 6 0 1 0-11.4 2.4A3.5 3.5 0 0 0 6.5 19h11z" />
            </svg>
          </span>
          <span className="prefs-label">Mounts</span>
        </button>
        <button
          type="button"
          title="Preferences"
          aria-label="Preferences"
          className={
            "sidebar-item prefs-link" + (location.pathname === "/view/_prefs" ? " active" : "")
          }
          onClick={() => navigateUrl("/view/_prefs")}
        >
          <span className="icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
            {/* Fused-account signed-in signal (SPEC AC-1), folded onto the
                Preferences entry now that account management lives there as
                a tab rather than its own sidebar entry. Gated on Deploy
                being enabled — that's the only reason a Fused account
                matters here, and the dot rides the button's existing click
                target rather than being its own (too small to hit on its
                own). */}
            {deployEnabled && accountLoggedIn && <span className="account-signedin-dot" />}
          </span>
          <span className="prefs-label">Preferences</span>
        </button>
      </div>
      <div id="bookmark-tooltip" ref={tooltipRef} style={hover ? { display: "block" } : undefined}>
        {hover && <TooltipContent bookmark={hover.bookmark} missing={isBookmarkMissing(hover.bookmark.id)} />}
      </div>
      {iconPicker && (
        <IconPicker
          anchor={iconPicker}
          onPick={(icon) => onPickIcon(icon)}
          onRemove={() => onPickIcon(null)}
          onClose={() => setIconPicker(null)}
        />
      )}
      {/* Resize handle riding the right border: drag to resize (pointer
          capture keeps the gesture even when the cursor leaves the strip),
          double-press to collapse (detected in pointerdown — see
          lastHandlePressRef). */}
      <div
        className={"sidebar-resize-handle" + (resizing ? " resizing" : "")}
        style={{ left: sidebarWidth - 3 }}
        onPointerDown={onHandlePointerDown}
        onPointerMove={onHandlePointerMove}
        onPointerUp={onHandlePointerUp}
        onPointerCancel={onHandlePointerUp}
      />
    </nav>
  );
}
