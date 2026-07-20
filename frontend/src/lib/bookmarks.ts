// Bookmark store, persisted server-side at ~/.fused-render/bookmarks.json via
// GET/PUT /api/bookmarks. Pure data layer — no DOM, no React.
//
// Reads are synchronous off an in-memory cache (React renders can't await);
// hydrateBookmarks() fills it once at boot. Mutations are async: they apply to
// a clone, `await` the whole-tree PUT, and only then advance the cache — so a
// failed write never leaves the UI showing an unpersisted state (no optimism,
// no rollback; a localhost PUT of this tiny tree is sub-millisecond). UI
// components still subscribe via useBookmarksVersion (lib/hooks.ts) and call
// notifyBookmarksChanged() after each mutation they trigger.
import { getBookmarks, putBookmarks, recordBookmarkHistory } from "./api";

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
  children: BookmarkItem[]; // folders may nest to any depth (D121)
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

// All cache access runs through one serial promise chain — hydration, every
// mutation, and the cross-tab refresh poll. So: (1) a mutation never reads a
// half-hydrated cache — an early click before the initial GET returns waits for
// the load instead of PUTting an empty tree over the file; (2) two overlapping
// mutations can't both clone the same snapshot and clobber each other — each
// runs after the previous one's commit; (3) the poll can't overwrite the cache
// mid-mutation. Cross-tab convergence is eventual (≤ the poll interval), still
// last-write-wins on simultaneous writes — D77.
let tail: Promise<unknown> = Promise.resolve();
let hydrated = false;

function enqueue<T>(op: () => Promise<T>): Promise<T> {
  const run = tail.then(op, op); // run regardless of a prior rejection
  tail = run.catch(() => {}); // keep the chain alive after a failed op
  return run;
}

// Load the cache from the server once at boot (idempotent; enqueued so it wins
// the race against any early mutation).
export function hydrateBookmarks(): Promise<void> {
  return enqueue(async () => {
    if (hydrated) return;
    try {
      const { exists, bookmarks } = await getBookmarks();
      cache = exists ? (bookmarks as BookmarkItem[]) : [];
      hydrated = true;
    } catch (e) {
      console.error("[fused] failed to load bookmarks:", e);
    }
  });
}

// Re-read the server tree to pick up another tab's writes (D77 poll). Enqueued
// so it never overwrites an in-flight local mutation; resolves true only when
// the tree actually changed, so the caller re-renders once every 30 s at most,
// not on every tick. No import logic — plain GET (hydrate owns the one-time
// import); a failed poll is logged and skipped.
export function refreshBookmarks(): Promise<boolean> {
  return enqueue(async () => {
    try {
      const { bookmarks } = await getBookmarks();
      const next = bookmarks as BookmarkItem[];
      if (JSON.stringify(next) === JSON.stringify(cache)) return false;
      cache = next;
      return true;
    } catch (e) {
      console.error("[fused] failed to refresh bookmarks:", e);
      return false;
    }
  });
}

export function isFolder(item: BookmarkItem): item is BookmarkFolder {
  return item.type === "folder";
}

// --- recursive tree helpers (D121) ------------------------------------------
// Folders nest arbitrarily, so every lookup/removal walks the whole tree.
// These are the single source of that walk; mutators below build on them.

function findById(items: BookmarkItem[], id: string): BookmarkItem | undefined {
  for (const item of items) {
    if (item.id === id) return item;
    if (isFolder(item)) {
      const hit = findById(item.children, id);
      if (hit) return hit;
    }
  }
  return undefined;
}

// Splice the item out of whichever children array holds it; returns it.
function removeById(items: BookmarkItem[], id: string): BookmarkItem | undefined {
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (item.id === id) {
      items.splice(i, 1);
      return item;
    }
    if (isFolder(item)) {
      const removed = removeById(item.children, id);
      if (removed) return removed;
    }
  }
  return undefined;
}

// The children array a move should insert into: the tree root for parentId
// null, else the named folder's children (undefined if it no longer exists).
function containerOf(items: BookmarkItem[], parentId: string | null): BookmarkItem[] | undefined {
  if (parentId === null) return items;
  const dest = findById(items, parentId);
  return dest && isFolder(dest) ? dest.children : undefined;
}

// True when `id` lives anywhere inside folder `ancestorId`'s subtree. Used as
// the cycle guard for folder moves (a folder must never become its own
// descendant) and exported for drag-over feedback in the sidebar.
export function isDescendant(items: BookmarkItem[], ancestorId: string, id: string): boolean {
  const ancestor = findById(items, ancestorId);
  return !!ancestor && isFolder(ancestor) && !!findById(ancestor.children, id);
}

// Flatten to bookmarks only (no folders), all depths, in display order.
export function allBookmarks(): Bookmark[] {
  const out: Bookmark[] = [];
  const walk = (list: BookmarkItem[]): void => {
    for (const item of list) {
      if (isFolder(item)) walk(item.children);
      else out.push(item);
    }
  };
  walk(cache);
  return out;
}

// Bookmark name -> filename stem: path separators, the colon (path-hostile on
// Windows, legacy-HFS on macOS) and control chars become "-". Lives here (not
// bookmark-file.ts) because uniqueness below keys on it; bookmark-file.ts
// imports it for the actual filename. Char class must stay in sync with
// _sanitize_stem in fused_render/shell/bookmarks.py.
export function sanitizeBookmarkStem(name: string): string {
  // eslint-disable-next-line no-control-regex
  return name.replace(/[/\\:\u0000-\u001f\u007f]/g, "-").trim();
}

// Uniqueness comparison key (D97): the sanitized filename stem, lowercased.
// Keying on the stem (not the raw name) makes `.bookmark` filename collisions
// impossible by construction — distinct names like `a/b` and `a:b` sanitize to
// the same `a-b` and therefore count as duplicates.
const nameKey = (name: string): string => sanitizeBookmarkStem(name).toLowerCase();

// Bookmark names are globally unique by sanitized-stem key (they become
// `<name>.bookmark` filenames — D97); folder names are a separate namespace.
// Returns `base` when free, else `base-1`, `base-2`, ... (first free suffix;
// "-" and digits survive sanitization, so suffixed keys stay distinct).
// `excludeId` skips the bookmark being renamed so a no-op rename isn't suffixed.
function uniqueNameIn(items: BookmarkItem[], base: string, excludeId?: string): string {
  const taken = new Set<string>();
  const collect = (list: BookmarkItem[]): void => {
    for (const it of list) {
      if (isFolder(it)) collect(it.children);
      else if (it.id !== excludeId) taken.add(nameKey(it.name));
    }
  };
  collect(items);
  if (!taken.has(nameKey(base))) return base;
  for (let n = 1; ; n++) {
    const candidate = `${base}-${n}`;
    if (!taken.has(nameKey(candidate))) return candidate;
  }
}

// Public preview against the current cache (mutations dedupe internally via
// uniqueNameIn on their own snapshot, so callers need not pre-clean names).
export function uniqueBookmarkName(base: string, excludeId?: string): string {
  return uniqueNameIn(cache, base, excludeId);
}

// Remove folders left empty by a mutation. Depth-first so an emptied nested
// folder disappears first and can in turn empty (and remove) its parent.
// Mutates in place, returns items.
function prune(items: BookmarkItem[]): BookmarkItem[] {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (!isFolder(item)) continue;
    prune(item.children);
    if (item.children.length === 0) items.splice(i, 1);
  }
  return items;
}

// Every mutation goes through here: `transform` receives a fresh clone of the
// current cache and returns the tree to persist, or null to abort with no write
// (a no-op lookup). Serialized via enqueue so the clone-read and the commit are
// one atomic step relative to other mutations and to hydration.
function mutate(transform: (items: BookmarkItem[]) => BookmarkItem[] | null): Promise<void> {
  return enqueue(async () => {
    const next = transform(clone(cache));
    if (next) await commit(next);
  });
}

export async function addBookmark(name: string, url: string): Promise<void> {
  const item: Bookmark = { id: crypto.randomUUID(), name, url, created_at: Date.now() };
  await mutate((items) => {
    item.name = uniqueNameIn(items, name); // dedupe against the same snapshot we push into
    items.push(item);
    return items;
  });
  // Fire-and-forget after the bookmark write commits, so sidecar I/O never
  // blocks or fails the bookmark itself.
  recordBookmarkHistory({ id: item.id, name: item.name, url, created_at: item.created_at })
    .catch((e) => console.error("[fused] failed to record bookmark history:", e));
}

export function deleteBookmark(id: string): Promise<void> {
  return mutate((items) => {
    removeById(items, id);
    return prune(items);
  });
}

// Remove a folder (and its whole subtree) entirely, wherever it nests.
export function deleteFolder(id: string): Promise<void> {
  return mutate((items) => {
    removeById(items, id);
    return prune(items); // removal may leave an emptied ancestor behind
  });
}

// Rename a bookmark or a folder, at any depth.
export function renameBookmark(id: string, name: string): Promise<void> {
  return mutate((items) => {
    const target = findById(items, id);
    if (!target) return null; // nothing to change -> no write
    // Folders keep their own namespace; bookmark names auto-suffix on clash.
    target.name = isFolder(target) ? name : uniqueNameIn(items, name, id);
    return items;
  });
}

// Move an item (bookmark or folder) to a new position. parentId null = top
// level; otherwise the id of the destination folder, at any depth. targetIndex
// is the index in the destination array AFTER the moved item is removed; the
// caller is responsible for that convention.
export function moveItem(
  id: string,
  parentId: string | null,
  targetIndex: number
): Promise<void> {
  return mutate((items) => {
    // Cycle guard: a folder dropped onto itself or anywhere inside its own
    // subtree would orphan the whole branch — abort before removal.
    if (parentId !== null) {
      const moved = findById(items, id);
      if (moved && isFolder(moved) && (parentId === id || isDescendant(items, id, parentId))) {
        return null;
      }
    }

    const moved = removeById(items, id);
    if (!moved) return null;

    // Destination resolved AFTER removal so a vanished folder (or one that
    // lived inside the moved subtree) bails without saving — nothing is lost.
    const dest = containerOf(items, parentId);
    if (!dest) return null;
    dest.splice(targetIndex, 0, moved);
    return prune(items);
  });
}

// Replace top-level bookmark targetId with a new folder containing
// [target, dragged], preserving the target's slot. Returns the folder id
// (or null if either lookup fails). draggedId is removed from wherever it
// lives (top level or another folder), then the folder is created.
export function createFolderWith(
  targetId: string,
  draggedId: string
): Promise<string | null> {
  return enqueue(async () => {
    const items = clone(cache);
    const targetIdx = items.findIndex((it) => it.id === targetId && !isFolder(it));
    if (targetIdx === -1) return null;

    // Remove dragged from wherever it lives — top level or any nested folder.
    const dragged = removeById(items, draggedId);
    if (!dragged || isFolder(dragged)) return null; // combine is bookmarks-only

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
  });
}

export function toggleFolder(id: string): Promise<void> {
  return mutate((items) => {
    const folder = findById(items, id);
    if (!folder || !isFolder(folder)) return null;
    folder.collapsed = !folder.collapsed;
    return items;
  });
}

export async function updateBookmarkUrl(id: string, url: string): Promise<void> {
  let name: string | undefined;
  let found = false;
  await mutate((items) => {
    const bookmark = findById(items, id);
    if (!bookmark || isFolder(bookmark)) return null;
    bookmark.url = url;
    name = bookmark.name;
    found = true;
    return items;
  });
  if (!found) return;
  // Record the new url for that id (server upserts, refreshing updated_at).
  recordBookmarkHistory({ id, url, name })
    .catch((e) => console.error("[fused] failed to record bookmark history:", e));
}

// Set or clear (icon = null) a bookmark's emoji icon. Bookmarks only —
// folders keep the themed folder glyph.
export function setBookmarkIcon(id: string, icon: string | null): Promise<void> {
  return mutate((items) => {
    const bookmark = findById(items, id);
    if (!bookmark || isFolder(bookmark)) return null;
    if (icon === null) delete bookmark.icon;
    else bookmark.icon = icon;
    return items;
  });
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
