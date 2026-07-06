// Sidebar UI: brand, Home entry, bookmark rows with hover card + inline rename.
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { navigate, navigateUrl, currentUrl, VIEW_PREFIX } from "../lib/router.js";
// Folder-as-tabs entry (TM-8): composeFolderTabsUrl builds the `/view/_tab` url
// from a folder's children. This sidebar -> views/Tabs.jsx import is the
// documented acyclic exception (Tabs.jsx never imports back), mirroring
// Breadcrumb.jsx -> views/Panel.jsx.
import { composeFolderTabsUrl } from "../views/Tabs.jsx";
import {
  loadBookmarks,
  isFolder,
  deleteBookmark,
  deleteFolder,
  renameBookmark,
  moveItem,
  createFolderWith,
  toggleFolder,
  armBookmark,
  disarmBookmark,
  getArmedBookmark,
} from "../lib/bookmarks.js";
import { useUrlVersion, useBookmarksVersion, notifyBookmarksChanged } from "../lib/hooks.js";

// Folder shape drawn inline so it inherits currentColor — an emoji folder
// ignores the theme and looks heavy at this size.
const FOLDER_ICON = (
  <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
    <path d="M1.5 4A1.5 1.5 0 0 1 3 2.5h3.1c.4 0 .78.16 1.06.44l.8.8c.1.1.22.16.35.16H13A1.5 1.5 0 0 1 14.5 5.4V12A1.5 1.5 0 0 1 13 13.5H3A1.5 1.5 0 0 1 1.5 12V4z" />
  </svg>
);

// Hover card content: target fs path + saved params, decoded like the
// vanilla shell (raw URLSearchParams on the saved search — bookmark
// tooltips predate the `_layout` grammar; parity over cleverness).
function TooltipContent({ bookmark }) {
  let pathname = bookmark.url;
  let search = "";
  const qIdx = bookmark.url.indexOf("?");
  if (qIdx !== -1) {
    pathname = bookmark.url.slice(0, qIdx);
    search = bookmark.url.slice(qIdx);
  }
  const fsPath = pathname.startsWith(VIEW_PREFIX)
    ? "/" + pathname.slice(VIEW_PREFIX.length).split("/").map(decodeURIComponent).join("/")
    : pathname;

  const params = [...new URLSearchParams(search)];
  return (
    <>
      <div className="tip-path">{fsPath}</div>
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
function RenameInput({ initialName, onCommit, onCancel }) {
  const [value, setValue] = useState(initialName);
  const inputRef = useRef(null);
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
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={(e) => {
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

// Template for a bookmark row (top-level or, with child=true, inside a folder).
function BookmarkRow({ b, child, parentId, isRenaming, onNameClick, onRename, onDelete, onCommitRename, onCancelRename, onMouseEnter, onMouseLeave, registerRef, dragProps }) {
  return (
    <div
      className={"bookmark-row" + (child ? " child-row" : "") + (b.url === currentUrl() ? " active" : "")}
      data-id={b.id}
      data-parent={child ? parentId : undefined}
      draggable="true"
      ref={registerRef}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      {...dragProps}
    >
      <span className="bookmark-glyph">★</span>
      {isRenaming ? (
        <RenameInput initialName={b.name} onCommit={onCommitRename} onCancel={onCancelRename} />
      ) : (
        <a className="bookmark-name" href={b.url} draggable={false} onClick={onNameClick}>
          {b.name}
        </a>
      )}
      <span className="bookmark-actions">
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

// activeHint: folder is collapsed but holds the current view's bookmark —
// highlight the row so the selection isn't invisible while folded away.
function FolderRow({ folder, activeHint, isRenaming, onGlyphClick, onRowClick, onRename, onDelete, onCommitRename, onCancelRename, registerRef, dragProps }) {
  return (
    <div
      className={"bookmark-row folder-row" + (folder.collapsed ? " collapsed" : "") + (activeHint ? " active" : "")}
      data-id={folder.id}
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

export default function Sidebar({ config }) {
  // Re-render on any nav/url change (active-row highlight) and on every
  // bookmark-store mutation (this component is itself the primary subscriber
  // of the store it renders).
  useUrlVersion();
  useBookmarksVersion();

  const [renamingId, setRenamingId] = useState(null);
  const [hover, setHover] = useState(null); // { bookmark, rect: {top, right} }
  const tooltipRef = useRef(null);
  // id -> row DOM node, for imperative drag-class toggling (mirrors the
  // vanilla module's querySelectorAll(".bookmark-row") sweep on dragend).
  const rowRefs = useRef(new Map());
  // Drag state lives in refs, not React state — it changes on every
  // dragover and must never trigger a re-render (that would fight the
  // imperative classList toggling below).
  const draggedIdRef = useRef(null);
  const draggedIsFolderRef = useRef(false);

  const items = loadBookmarks(); // top-level items: bookmarks and folders
  const folderById = new Map(items.filter(isFolder).map((f) => [f.id, f]));
  const topOrder = items.map((it) => it.id); // top-level display order

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

  const registerRow = (id) => (el) => {
    if (el) rowRefs.current.set(id, el);
    else rowRefs.current.delete(id);
  };

  const onHomeClick = (e) => {
    e.preventDefault();
    if (config && config.home) navigate(config.home);
  };

  // --- bookmark row handlers -------------------------------------------------

  const onBookmarkNameClick = (e, b) => {
    // Open the bookmark and arm it for tracking. href is kept for
    // middle-click / copy-link, but a plain click routes in-shell.
    e.preventDefault();
    hideTooltip();
    armBookmark(b.id, b.url);
    navigateUrl(b.url);
  };

  const onDeleteBookmark = (e, id) => {
    e.preventDefault();
    hideTooltip();
    const armed = getArmedBookmark();
    deleteBookmark(id);
    if (armed && armed.id === id) {
      disarmBookmark();
      // No breadcrumb import (one-way dep rule); let main.jsx re-sync.
      window.dispatchEvent(new Event("fused:urlchange"));
    }
    notifyBookmarksChanged();
  };

  const onRenameBookmark = (e, id) => {
    e.preventDefault();
    hideTooltip();
    setRenamingId(id);
  };

  const onRowMouseEnter = (e, b) => {
    // No tooltip while renaming this row or while a drag is in progress.
    if (draggedIdRef.current !== null) return;
    if (renamingId === b.id) return;
    const rect = e.currentTarget.getBoundingClientRect();
    setHover({ bookmark: b, rect: { top: rect.top, right: rect.right } });
  };

  const commitRename = (id, value, fallbackName) => {
    renameBookmark(id, value.trim() || fallbackName);
    setRenamingId(null);
    notifyBookmarksChanged();
  };
  const cancelRename = () => setRenamingId(null);

  // --- folder row handlers ----------------------------------------------------

  const onFolderGlyphClick = (e, id) => {
    e.preventDefault();
    e.stopPropagation(); // don't also trigger the row's open handler
    toggleFolder(id);
    notifyBookmarksChanged();
  };

  // Name or row click opens the folder as tabs, except over the glyph, the
  // action buttons, or the inline rename input. Opening arms nothing — a
  // folder is not a bookmark.
  const onFolderRowClick = (e, folder) => {
    if (
      e.target.closest(".folder-glyph") ||
      e.target.closest(".bookmark-actions") ||
      e.target.closest(".bookmark-rename-input")
    ) {
      return;
    }
    e.preventDefault();
    if (!folder || !folder.children.length) return;
    if (folder.collapsed) toggleFolder(folder.id); // expand only — never re-collapse
    // No notifyBookmarksChanged() here: navigateUrl re-renders the sidebar
    // via useUrlVersion (mirrors the vanilla route()-driven re-render).
    navigateUrl(composeFolderTabsUrl(folder.children));
  };

  const onDeleteFolder = (e, id, folder) => {
    e.preventDefault();
    // Deleting a folder removes its children too; disarm if the armed
    // bookmark is one of them (mirrors the bookmark delete handler).
    const armed = getArmedBookmark();
    deleteFolder(id);
    if (armed && folder && folder.children.some((c) => c.id === armed.id)) {
      disarmBookmark();
      window.dispatchEvent(new Event("fused:urlchange"));
    }
    notifyBookmarksChanged();
  };

  // --- drag & drop -------------------------------------------------------------

  // Compute the active drop zone for a row given the dragged item, or null
  // when the drag should be ignored entirely. Zones: "above" | "below" | "into".
  const dropZone = (e, row, rowIsFolder, rowIsChild) => {
    // A folder cannot be dropped inside a folder.
    if (rowIsChild && draggedIsFolderRef.current) return null;
    const rect = row.getBoundingClientRect();
    const y = e.clientY - rect.top;
    // Combine (folder-creation / drop-into) only for a bookmark onto a
    // top-level bookmark or a folder — never inside a folder, never for folders.
    const combine = !draggedIsFolderRef.current && !rowIsChild;
    if (combine) {
      if (y < rect.height * 0.25) return "above";
      if (y > rect.height * 0.75) return "below";
      return "into";
    }
    return y > rect.height / 2 ? "below" : "above";
  };

  // Top-level reorder: move dragged to sit above/below the target row.
  const moveTopLevel = (targetId, below) => {
    let target = topOrder.indexOf(targetId) + (below ? 1 : 0);
    // Post-removal convention: a top-level dragged item earlier in the array
    // shifts every later index down by one. Items dragged out of a folder are
    // not in topOrder, so they need no adjustment.
    const from = topOrder.indexOf(draggedIdRef.current);
    if (from !== -1 && from < target) target -= 1;
    moveItem(draggedIdRef.current, null, target);
  };

  const clearDragClasses = () => {
    rowRefs.current.forEach((r) => {
      r.classList.remove("dragging", "drag-above", "drag-below", "drag-into");
    });
  };

  const onRowDragStart = (e, id, rowIsFolder) => {
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

  const onRowDragOver = (e, id, rowIsFolder, rowIsChild) => {
    if (draggedIdRef.current === null || draggedIdRef.current === id) return;
    const row = e.currentTarget;
    const zone = dropZone(e, row, rowIsFolder, rowIsChild);
    if (zone === null) return; // ignore (e.g. folder over a child row)
    e.preventDefault(); // required to allow a drop
    e.dataTransfer.dropEffect = "move";
    row.classList.toggle("drag-above", zone === "above");
    row.classList.toggle("drag-below", zone === "below");
    row.classList.toggle("drag-into", zone === "into");
  };

  const onRowDragLeave = (e) => {
    e.currentTarget.classList.remove("drag-above", "drag-below", "drag-into");
  };

  const onRowDrop = (e, id, rowIsFolder, rowIsChild) => {
    if (draggedIdRef.current === null || draggedIdRef.current === id) return;
    const row = e.currentTarget;
    const zone = dropZone(e, row, rowIsFolder, rowIsChild);
    if (zone === null) return;
    e.preventDefault();
    const below = zone === "below";

    if (zone === "into" && !rowIsFolder) {
      // Bookmark onto a top-level bookmark: make a folder of the two, then
      // immediately rename it.
      const folderId = createFolderWith(id, draggedIdRef.current);
      draggedIdRef.current = null;
      draggedIsFolderRef.current = false;
      notifyBookmarksChanged();
      if (folderId) setRenamingId(folderId);
      return;
    }

    if (zone === "into" && rowIsFolder) {
      // Bookmark into a folder: append to its children.
      const folder = folderById.get(id);
      const inThisFolder = folder && folder.children.some((c) => c.id === draggedIdRef.current);
      const targetIndex = (folder ? folder.children.length : 0) - (inThisFolder ? 1 : 0);
      moveItem(draggedIdRef.current, id, targetIndex);
    } else if (rowIsChild) {
      // Reorder within the target's folder.
      const parentId = row.getAttribute("data-parent");
      const folder = folderById.get(parentId);
      const childOrder = folder ? folder.children.map((c) => c.id) : [];
      let index = childOrder.indexOf(id) + (below ? 1 : 0);
      const from = childOrder.indexOf(draggedIdRef.current);
      if (from !== -1 && from < index) index -= 1; // dragged in same folder, earlier
      moveItem(draggedIdRef.current, parentId, index);
    } else {
      // Top-level reorder (target is a top-level bookmark or a folder row).
      moveTopLevel(id, below);
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

  const dragProps = (id, rowIsFolder, rowIsChild) => ({
    onDragStart: (e) => onRowDragStart(e, id, rowIsFolder),
    onDragOver: (e) => onRowDragOver(e, id, rowIsFolder, rowIsChild),
    onDragLeave: onRowDragLeave,
    onDrop: (e) => onRowDrop(e, id, rowIsFolder, rowIsChild),
    onDragEnd: onRowDragEnd,
  });

  return (
    <nav id="sidebar">
      <div className="sidebar-brand">
        <span className="logo">✦</span> fused-render
      </div>
      <div className="sidebar-section">
        <a href="#" id="home-link" className="sidebar-item" onClick={onHomeClick}>
          <span className="icon">🏠</span> Home
        </a>
      </div>
      <div className="sidebar-section sidebar-bookmarks">
        <div className="sidebar-heading">Bookmarks</div>
        {items.length === 0 ? (
          <div className="sidebar-empty">No bookmarks yet</div>
        ) : (
          items.map((it) => {
            if (isFolder(it)) {
              const activeHint = it.collapsed && it.children.some((c) => c.url === currentUrl());
              return (
                <React.Fragment key={it.id}>
                  <FolderRow
                    folder={it}
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
                    dragProps={dragProps(it.id, true, false)}
                  />
                  {!it.collapsed && (
                    <div className="folder-children">
                      {it.children.map((c) => (
                        <BookmarkRow
                          key={c.id}
                          b={c}
                          child
                          parentId={it.id}
                          isRenaming={renamingId === c.id}
                          registerRef={registerRow(c.id)}
                          onNameClick={(e) => onBookmarkNameClick(e, c)}
                          onRename={(e) => onRenameBookmark(e, c.id)}
                          onDelete={(e) => onDeleteBookmark(e, c.id)}
                          onCommitRename={(value) => commitRename(c.id, value, c.name)}
                          onCancelRename={cancelRename}
                          onMouseEnter={(e) => onRowMouseEnter(e, c)}
                          onMouseLeave={hideTooltip}
                          dragProps={dragProps(c.id, false, true)}
                        />
                      ))}
                    </div>
                  )}
                </React.Fragment>
              );
            }
            return (
              <BookmarkRow
                key={it.id}
                b={it}
                isRenaming={renamingId === it.id}
                registerRef={registerRow(it.id)}
                onNameClick={(e) => onBookmarkNameClick(e, it)}
                onRename={(e) => onRenameBookmark(e, it.id)}
                onDelete={(e) => onDeleteBookmark(e, it.id)}
                onCommitRename={(value) => commitRename(it.id, value, it.name)}
                onCancelRename={cancelRename}
                onMouseEnter={(e) => onRowMouseEnter(e, it)}
                onMouseLeave={hideTooltip}
                dragProps={dragProps(it.id, false, false)}
              />
            );
          })
        )}
      </div>
      <div id="bookmark-tooltip" ref={tooltipRef} style={hover ? { display: "block" } : undefined}>
        {hover && <TooltipContent bookmark={hover.bookmark} />}
      </div>
    </nav>
  );
}
