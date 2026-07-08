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

export function getConfig(): Promise<Config> {
  return getJson<Config>("/api/config");
}

export function listDir(fsPath: string): Promise<ListResult> {
  return getJson<ListResult>("/api/fs/list?path=" + encodeURIComponent(fsPath));
}

export function walkDir(fsPath: string): Promise<WalkResult> {
  return getJson<WalkResult>("/api/fs/walk?path=" + encodeURIComponent(fsPath));
}

export function statPath(fsPath: string): Promise<StatResult> {
  return getJson<StatResult>("/api/fs/stat?path=" + encodeURIComponent(fsPath));
}

export function rawUrl(fsPath: string): string {
  return "/api/fs/raw?path=" + encodeURIComponent(fsPath);
}
