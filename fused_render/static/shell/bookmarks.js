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

export function addBookmark(name, url) {
  const bookmarks = loadBookmarks();
  bookmarks.push({ id: crypto.randomUUID(), name, url, created_at: Date.now() });
  save(bookmarks);
}

export function deleteBookmark(id) {
  save(loadBookmarks().filter((b) => b.id !== id));
}

export function renameBookmark(id, name) {
  const bookmarks = loadBookmarks();
  const bookmark = bookmarks.find((b) => b.id === id);
  if (bookmark) bookmark.name = name;
  save(bookmarks);
}

export function updateBookmarkUrl(id, url) {
  const bookmarks = loadBookmarks();
  const bookmark = bookmarks.find((b) => b.id === id);
  if (bookmark) bookmark.url = url;
  save(bookmarks);
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
