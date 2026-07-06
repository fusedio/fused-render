// Sidebar UI: brand, Home entry, bookmark rows with hover card + inline rename.
import { navigate, navigateUrl, currentUrl, VIEW_PREFIX } from "./router.js";
import { escapeHtml } from "./format.js";
import {
  loadBookmarks,
  allBookmarks,
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
} from "./bookmarks.js";

const sidebarEl = document.getElementById("sidebar");

let config = null;
let draggedId = null; // id of the row currently being dragged (bookmark or folder)
let draggedIsFolder = false; // whether the dragged item is a folder

export function initSidebar(cfg) {
  config = cfg;
}

// Hover card showing a bookmark's target path + saved params.
const tooltipEl = document.createElement("div");
tooltipEl.id = "bookmark-tooltip";
document.body.appendChild(tooltipEl);

function showBookmarkTooltip(row, bookmark) {
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
  const paramsHtml = params.length
    ? `<div class="tip-params">${params
        .map(([k, v]) => `<span class="tip-key">${escapeHtml(k)}</span><span class="tip-val">${escapeHtml(v)}</span>`)
        .join("")}</div>`
    : `<div class="tip-none">no params</div>`;

  tooltipEl.innerHTML = `<div class="tip-path">${escapeHtml(fsPath)}</div>${paramsHtml}`;
  tooltipEl.style.display = "block";
  const rect = row.getBoundingClientRect();
  tooltipEl.style.left = `${rect.right + 8}px`;
  const top = Math.min(rect.top, window.innerHeight - tooltipEl.offsetHeight - 12);
  tooltipEl.style.top = `${Math.max(8, top)}px`;
}

function hideBookmarkTooltip() {
  tooltipEl.style.display = "none";
}

// Template for a bookmark row (top-level or, with child=true, inside a folder).
function bookmarkRowHtml(b, { child, parentId } = {}) {
  return `
      <div class="bookmark-row${child ? " child-row" : ""}${b.url === currentUrl() ? " active" : ""}" data-id="${escapeHtml(b.id)}"${child ? ` data-parent="${escapeHtml(parentId)}"` : ""} draggable="true">
        <span class="bookmark-glyph">&#9733;</span>
        <a class="bookmark-name" href="${escapeHtml(b.url)}" draggable="false">${escapeHtml(b.name)}</a>
        <span class="bookmark-actions">
          <button class="icon-btn rename-btn" title="Rename">&#9998;</button>
          <button class="icon-btn delete-btn" title="Delete">&#10005;</button>
        </span>
      </div>`;
}

// Folder shape drawn inline so it inherits currentColor — an emoji folder
// ignores the theme and looks heavy at this size.
const FOLDER_ICON = `<svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M1.5 4A1.5 1.5 0 0 1 3 2.5h3.1c.4 0 .78.16 1.06.44l.8.8c.1.1.22.16.35.16H13A1.5 1.5 0 0 1 14.5 5.4V12A1.5 1.5 0 0 1 13 13.5H3A1.5 1.5 0 0 1 1.5 12V4z"/></svg>`;

// activeHint: folder is collapsed but holds the current view's bookmark —
// highlight the row so the selection isn't invisible while folded away.
function folderRowHtml(f, activeHint) {
  return `
      <div class="bookmark-row folder-row${f.collapsed ? " collapsed" : ""}${activeHint ? " active" : ""}" data-id="${escapeHtml(f.id)}" draggable="true">
        <span class="bookmark-glyph folder-glyph">${FOLDER_ICON}</span>
        <span class="bookmark-name folder-name">${escapeHtml(f.name)}</span>
        <span class="folder-count">${f.children.length}</span>
        <span class="bookmark-actions">
          <button class="icon-btn rename-btn" title="Rename">&#9998;</button>
          <button class="icon-btn delete-btn" title="Delete folder and contents">&#10005;</button>
        </span>
      </div>`;
}

export function renderSidebar() {
  const items = loadBookmarks(); // top-level items: bookmarks and folders
  const rows = items
    .map((it) => {
      if (isFolder(it)) {
        // Children live in a wrapper that draws the indent rail; the wrapper
        // has no handlers, so per-row drag/click wiring is unaffected.
        const children = it.collapsed
          ? ""
          : `<div class="folder-children">${it.children.map((c) => bookmarkRowHtml(c, { child: true, parentId: it.id })).join("")}</div>`;
        const activeHint = it.collapsed && it.children.some((c) => c.url === currentUrl());
        return folderRowHtml(it, activeHint) + children;
      }
      return bookmarkRowHtml(it);
    })
    .join("");

  sidebarEl.innerHTML = `
    <div class="sidebar-brand"><span class="logo">&#10022;</span> fused-render</div>
    <div class="sidebar-section">
      <a href="#" id="home-link" class="sidebar-item"><span class="icon">&#127968;</span> Home</a>
    </div>
    <div class="sidebar-section sidebar-bookmarks">
      <div class="sidebar-heading">Bookmarks</div>
      ${rows || `<div class="sidebar-empty">No bookmarks yet</div>`}
    </div>`;

  document.getElementById("home-link").addEventListener("click", (e) => {
    e.preventDefault();
    if (config && config.home) navigate(config.home);
  });

  // Lookups spanning the whole tree: bookmarks (top-level + children) and folders.
  const bookmarkById = new Map(allBookmarks().map((b) => [b.id, b]));
  const folderById = new Map(items.filter(isFolder).map((f) => [f.id, f]));
  const topOrder = items.map((it) => it.id); // top-level display order

  sidebarEl.querySelectorAll(".bookmark-row").forEach((row) => {
    const id = row.getAttribute("data-id");
    const rowIsFolder = row.classList.contains("folder-row");
    const rowIsChild = row.classList.contains("child-row");

    if (rowIsFolder) {
      wireFolderRow(row, id, folderById.get(id));
    } else {
      wireBookmarkRow(row, id, bookmarkById.get(id));
    }
    wireDrag(row, id, { rowIsFolder, rowIsChild, folderById, topOrder });
  });

  syncStarButton();
}

// Click/rename/delete/hover handlers for a bookmark row (top-level or child).
function wireBookmarkRow(row, id, bookmark) {
  row.querySelector(".bookmark-name").addEventListener("click", (e) => {
    // Open the bookmark and arm it for tracking. href is kept for
    // middle-click / copy-link, but a plain click routes in-shell.
    e.preventDefault();
    hideBookmarkTooltip();
    armBookmark(bookmark.id, bookmark.url);
    navigateUrl(bookmark.url);
  });
  row.querySelector(".delete-btn").addEventListener("click", (e) => {
    e.preventDefault();
    hideBookmarkTooltip();
    const armed = getArmedBookmark();
    deleteBookmark(id);
    if (armed && armed.id === id) {
      disarmBookmark();
      // No breadcrumb import (one-way dep rule); let main.js re-sync.
      window.dispatchEvent(new Event("fused:urlchange"));
    }
    renderSidebar();
  });
  row.querySelector(".rename-btn").addEventListener("click", (e) => {
    e.preventDefault();
    hideBookmarkTooltip();
    startRename(row, id);
  });
  row.addEventListener("mouseenter", () => {
    // No tooltip while renaming this row or while a drag is in progress.
    if (draggedId !== null) return;
    if (!row.querySelector(".bookmark-rename-input")) showBookmarkTooltip(row, bookmark);
  });
  row.addEventListener("mouseleave", hideBookmarkTooltip);
}

// Chevron/name toggle + rename/delete handlers for a folder row.
function wireFolderRow(row, id, folder) {
  const toggle = (e) => {
    e.preventDefault();
    toggleFolder(id);
    renderSidebar();
  };
  row.querySelector(".folder-glyph").addEventListener("click", toggle);
  row.querySelector(".folder-name").addEventListener("click", toggle);
  row.querySelector(".rename-btn").addEventListener("click", (e) => {
    e.preventDefault();
    startRename(row, id);
  });
  row.querySelector(".delete-btn").addEventListener("click", (e) => {
    e.preventDefault();
    // Deleting a folder removes its children too; disarm if the armed
    // bookmark is one of them (mirrors the bookmark delete handler).
    const armed = getArmedBookmark();
    deleteFolder(id);
    if (armed && folder && folder.children.some((c) => c.id === armed.id)) {
      disarmBookmark();
      window.dispatchEvent(new Event("fused:urlchange"));
    }
    renderSidebar();
  });
  // Folders show no tooltip.
}

// Compute the active drop zone for a row given the dragged item, or null when
// the drag should be ignored entirely. Zones: "above" | "below" | "into".
function dropZone(e, row, rowIsFolder, rowIsChild) {
  // A folder cannot be dropped inside a folder.
  if (rowIsChild && draggedIsFolder) return null;
  const rect = row.getBoundingClientRect();
  const y = e.clientY - rect.top;
  // Combine (folder-creation / drop-into) only for a bookmark onto a
  // top-level bookmark or a folder — never inside a folder, never for folders.
  const combine = !draggedIsFolder && !rowIsChild;
  if (combine) {
    if (y < rect.height * 0.25) return "above";
    if (y > rect.height * 0.75) return "below";
    return "into";
  }
  return y > rect.height / 2 ? "below" : "above";
}

// Top-level reorder: move dragged to sit above/below the target row.
function moveTopLevel(targetId, below, topOrder) {
  let target = topOrder.indexOf(targetId) + (below ? 1 : 0);
  // Post-removal convention: a top-level dragged item earlier in the array
  // shifts every later index down by one. Items dragged out of a folder are
  // not in topOrder, so they need no adjustment.
  const from = topOrder.indexOf(draggedId);
  if (from !== -1 && from < target) target -= 1;
  moveItem(draggedId, null, target);
}

function wireDrag(row, id, ctx) {
  const { rowIsFolder, rowIsChild, folderById, topOrder } = ctx;

  row.addEventListener("dragstart", (e) => {
    // No drag while renaming — let the input keep native text selection.
    if (row.querySelector(".bookmark-rename-input")) {
      e.preventDefault();
      return;
    }
    draggedId = id;
    draggedIsFolder = rowIsFolder;
    row.classList.add("dragging");
    hideBookmarkTooltip();
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", id); // Firefox needs data set to start a drag
  });
  row.addEventListener("dragover", (e) => {
    if (draggedId === null || draggedId === id) return;
    const zone = dropZone(e, row, rowIsFolder, rowIsChild);
    if (zone === null) return; // ignore (e.g. folder over a child row)
    e.preventDefault(); // required to allow a drop
    e.dataTransfer.dropEffect = "move";
    row.classList.toggle("drag-above", zone === "above");
    row.classList.toggle("drag-below", zone === "below");
    row.classList.toggle("drag-into", zone === "into");
  });
  row.addEventListener("dragleave", () => {
    row.classList.remove("drag-above", "drag-below", "drag-into");
  });
  row.addEventListener("drop", (e) => {
    if (draggedId === null || draggedId === id) return;
    const zone = dropZone(e, row, rowIsFolder, rowIsChild);
    if (zone === null) return;
    e.preventDefault();
    const below = zone === "below";

    if (zone === "into" && !rowIsFolder) {
      // Bookmark onto a top-level bookmark: make a folder of the two, then
      // immediately rename it.
      const folderId = createFolderWith(id, draggedId);
      draggedId = null;
      draggedIsFolder = false;
      renderSidebar();
      if (folderId) {
        const folderRow = sidebarEl.querySelector(`.folder-row[data-id="${CSS.escape(folderId)}"]`);
        if (folderRow) startRename(folderRow, folderId);
      }
      return;
    }

    if (zone === "into" && rowIsFolder) {
      // Bookmark into a folder: append to its children.
      const folder = folderById.get(id);
      const inThisFolder = folder && folder.children.some((c) => c.id === draggedId);
      const targetIndex = (folder ? folder.children.length : 0) - (inThisFolder ? 1 : 0);
      moveItem(draggedId, id, targetIndex);
    } else if (rowIsChild) {
      // Reorder within the target's folder.
      const parentId = row.getAttribute("data-parent");
      const folder = folderById.get(parentId);
      const childOrder = folder ? folder.children.map((c) => c.id) : [];
      let index = childOrder.indexOf(id) + (below ? 1 : 0);
      const from = childOrder.indexOf(draggedId);
      if (from !== -1 && from < index) index -= 1; // dragged in same folder, earlier
      moveItem(draggedId, parentId, index);
    } else {
      // Top-level reorder (target is a top-level bookmark or a folder row).
      moveTopLevel(id, below, topOrder);
    }

    // Reset here, not just in dragend: renderSidebar() detaches the dragged
    // row, and Chrome skips dragend on a removed source element.
    draggedId = null;
    draggedIsFolder = false;
    renderSidebar();
  });
  row.addEventListener("dragend", () => {
    // Fires even on Escape-cancelled drags — the universal cleanup.
    draggedId = null;
    draggedIsFolder = false;
    sidebarEl.querySelectorAll(".bookmark-row").forEach((r) => {
      r.classList.remove("dragging", "drag-above", "drag-below", "drag-into");
    });
  });
}

export function syncStarButton() {
  const btn = document.getElementById("bookmark-btn");
  if (!btn) return;
  const starred = allBookmarks().some((b) => b.url === currentUrl());
  btn.classList.toggle("starred", starred);
  btn.title = starred ? "View is bookmarked (★ adds another)" : "Bookmark this view";
}

function startRename(row, id) {
  // Look up across the whole tree: top-level bookmarks, folders, and children.
  const item = loadBookmarks().find((it) => it.id === id) || allBookmarks().find((b) => b.id === id);
  if (!item) return;
  // Folder rows label with .folder-name; bookmark rows with .bookmark-name.
  const nameEl = row.querySelector(".folder-name") || row.querySelector(".bookmark-name");
  const input = document.createElement("input");
  input.type = "text";
  input.className = "bookmark-rename-input";
  input.value = item.name;
  nameEl.replaceWith(input);
  input.focus();
  input.select();

  let settled = false;
  const commit = () => {
    if (settled) return;
    settled = true;
    renameBookmark(id, input.value.trim() || item.name);
    renderSidebar();
  };
  const cancel = () => {
    if (settled) return;
    settled = true;
    renderSidebar();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancel();
    }
  });
  input.addEventListener("blur", commit);
}
