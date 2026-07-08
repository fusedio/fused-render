// Server API wrappers. Non-ok responses throw with the server's error message.
export interface Config {
  start_dir: string;
  home: string;
}

export interface FsEntry {
  name: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
}

export interface ListResult {
  path: string;
  entries: FsEntry[];
}

// One entry from GET /api/fs/walk. `rel` is a posix path relative to the
// walked directory; dir entries carry size null (same convention as FsEntry).
export interface WalkEntry {
  rel: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
}

export interface WalkResult {
  path: string;
  entries: WalkEntry[];
  truncated: boolean; // hit the server's entry cap
}

// One entry per resolved template mode (SPEC PT-8), in order, first = default.
// path is null for a sentinel mode (PT-12, e.g. "_render") — no template
// folder backs it, the shell knows what to do from the mode name alone.
export interface TemplateEntry {
  mode: string;
  path: string | null;
  icon: string | null;
}

export interface StatResult {
  path: string;
  name: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
  templates: TemplateEntry[];
  template_error?: string;
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data as T;
}

async function putJson<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "PUT",
    // X-Fused forces a CORS preflight so a foreign page can't write blind,
    // same D3 guard as the reveal/write POSTs.
    headers: { "Content-Type": "application/json", "X-Fused": "1" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data as T;
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    // X-Fused forces a CORS preflight so a foreign page can't write blind,
    // same D3 guard as putJson.
    headers: { "Content-Type": "application/json", "X-Fused": "1" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data as T;
}

export function getConfig(): Promise<Config> {
  return getJson<Config>("/api/config");
}

export function listDir(fsPath: string): Promise<ListResult> {
  return getJson<ListResult>("/api/fs/list?path=" + encodeURIComponent(fsPath));
}

export function walkDir(fsPath: string, opts?: { hidden?: boolean }): Promise<WalkResult> {
  let url = "/api/fs/walk?path=" + encodeURIComponent(fsPath);
  if (opts?.hidden) url += "&hidden=1";
  return getJson<WalkResult>(url);
}

export function statPath(fsPath: string): Promise<StatResult> {
  return getJson<StatResult>("/api/fs/stat?path=" + encodeURIComponent(fsPath));
}

export function rawUrl(fsPath: string): string {
  return "/api/fs/raw?path=" + encodeURIComponent(fsPath);
}

// Bookmark store (server-side, ~/.fused-render/bookmarks.json). The tree shape
// is BookmarkItem[] (lib/bookmarks.ts); kept as unknown[] here so api.ts has no
// dependency on the bookmark data layer. `exists` is false only until the file
// is first written — the shell's one-time localStorage-import gate.
export interface BookmarksResult {
  exists: boolean;
  bookmarks: unknown[];
}

export function getBookmarks(): Promise<BookmarksResult> {
  return getJson<BookmarksResult>("/api/bookmarks");
}

export function putBookmarks(bookmarks: unknown[]): Promise<void> {
  return putJson<unknown>("/api/bookmarks", bookmarks).then(() => undefined);
}

export interface BookmarkHistoryEntry {
  id: string;
  url: string;
  name?: string;
  created_at?: number;
  icon?: string;
}

// Best-effort: append/refresh this bookmark in its target file's .html.json
// sidecar (bookmarkHistory). Server no-ops for sentinel/dir-gone/non-file urls.
export function recordBookmarkHistory(entry: BookmarkHistoryEntry): Promise<void> {
  return postJson<unknown>("/api/bookmarks/history", entry).then(() => undefined);
}

// Per-file session restore (LSN-*). `search` is the shell query without the
// leading "?", stored verbatim in the target file's .html.json sidecar.
export interface LastSession {
  search: string;
  updated_at: number;
}

export function getSession(fsPath: string): Promise<{ lastSession: LastSession | null }> {
  return getJson("/api/session?path=" + encodeURIComponent(fsPath));
}

export function putSession(fsPath: string, search: string): Promise<void> {
  return putJson<unknown>("/api/session", { path: fsPath, search }).then(() => undefined);
}
