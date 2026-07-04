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
