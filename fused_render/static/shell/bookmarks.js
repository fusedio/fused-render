// Bookmark store on localStorage. Pure data layer — no DOM. UI (sidebar.js)
// subscribes by re-rendering after each mutation it triggers.
const KEY = "fused.bookmarks";

export function loadBookmarks() {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return []; // corrupt JSON -> treat as empty; next save overwrites it
  }
}

function save(bookmarks) {
  try {
    localStorage.setItem(KEY, JSON.stringify(bookmarks));
  } catch (e) {
    console.error("[fused] failed to save bookmarks:", e);
  }
}

export function isFolder(item) {
  return item.type === "folder";
}

// Flatten to bookmarks only (no folders): top-level bookmarks and all folder
// children, in display order.
export function allBookmarks() {
  const out = [];
  for (const item of loadBookmarks()) {
    if (isFolder(item)) out.push(...item.children);
    else out.push(item);
  }
  return out;
}

// Remove folders left empty by a mutation. Mutates in place, returns items.
function prune(items) {
  for (let i = items.length - 1; i >= 0; i--) {
    if (isFolder(items[i]) && items[i].children.length === 0) items.splice(i, 1);
  }
  return items;
}

export function addBookmark(name, url) {
  const bookmarks = loadBookmarks();
  bookmarks.push({ id: crypto.randomUUID(), name, url, created_at: Date.now() });
  save(bookmarks);
}

export function deleteBookmark(id) {
  const items = loadBookmarks();
  const at = items.findIndex((it) => it.id === id);
  if (at !== -1) {
    items.splice(at, 1);
  } else {
    // Not top-level: search folder children.
    for (const item of items) {
      if (!isFolder(item)) continue;
      const ci = item.children.findIndex((b) => b.id === id);
      if (ci !== -1) {
        item.children.splice(ci, 1);
        break;
      }
    }
  }
  save(prune(items));
}

// Remove a folder (and its children) entirely.
export function deleteFolder(id) {
  save(loadBookmarks().filter((it) => it.id !== id));
}

// Rename a bookmark or a folder. Top-level items are searched first, then
// folder children.
export function renameBookmark(id, name) {
  const items = loadBookmarks();
  let target = items.find((it) => it.id === id);
  if (!target) {
    for (const item of items) {
      if (!isFolder(item)) continue;
      target = item.children.find((b) => b.id === id);
      if (target) break;
    }
  }
  if (target) target.name = name;
  save(items);
}

// Move an item (bookmark or folder) to a new position. parentId null = top
// level; otherwise the id of the destination folder. targetIndex is the index
// in the destination array AFTER the moved item is removed; the caller is
// responsible for that convention. Folders can only move to the top level.
export function moveItem(id, parentId, targetIndex) {
  const items = loadBookmarks();

  // Locate the item first so a folder→folder move can abort before removal.
  let moved = items.find((it) => it.id === id);
  let isFolderItem = moved && isFolder(moved);
  if (isFolderItem && parentId !== null) return;

  // Remove from top level or whichever folder holds it.
  if (moved) {
    items.splice(items.indexOf(moved), 1);
  } else {
    for (const item of items) {
      if (!isFolder(item)) continue;
      const ci = item.children.findIndex((b) => b.id === id);
      if (ci !== -1) {
        [moved] = item.children.splice(ci, 1);
        break;
      }
    }
  }
  if (!moved) return;

  if (parentId === null) {
    items.splice(targetIndex, 0, moved);
  } else {
    const dest = items.find((it) => it.id === parentId && isFolder(it));
    if (!dest) return; // destination gone; bail without saving so nothing is lost
    dest.children.splice(targetIndex, 0, moved);
  }
  save(prune(items));
}

// Replace top-level bookmark targetId with a new folder containing
// [target, dragged], preserving the target's slot. Returns the folder id
// (or null if either lookup fails). draggedId is removed from wherever it
// lives (top level or another folder), then the folder is created.
export function createFolderWith(targetId, draggedId) {
  const items = loadBookmarks();
  const targetIdx = items.findIndex((it) => it.id === targetId && !isFolder(it));
  if (targetIdx === -1) return null;

  // Remove dragged from top level or a folder's children.
  let dragged = items.find((it) => it.id === draggedId && !isFolder(it));
  if (dragged) {
    items.splice(items.indexOf(dragged), 1);
  } else {
    for (const item of items) {
      if (!isFolder(item)) continue;
      const ci = item.children.findIndex((b) => b.id === draggedId);
      if (ci !== -1) {
        [dragged] = item.children.splice(ci, 1);
        break;
      }
    }
  }
  if (!dragged) return null;

  // Target index may have shifted if dragged was an earlier top-level item.
  const target = items.find((it) => it.id === targetId);
  const at = items.indexOf(target);
  const folder = {
    id: crypto.randomUUID(),
    type: "folder",
    name: "New folder",
    collapsed: false,
    children: [target, dragged],
  };
  items.splice(at, 1, folder);
  save(prune(items));
  return folder.id;
}

export function toggleFolder(id) {
  const items = loadBookmarks();
  const folder = items.find((it) => it.id === id && isFolder(it));
  if (folder) folder.collapsed = !folder.collapsed;
  save(items);
}

export function updateBookmarkUrl(id, url) {
  const items = loadBookmarks();
  let bookmark = items.find((it) => it.id === id && !isFolder(it));
  if (!bookmark) {
    for (const item of items) {
      if (!isFolder(item)) continue;
      bookmark = item.children.find((b) => b.id === id);
      if (bookmark) break;
    }
  }
  if (bookmark) {
    bookmark.url = url;
    save(items);
  }
}

// Armed-bookmark tracking on sessionStorage. Records the bookmark being
// "followed" so the shell can offer to update its saved url when the current
// params diverge. `url` is the SAVED bookmark url at arm/update time.
const ARMED_KEY = "fused.armedBookmark";

export function armBookmark(id, url) {
  try {
    sessionStorage.setItem(ARMED_KEY, JSON.stringify({ id, url }));
  } catch (e) {
    console.error("[fused] failed to arm bookmark:", e);
  }
}

export function disarmBookmark() {
  try {
    sessionStorage.removeItem(ARMED_KEY);
  } catch (e) {
    console.error("[fused] failed to disarm bookmark:", e);
  }
}

export function getArmedBookmark() {
  try {
    const raw = sessionStorage.getItem(ARMED_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.id === "string" && typeof parsed.url === "string") {
      return parsed;
    }
    return null;
  } catch {
    return null; // corrupt JSON -> treat as not armed
  }
}
