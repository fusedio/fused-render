// Bookmark store, persisted server-side at ~/.fused-render/bookmarks.json via
// GET/PUT /api/bookmarks (superseded localStorage; DECISIONS D21 -> D74). Pure
// data layer — no DOM, no React.
//
// Reads are synchronous off an in-memory cache (React renders can't await);
// hydrateBookmarks() fills it once at boot. Mutations are async: they apply to
// a clone, `await` the whole-tree PUT, and only then advance the cache — so a
// failed write never leaves the UI showing an unpersisted state (no optimism,
// no rollback; a localhost PUT of this tiny tree is sub-millisecond). UI
// components still subscribe via useBookmarksVersion (lib/hooks.ts) and call
// notifyBookmarksChanged() after each mutation they trigger.
import { getBookmarks, putBookmarks } from "./api";

// Legacy localStorage key — read once for the one-time server import, then left
// dormant as a fallback (never written again).
const LS_KEY = "fused.bookmarks";

export interface Bookmark {
  id: string;
  name: string;
  url: string;
  created_at: number;
  icon?: string; // single emoji; absent -> default ★ glyph
  type?: undefined; // discriminant vs BookmarkFolder
}

export interface BookmarkFolder {
  id: string;
  type: "folder";
  name: string;
  collapsed: boolean;
  children: Bookmark[];
}

export type BookmarkItem = Bookmark | BookmarkFolder;

// In-memory mirror of the server file. Empty until hydrateBookmarks() resolves;
// the cache only advances after a successful PUT, so it never holds unsaved
// state. loadBookmarks() hands out this live array — callers must treat it as
// read-only (mutators clone before changing).
let cache: BookmarkItem[] = [];

export function loadBookmarks(): BookmarkItem[] {
  return cache;
}

const clone = (items: BookmarkItem[]): BookmarkItem[] =>
  JSON.parse(JSON.stringify(items));

// Persist a new tree, then advance the cache (order matters: on PUT failure the
// cache is untouched and the mutation rejects, so the UI stays consistent).
async function commit(items: BookmarkItem[]): Promise<void> {
  await putBookmarks(items);
  cache = items;
}

// Old localStorage tree, or null if absent/empty/corrupt. Only a non-empty
// array is worth importing.
function readLegacy(): BookmarkItem[] | null {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) && parsed.length ? parsed : null;
  } catch {
    return null;
  }
}

// Load the cache from the server once at boot. If the file was never written
// (`exists` false) and legacy localStorage has data, import it once (PUT it so
// `exists` flips true and this never re-imports — even after the user later
// deletes every bookmark). Failure leaves the cache empty; a later mutation
// still tries to persist.
export async function hydrateBookmarks(): Promise<void> {
  try {
    const { exists, bookmarks } = await getBookmarks();
    if (!exists) {
      const legacy = readLegacy();
      if (legacy) {
        await commit(legacy);
        return;
      }
    }
    cache = bookmarks as BookmarkItem[];
  } catch (e) {
    console.error("[fused] failed to load bookmarks:", e);
  }
}

export function isFolder(item: BookmarkItem): item is BookmarkFolder {
  return item.type === "folder";
}

// Flatten to bookmarks only (no folders): top-level bookmarks and all folder
// children, in display order.
export function allBookmarks(): Bookmark[] {
  const out: Bookmark[] = [];
  for (const item of cache) {
    if (isFolder(item)) out.push(...item.children);
    else out.push(item);
  }
  return out;
}

// Remove folders left empty by a mutation. Mutates in place, returns items.
function prune(items: BookmarkItem[]): BookmarkItem[] {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (isFolder(item) && item.children.length === 0) items.splice(i, 1);
  }
  return items;
}

export async function addBookmark(name: string, url: string): Promise<void> {
  const items = clone(cache);
  items.push({ id: crypto.randomUUID(), name, url, created_at: Date.now() });
  await commit(items);
}

export async function deleteBookmark(id: string): Promise<void> {
  const items = clone(cache);
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
  await commit(prune(items));
}

// Remove a folder (and its children) entirely.
export async function deleteFolder(id: string): Promise<void> {
  await commit(clone(cache).filter((it) => it.id !== id));
}

// Rename a bookmark or a folder. Top-level items are searched first, then
// folder children.
export async function renameBookmark(id: string, name: string): Promise<void> {
  const items = clone(cache);
  let target: BookmarkItem | undefined = items.find((it) => it.id === id);
  if (!target) {
    for (const item of items) {
      if (!isFolder(item)) continue;
      target = item.children.find((b) => b.id === id);
      if (target) break;
    }
  }
  if (!target) return; // nothing to change -> no write
  target.name = name;
  await commit(items);
}

// Move an item (bookmark or folder) to a new position. parentId null = top
// level; otherwise the id of the destination folder. targetIndex is the index
// in the destination array AFTER the moved item is removed; the caller is
// responsible for that convention. Folders can only move to the top level.
export async function moveItem(
  id: string,
  parentId: string | null,
  targetIndex: number
): Promise<void> {
  const items = clone(cache);

  // Locate the item first so a folder→folder move can abort before removal.
  let moved: BookmarkItem | undefined = items.find((it) => it.id === id);
  const isFolderItem = moved !== undefined && isFolder(moved);
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
    if (!dest || !isFolder(dest)) return; // destination gone; bail without saving so nothing is lost
    dest.children.splice(targetIndex, 0, moved as Bookmark);
  }
  await commit(prune(items));
}

// Replace top-level bookmark targetId with a new folder containing
// [target, dragged], preserving the target's slot. Returns the folder id
// (or null if either lookup fails). draggedId is removed from wherever it
// lives (top level or another folder), then the folder is created.
export async function createFolderWith(
  targetId: string,
  draggedId: string
): Promise<string | null> {
  const items = clone(cache);
  const targetIdx = items.findIndex((it) => it.id === targetId && !isFolder(it));
  if (targetIdx === -1) return null;

  // Remove dragged from top level or a folder's children.
  let dragged = items.find((it) => it.id === draggedId && !isFolder(it)) as Bookmark | undefined;
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
  const target = items.find((it) => it.id === targetId) as Bookmark;
  const at = items.indexOf(target);
  const folder: BookmarkFolder = {
    id: crypto.randomUUID(),
    type: "folder",
    name: "New folder",
    collapsed: false,
    children: [target, dragged],
  };
  items.splice(at, 1, folder);
  await commit(prune(items));
  return folder.id;
}

export async function toggleFolder(id: string): Promise<void> {
  const items = clone(cache);
  const folder = items.find((it) => it.id === id && isFolder(it));
  if (!folder || !isFolder(folder)) return;
  folder.collapsed = !folder.collapsed;
  await commit(items);
}

export async function updateBookmarkUrl(id: string, url: string): Promise<void> {
  const items = clone(cache);
  let bookmark = items.find((it) => it.id === id && !isFolder(it)) as Bookmark | undefined;
  if (!bookmark) {
    for (const item of items) {
      if (!isFolder(item)) continue;
      bookmark = item.children.find((b) => b.id === id);
      if (bookmark) break;
    }
  }
  if (bookmark) {
    bookmark.url = url;
    await commit(items);
  }
}

// Set or clear (icon = null) a bookmark's emoji icon. Bookmarks only —
// folders keep the themed folder glyph.
export async function setBookmarkIcon(id: string, icon: string | null): Promise<void> {
  const items = clone(cache);
  let bookmark = items.find((it) => it.id === id && !isFolder(it)) as Bookmark | undefined;
  if (!bookmark) {
    for (const item of items) {
      if (!isFolder(item)) continue;
      bookmark = item.children.find((b) => b.id === id);
      if (bookmark) break;
    }
  }
  if (bookmark) {
    if (icon === null) delete bookmark.icon;
    else bookmark.icon = icon;
    await commit(items);
  }
}

// Armed-bookmark tracking on sessionStorage. Records the bookmark being
// "followed" so the shell can offer to update its saved url when the current
// params diverge. `url` is the SAVED bookmark url at arm/update time.
const ARMED_KEY = "fused.armedBookmark";

export interface ArmedBookmark {
  id: string;
  url: string;
}

export function armBookmark(id: string, url: string): void {
  try {
    sessionStorage.setItem(ARMED_KEY, JSON.stringify({ id, url }));
  } catch (e) {
    console.error("[fused] failed to arm bookmark:", e);
  }
}

export function disarmBookmark(): void {
  try {
    sessionStorage.removeItem(ARMED_KEY);
  } catch (e) {
    console.error("[fused] failed to disarm bookmark:", e);
  }
}

export function getArmedBookmark(): ArmedBookmark | null {
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
