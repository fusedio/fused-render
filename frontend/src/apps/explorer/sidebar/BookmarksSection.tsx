// The explorer's bookmark tree — extracted from the shell Sidebar when
// bookmarks became an explorer concept (super-app step 2): search, nested
// folders, drag-reorder, inline rename, icon picker, hover card. Renders both
// as the explorer sidebar's Bookmarks section and as the /explorer homepage's
// launcher (FilesHome).
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { navigateUrl, currentUrl, rootedFsPath, VIEW_PREFIX } from "@platform/lib/router";
// Folder-as-tabs entry (TM-8): composeFolderTabsUrl builds the `_tab` url
// from a folder's children. This sidebar -> Tabs import is the documented
// acyclic exception (Tabs never imports back), mirroring Breadcrumb -> Panel.
import { composeFolderTabsUrl } from "@apps/explorer/Tabs";
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
  takeLastAddedBookmarkId,
} from "@platform/lib/bookmarks";
import { bookmarkSaveTarget } from "@platform/lib/bookmark-file";
import { exportBookmarkFile } from "@platform/lib/api";
import { isRowDragActive } from "@apps/explorer/listing/row-drag";
import IconPicker from "@platform/ui/IconPicker";
import type { Bookmark, BookmarkFolder, BookmarkItem } from "@platform/lib/bookmarks";
import {
  useUrlVersion,
  useBookmarksVersion,
  notifyBookmarksChanged,
  useArmedVersion,
} from "@platform/lib/hooks";
import { splitShellSearch } from "@platform/lib/layout-codec";
import { fuzzyMatch, highlightSegments } from "@platform/lib/fuzzy";
import type { FuzzyResult } from "@platform/lib/fuzzy";

// The fs path a bookmark targets, decoded from its explorer url (same rule as
// the hover card). Used for search matching and the tooltip. The bare legacy
// "/view/" prefix still decodes (bookmarks saved before the /explorer rename).
export function bookmarkFsPath(url: string): string {
  const qIdx = url.indexOf("?");
  const pathname = qIdx !== -1 ? url.slice(0, qIdx) : url;
  const prefix = [VIEW_PREFIX, "/view/"].find((p) => pathname.startsWith(p));
  return prefix
    ? rootedFsPath(pathname.slice(prefix.length).split("/").map(decodeURIComponent).join("/"))
    : pathname;
}

// The folder a FILE DRAG may be dropped onto for this bookmark, or null when
// the bookmark isn't a place on this filesystem at all. Only explorer view
// urls qualify: bookmarkFsPath falls through to the raw pathname for anything
// else (a "/mounts" page, a cloud url), and "moving files into /mounts" is not
// a thing. Whether the path is a DIRECTORY is a question for the server — see
// the kind probe in BookmarksSection.
function bookmarkDropPath(url: string): string | null {
  const pathname = url.split("?")[0];
  const isView = [VIEW_PREFIX, "/view/"].some((p) => pathname.startsWith(p));
  return isView ? bookmarkFsPath(url) : null;
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
  // The folder a FILE dragged out of the listing may land in — null for a
  // bookmark that doesn't point at the filesystem at all.
  fsDropPath?: string | null;
}

// Template for a bookmark row (top-level or, with child=true, inside a folder).
function BookmarkRow({ b, child, parentId, active, dirty, missing, isRenaming, justSaved, namePositions, onNameClick, onSave, onRename, onDelete, onCommitRename, onCancelRename, onGlyphClick, onMouseEnter, onMouseLeave, registerRef, dragProps, fsDropPath }: BookmarkRowProps) {
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
      /* A drop target for entries dragged out of the listing. No
         data-fs-drop-dir: only the server knows whether this path is a folder,
         so the drag probes it (listing/row-drag.ts). announce, because the
         destination is not on screen. */
      data-fs-drop-path={fsDropPath ?? undefined}
      data-fs-drop-announce={fsDropPath ? "1" : undefined}
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
      {/* While the inline rename input is open the whole action cluster is
          gone: the input wants the row's full width, and every one of the
          three fights the edit in progress — save would snapshot the pre-edit
          name, rename is what's already happening, and delete would destroy
          the row being named. Commit or Escape first. */}
      {!isRenaming && (
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
      )}
    </div>
  );
}

interface FolderRowProps {
  folder: BookmarkFolder;
  child?: boolean;
  parentId?: string;
  // Optimistic expand/collapse state (see isCollapsed below): the store's
  // own `folder.collapsed` lags a click by a store write, so the row is told.
  collapsed: boolean;
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
function FolderRow({ folder, child, parentId, collapsed, activeHint, isRenaming, onGlyphClick, onRowClick, onRename, onDelete, onCommitRename, onCancelRename, registerRef, dragProps }: FolderRowProps) {
  return (
    <div
      className={"bookmark-row folder-row" + (child ? " child-row" : "") + (collapsed ? " collapsed" : "") + (activeHint ? " active" : "")}
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
      {/* Hidden mid-rename for the same reason as a bookmark row's cluster. */}
      {!isRenaming && (
        <span className="bookmark-actions">
          <button className="icon-btn rename-btn" title="Rename" onClick={onRename}>
            ✎
          </button>
          <button className="icon-btn delete-btn" title="Delete folder and contents" onClick={onDelete}>
            ✕
          </button>
        </span>
      )}
    </div>
  );
}

interface HoverState {
  bookmark: Bookmark;
  rect: { top: number; right: number };
}

export default function BookmarksSection() {
  // Re-render on any nav/url change (active-row highlight) and on every
  // bookmark-store mutation (this component is itself the primary subscriber
  // of the store it renders).
  useUrlVersion();
  const bookmarksVersion = useBookmarksVersion();
  // Arm/disarm doesn't always coincide with a url or bookmark-store event —
  // the Breadcrumb's pathname-change disarm fires from an effect after this
  // component already rendered — so the armed store notifies separately.
  useArmedVersion();

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

  // A new bookmark is appended to the end of the top-level list, which on a
  // tree of any size sits below the fold — so scroll it into view once the row
  // has rendered. Keyed off the bookmark-store version (the same signal that
  // rendered the row), and the id is consumed once by the store, so unrelated
  // later mutations don't re-scroll. The row is missing from rowRefs only
  // while a search filter is showing (those rows don't register) — nothing to
  // scroll to then, so skip.
  useEffect(() => {
    const id = takeLastAddedBookmarkId();
    if (!id) return;
    // block: "nearest" scrolls the section's own overflow container the
    // minimum amount, and won't drag the page around it.
    rowRefs.current.get(id)?.scrollIntoView({ block: "nearest" });
  }, [bookmarksVersion]);

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
      // No breadcrumb import (one-way dep rule); let main.tsx re-sync.
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
    // No tooltip while renaming this row or while a drag is in progress —
    // either a bookmark being reordered or files being dragged over from the
    // listing (a hover card over the row you are aiming at is the one thing it
    // must not do).
    if (draggedIdRef.current !== null || isRowDragActive()) return;
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

  // Expand/collapse is a store write (bookmarks.json, through a serial mutation
  // queue), and awaiting it before the UI moved meant the chevron and the
  // children lagged the click by a round trip. Flip optimistically instead: the
  // override wins over the store's value until the write lands and the store
  // notify re-renders with the same answer. Keyed per id, so two folders toggled
  // in quick succession don't clobber each other.
  const [collapseOverride, setCollapseOverride] = useState<Record<string, boolean>>({});
  const isCollapsed = (folder: BookmarkFolder): boolean =>
    collapseOverride[folder.id] ?? !!folder.collapsed;
  const applyToggle = async (folder: BookmarkFolder) => {
    const next = !isCollapsed(folder);
    setCollapseOverride((m) => ({ ...m, [folder.id]: next }));
    try {
      await toggleFolder(folder.id);
      notifyBookmarksChanged();
    } finally {
      // Drop the override either way: on success the store now agrees, and on
      // failure the row must fall back to the truth rather than lie forever.
      setCollapseOverride((m) => {
        const { [folder.id]: _dropped, ...rest } = m;
        return rest;
      });
    }
  };

  const onFolderGlyphClick = (e: React.MouseEvent<HTMLSpanElement>, folder: BookmarkFolder) => {
    e.preventDefault();
    e.stopPropagation(); // don't also trigger the row's open handler
    void applyToggle(folder);
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
      if (folder && isCollapsed(folder) && folder.children.length) {
        void applyToggle(folder);
      }
      return;
    }
    if (isCollapsed(folder)) void applyToggle(folder); // expand only — never re-collapse
    // No notifyBookmarksChanged() here: navigateUrl re-renders the section
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

  // --- files dragged in from the listing ---------------------------------------
  //
  // A bookmark that points at a folder is a drop target for entries dragged out
  // of the listing — the shortcut is where the user already thinks of that
  // folder as living, so dragging onto it is the one gesture that moves
  // something somewhere NOT on screen. That is also why it announces itself
  // with a toast (`data-fs-drop-announce`): the destination isn't visible, so
  // the confirmation has to be.
  //
  // ALL of that now happens in listing/row-drag.ts, and this section is the
  // three attributes below on the row. It used to be ~90 lines of dragover /
  // dragleave / drop here — a stat probe with its own cache, its own late-
  // repaint guard, its own highlight classes and its own copy of the drop
  // verdict — because native DnD delivers its events to the element under the
  // cursor and this was that element. The pointer drag hit-tests instead, so a
  // target says WHAT IT IS and nothing more:
  //
  //   data-fs-drop-path      the folder entries would move into
  //   data-fs-drop-announce  it is off screen, so say so when the move lands
  //   (no data-fs-drop-dir)  only the server knows whether this path is a
  //                          folder — the drag probes it, optimistically
  //                          treating it as one until the answer arrives, and
  //                          waits for the real answer before moving anything.

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
      r.classList.remove(
        "dragging",
        "drag-above",
        "drag-below",
        "drag-into",
        "drop-into",
        "drop-reject",
      );
    });
  };

  // End-of-drag cleanup for a REORDER drag that ends somewhere this section
  // never hears about (a drop outside the window). onRowDragEnd covers the
  // ordinary cases; this is the backstop, and it is document-level because the
  // events it needs are the ones that never reach us any other way.
  //
  // It no longer has to clean up after a FILE drag: that gesture is
  // pointer-driven and clears its own highlight from whatever it painted, on
  // every path out including Escape (listing/row-drag.ts).
  useEffect(() => {
    const onEnd = () => clearDragClasses();
    document.addEventListener("dragend", onEnd);
    document.addEventListener("drop", onEnd);
    return () => {
      document.removeEventListener("dragend", onEnd);
      document.removeEventListener("drop", onEnd);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  // Reordering the bookmark TREE. A file drag out of the listing is a different
  // gesture entirely — it moves FILES INTO the folder a bookmark points at —
  // and the two no longer need telling apart here: a file drag is pointer-
  // driven and fires no drag events at all, so anything that reaches this
  // handler is a bookmark being reordered.
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
    e.currentTarget.classList.remove("drag-above", "drag-below", "drag-into", "drop-reject");
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

  // Reordering the tree only. Where a FILE drag may land is not a handler at
  // all any more — it is `data-fs-drop-path` on the row (see BookmarkRow).
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
        const collapsed = isCollapsed(it);
        const activeHint = collapsed && subtreeHoldsActive(it.children);
        return (
          <React.Fragment key={it.id}>
            <FolderRow
              folder={it}
              child={child}
              parentId={parentId ?? undefined}
              collapsed={collapsed}
              activeHint={activeHint}
              isRenaming={renamingId === it.id}
              registerRef={registerRow(it.id)}
              onGlyphClick={(e) => onFolderGlyphClick(e, it)}
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
            {!collapsed && (
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
          fsDropPath={bookmarkDropPath(it.url)}
        />
      );
    });

  return (
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
    </div>
  );
}
