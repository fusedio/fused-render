// Server API wrappers. Non-ok responses throw with the server's error message.
import { noteFsMutation, noteIndexLifecycle } from "@platform/lib/index-freshness";
import { outcomeFrom } from "@platform/lib/index-query";
import type { IndexQueryOutcome } from "@platform/lib/index-query";
import type { ModifiedInstall } from "@platform/lib/selffix";

export interface Config {
  start_dir: string;
  home: string;
  // The Fused workspace dir (~/Fused) — the sidebar's "Fused" entry.
  fused_dir: string;
  version: string;
  // Version installed on disk (bundle Info.plist), null when unpackaged.
  // Drifts from `version` after a DMG install replaces the bundle under a
  // still-running process — ServerStatusBanner then asks for an app restart.
  installed_version: string | null;
  // Root of the mounts dir (~/.fused-render/mounts). The sidebar's "Learn"
  // entry navigates to `${mounts_root}/learn`, the builtin read-only mount
  // of the bundled learn.zip (D123) — same dir every mount lives under.
  mounts_root: string;
  // Whether the builtin learn mount record exists yet — the sidebar only
  // renders the Learn entry when this is true, so it's never a dead link
  // (unpackaged dev run with no zip, or the brief window before startup's
  // background automount thread has upserted the record).
  learn_mount_ready: boolean;
  // Same gate for the builtin sessions mount (the Claude Sessions sub-app).
  sessions_mount_ready: boolean;
  // Self-update state (fused_render/update/mac.py) — present only when the
  // packaged mac app started the update manager; absent on dev servers and
  // the Windows/Linux packages (those update through their supervisor).
  update?: UpdateStatus;
  // A Claude session changed this installation (fused_render/selffix.py) — the
  // sidebar's version chip turns amber and leads to its report. PRESENT ONLY
  // WHEN MODIFIED: there is no `modified: false` shape, because the ordinary
  // state of an installation is that nobody has touched it, and a field that is
  // always there invites a truthiness check that a `{modified: false}` object
  // would silently pass.
  modified_install?: ModifiedInstall;
  // The installation cannot be written to, so a self-fix session started here
  // can only DIAGNOSE (fused_render/selffix.py, SPEC §43 SF-13). PRESENT ONLY
  // WHEN READ-ONLY, for the same reason as `modified_install` above: the
  // ordinary install is one the user owns.
  read_only?: boolean;
  // No claude_config gate here any more: the Claude Config app stopped being a
  // mounted html+py app and became native React over its own server bridge, so
  // its availability is GET /api/claude-config/status (useClaudeConfigAvailable
  // in apps/claude_config), not a mount record.
}

export interface FsEntry {
  name: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
  ignored?: boolean; // matched by .gitignore inside a git repo (dimmed in the UI)
}

export interface ListResult {
  path: string;
  entries: FsEntry[];
  // The listing is a partial page: the directory has more entries than the
  // server's LIST_MAX_ENTRIES cap (or the remote listing was capped). Older
  // servers omit these two fields, so both are optional.
  truncated?: boolean;
  // Opaque continuation token for the next page — non-null only on the
  // resumable S3-direct route (rclone and a local scandir can't resume). Pass
  // it back to listDir to fetch the next page.
  cursor?: string | null;
}

// One entry from GET /api/fs/walk. `rel` is a posix path relative to the
// walked directory; dir entries carry size null (same convention as FsEntry).
export interface WalkEntry {
  rel: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
  // No `ignored` flag here (unlike FsEntry): the walk PRUNES gitignored
  // entries server-side, so nothing ignored ever reaches search results.
}

export interface WalkResult {
  path: string;
  entries: WalkEntry[];
  truncated: boolean; // hit the server's entry cap
}

// One entry per resolved template mode (SPEC PT-8), in order; the default is
// the first entry WITHOUT `conditional` (a gated template is never the default
// while normal ones exist). path is null for a sentinel mode (PT-12, e.g.
// "_render") — no template folder backs it, the shell knows what to do from
// the mode name alone. `conditional` marks a template whose condition.py gate
// has NOT been run yet (CT-12): stat no longer evaluates gates (they may do
// remote I/O), so the shell resolves them in the background via
// resolveConditions and shows the entry as pending until the verdict lands.
export interface TemplateEntry {
  mode: string;
  path: string | null;
  icon: string | null;
  conditional?: boolean;
}

export interface StatResult {
  path: string;
  name: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
  // Bytes come from a remote (path under a mount). Preview forwards this to
  // the template iframe as _remote=1 so pages can prefer ranged HTTP reads.
  remote?: boolean;
  // False for a file on a read-only mount (or any path the user can't write).
  writable?: boolean;
  // /api/fs/write only: whether that write ADDED this path rather than
  // replacing one. The index stores names, so it is only re-scanned for the
  // first kind, and the search box's "indexing…" caption follows the same
  // rule rather than guessing (lib/index-freshness).
  created?: boolean;
  templates: TemplateEntry[];
  template_error?: string;
}

// Error thrown by the shared fetch helpers, carrying the HTTP status alongside
// the server's message. `.message` is exactly what it was before (the server's
// `error` string, else `HTTP <status>`), so callers that only read `.message`
// are unaffected; the extra `.status` lets client-side humanizers (lib/
// fs-actions friendlyFsError) branch on e.g. 404 without re-parsing the text.
export interface HttpError extends Error {
  status?: number;
}
function httpError(data: { error?: string } | null, status: number): HttpError {
  const err = new Error((data && data.error) || `HTTP ${status}`) as HttpError;
  err.status = status;
  return err;
}

// `signal` is what a folder change uses to abandon an in-flight index fetch,
// the same way it abandons a walk stream.
// getJson/postJson are exported so a feature that keeps its own typed wrappers
// in its own module (the Claude-config bridge, apps/claude_config/api.ts) speaks
// this exact transport rather than a second hand-rolled fetch: same thrown
// HttpError contract, and — the part that actually bites — the same X-Fused
// write guard, without which those endpoints answer 403.
export async function getJson<T>(
  url: string,
  opts?: { headers?: Record<string, string>; signal?: AbortSignal },
): Promise<T> {
  const res = await fetch(url, opts);
  const data = await res.json();
  if (!res.ok) throw httpError(data, res.status);
  return data as T;
}

// One mutating-request helper for both PUT and POST — they differ only in the
// method. X-Fused forces a CORS preflight so a foreign page can't write blind
// (the D3 guard the reveal/write/clone endpoints require).
async function mutateJson<T>(method: "PUT" | "POST", url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-Fused": "1" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw httpError(data, res.status);
  return data as T;
}

const putJson = <T>(url: string, body: unknown) => mutateJson<T>("PUT", url, body);
export const postJson = <T>(url: string, body: unknown) => mutateJson<T>("POST", url, body);

export function getConfig(): Promise<Config> {
  return getJson<Config>("/api/config");
}

// -- Self-update (fused_render/server/routers/update.py) ---------------------

export interface UpdateStatus {
  // idle | checking | available | installing | installed | error
  state: string;
  // brew: the user runs `brew upgrade --cask` themselves (see manual_command);
  // dmg: the app downloads and swaps its own bundle; none: not updatable.
  method: string;
  latest_version: string | null;
  // Bytes downloaded so far (dmg method only) — the manifest carries no total
  // size, so the UI shows MB downloaded rather than a percentage.
  progress: number | null;
  error: string | null;
  // Set when the user must run the update themselves (brew-managed installs,
  // state "available") — shown with a copy button.
  manual_command: string | null;
}

export function updateCheck(): Promise<UpdateStatus> {
  return postJson<UpdateStatus>("/api/update/check", {});
}

export function updateInstall(): Promise<UpdateStatus> {
  return postJson<UpdateStatus>("/api/update/install", {});
}

export function listDir(fsPath: string, cursor?: string | null): Promise<ListResult> {
  let url = "/api/fs/list?path=" + encodeURIComponent(fsPath);
  if (cursor) url += "&cursor=" + encodeURIComponent(cursor);
  return getJson<ListResult>(url);
}

// Brief cross-mount dedupe for a directory's FIRST listing page. On navigation
// the app paints a listing scaffold whose Listing kicks off /api/fs/list in
// parallel with the slow /api/fs/stat; when stat resolves, the real preview
// mounts a fresh Listing for the SAME path. Without this cache that second
// mount would re-issue the identical request and throw the parallel fetch away.
// So the initial (non-cursor, un-refreshed) listing goes through here: a call
// within the short TTL of an earlier one for the same path reuses its promise.
// A rejected promise evicts at once (errors never stick); the TTL keeps the
// window small so a later navigation back to the same dir always re-reads,
// matching stat's freshness posture (the dir-watch socket refresh bypasses this
// entirely — it must see live data).
//
// ANY SUCCESSFUL FS MUTATION EMPTIES THIS MAP (clearListPrefetch, called from
// noteAfter below — add nothing that mutates the filesystem outside it). Without
// that it was a five-second window in which a moved file could still be painted
// in the folder it had left, and the report was "dragging a file onto a
// breadcrumb COPIES it": the spring-load navigates to the crumb (caching that
// listing) and unmounts the source listing with its dir-watch, the drop moves
// the file for real, and navigating back to the source within the TTL is a FRESH
// mount — refresh === 0, so useDirListing reads through here and repaints the
// pre-move listing. One file in two folders, self-healing after 5s.
//
// The WHOLE map goes, never one path: a rename touches two directories, a
// recursive delete a subtree, and a compress writes a sibling — path arithmetic
// over that buys nothing and can be wrong. The cache only exists to dedupe a
// double-fetch inside a single navigation, so its useful lifetime is about a
// second; over-evicting costs one extra /api/fs/list on the next navigation.
const LIST_PREFETCH_TTL_MS = 5000;
const listPrefetch = new Map<string, { promise: Promise<ListResult>; ts: number }>();

// Forget every cached listing. Three callers, each covering what the others
// cannot — the split is the point, so keep it accurate:
//
//   noteAfter, below           every mutation THIS module performs.
//   the dir-watch socket       a change made by anything else to the ONE folder a
//   (listing/useDirListing)    mounted listing is watching: an editor, Claude, a
//                              git checkout. Not a general backstop — it covers
//                              only that folder, only while it is mounted, and
//                              never (say) a crumb drop's destination.
//   window._fusedFsChanged     a write from inside a preview iframe — its own JS
//   (installed in main.tsx)    realm with its own copy of this module, so nothing
//                              here sees its fetches. static/runtime.js reports
//                              writeFile / uploadFile / mkdir / runPython up the
//                              same-origin ancestor chain. This is the only cover
//                              for a template view of a FILE, which mounts no
//                              listing and so has no watcher at all.
export function clearListPrefetch(): void {
  listPrefetch.clear();
}

export function prefetchListDir(fsPath: string): Promise<ListResult> {
  const hit = listPrefetch.get(fsPath);
  if (hit && Date.now() - hit.ts < LIST_PREFETCH_TTL_MS) return hit.promise;
  const promise = listDir(fsPath);
  listPrefetch.set(fsPath, { promise, ts: Date.now() });
  promise.catch(() => {
    // Evict only if still the same entry (a newer prefetch may have replaced it).
    if (listPrefetch.get(fsPath)?.promise === promise) listPrefetch.delete(fsPath);
  });
  return promise;
}

export function walkDir(fsPath: string, opts?: { hidden?: boolean }): Promise<WalkResult> {
  let url = "/api/fs/walk?path=" + encodeURIComponent(fsPath);
  if (opts?.hidden) url += "&hidden=1";
  return getJson<WalkResult>(url);
}

// Terminal record of a streamed walk (the server's final NDJSON line).
export interface WalkStreamEnd {
  truncated: boolean;
  total: number;
}

// Streaming walk: GET /api/fs/walk?stream=1 returns NDJSON — `{"entries":
// [...]}` batch lines then one `{"done": true, truncated, total}` line.
// `onBatch` fires once per network chunk (all complete lines in it, merged)
// with the new entries and the running total, so the caller can score/render
// progressively while the server is still walking. Resolves with the terminal
// record; rejects on HTTP errors, malformed/absent terminal line, or abort
// (an AbortError, which also cancels the server-side walk — Starlette closes
// the generator when the client goes away).
export async function walkDirStream(
  fsPath: string,
  opts: {
    hidden?: boolean;
    signal?: AbortSignal;
    onBatch: (entries: WalkEntry[], total: number) => void;
  }
): Promise<WalkStreamEnd> {
  let url = "/api/fs/walk?stream=1&path=" + encodeURIComponent(fsPath);
  if (opts.hidden) url += "&hidden=1";
  const res = await fetch(url, { signal: opts.signal });
  if (!res.ok) {
    // Error responses are plain JSON (the _error shape), not NDJSON.
    const data = await res.json().catch(() => null);
    throw new Error((data && data.error) || `HTTP ${res.status}`);
  }
  if (!res.body) throw new Error("streaming not supported by this browser");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let total = 0;
  let end: WalkStreamEnd | null = null;
  const consume = (raw: string) => {
    const chunkEntries: WalkEntry[] = [];
    for (const line of raw.split("\n")) {
      if (!line.trim()) continue;
      const msg = JSON.parse(line);
      if (msg.done) end = { truncated: !!msg.truncated, total: msg.total ?? total };
      else if (Array.isArray(msg.entries)) chunkEntries.push(...msg.entries);
    }
    if (chunkEntries.length) {
      total += chunkEntries.length;
      opts.onBatch(chunkEntries, total);
    }
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const cut = buffer.lastIndexOf("\n");
    if (cut === -1) continue; // no complete line yet
    consume(buffer.slice(0, cut + 1));
    buffer = buffer.slice(cut + 1);
  }
  buffer += decoder.decode(); // flush any trailing bytes
  if (buffer.trim()) consume(buffer);
  if (!end) throw new Error("walk stream ended without a terminal record");
  return end;
}

// GET /api/index/search (`fmt=columns`) has no client here any more.
//
// It served the in-folder search's whole-folder corpus, which the browser then
// ranked; the box asks `/api/index/rank` per query now, and the only corpus
// left in the app is the live walk's, for the folders no scan can cover. The
// SERVER route stays regardless: it is the `fused.fileIndex.search` bridge
// contract that user pages are written against.

// GET /api/index/rank — the home search: the server filters AND ranks, and
// answers with the top ~200 rows.
//
// The corpus route above is the other shape of the same index, and the
// difference is the whole point: `indexSearch` hands the browser every entry
// under the root (19.8 MB on a 164k-entry home, capped so most of a big home
// was unfindable) and ranks locally; this is a few KB per query and can see
// the whole index. The in-folder search keeps the corpus, because it also has
// a live walk to rank and only a browser-side ranker can rank a stream.
//
// `positions` are NOT on the wire: the caller re-runs `fuzzyMatch(q, rel)`
// over the rows it got back, so platform/lib/fuzzy.ts stays the single source
// of truth for what highlights (and the server's port of it, index/rank.py,
// stays free to change its internals). A miss is a normal 200 with
// covered:false, same as the corpus.
export interface IndexRankHit {
  rel: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
  // The ranking that produced this order. Carried for debugging and for
  // callers that want to group by tier; the ORDER is the contract.
  score: number;
  longest_run: number;
  tier: number;
  depth: number;
}

// Why a ranked answer is what it is. `""` is a real answer; the rest are the
// four ways the index cannot give one, and they are NOT interchangeable —
// `uncovered` is fixed by scanning the folder, `scanning` by waiting, and the
// other two never (see listing/index-source, which is the only place that
// switches on this).
export type RankReason =
  | ""
  | "mount"
  | "package"
  | "ignored"
  | "uncovered"
  | "scanning";

export interface IndexRankResult {
  covered: boolean;
  fresh: boolean;
  // WHY this answer is what it is — "" when the index answered outright, else
  // "mount" | "package" | "ignored" | "uncovered" | "scanning". The in-folder
  // search picks its source from this (listing/index-source); the client
  // deliberately holds no copy of the rules behind it, because the mount
  // policy is MountGuard's and the ignore list is the scan config's.
  reason: RankReason;
  root: string;
  hits: IndexRankHit[];
  // More matched than were returned — either more than `limit` survived
  // ranking, or the server's candidate cap bit.
  truncated: boolean;
  total: number;
  updated: number | null;
  age_s: number | null;
}

export function indexRank(
  fsPath: string,
  query: string,
  opts: { signal?: AbortSignal; limit?: number } = {},
): Promise<IndexRankResult> {
  const params = new URLSearchParams({ root: fsPath, q: query });
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  return getJson<IndexRankResult>("/api/index/rank?" + params.toString(), {
    signal: opts.signal,
  });
}

// GET /api/index/status with no run id — the state of the most recent scan,
// which is what a page that just loaded can ask about (it has no run id, but
// the startup scan may well be running).
export interface IndexStatus {
  // A scan is in flight. Independent of has_index: a rescan keeps serving the
  // last completed generation, so this means "say indexing…", not "stop using
  // the index".
  scanning: boolean;
  has_index: boolean;
  files_indexed: number; // rows in the last COMPLETED index
  last_completed_at: number | null;
  running: boolean; // the polled run specifically (== scanning with no run_id)
  run_id: string | null;
  root: string | null;
  phase: string;
  dirs: number;
  files: number; // this run's progress
  error: string | null;
}

export function indexStatus(signal?: AbortSignal): Promise<IndexStatus> {
  return getJson<IndexStatus>("/api/index/status", { signal });
}

// Preferences > Indexing. `roots` is what the scheduler scans (defaulted to
// the home dir server-side); `ignore` is the prune list; `defaults` is what
// "Restore defaults" restores to.
export interface IndexConfig {
  roots: string[];
  configured_roots: string[];
  ignore: string[];
  defaults: string[];
  location: string;
  // Set by a write: the saved rules no longer match the ones the index was
  // built under, so a reconciling scan was started (rescan_run_id).
  needs_rescan?: boolean;
  rescan_run_id?: string | null;
}

export function getIndexConfig(): Promise<IndexConfig> {
  return getJson<IndexConfig>("/api/index/config");
}

export function putIndexConfig(body: {
  roots?: string[];
  ignore?: string[];
}): Promise<IndexConfig> {
  return mutateJson<IndexConfig>("POST", "/api/index/config", body);
}

// With no `root` this scans EVERY configured root, so the answer is a list.
// `run_id`/`root` are the first run's, kept for callers that want just one.
export function startIndexScan(opts: { root?: string; full?: boolean } = {}): Promise<{
  run_id: string;
  root: string;
  runs: { run_id: string; root: string }[];
}> {
  return mutateJson("POST", "/api/index/scan", opts);
}

// POST /api/index/scan-folder — "cover this folder, someone is searching it".
//
// The in-folder search's answer to a folder the index has never visited, which
// used to be answered by a live streamed walk. Never an error and every "no"
// is durable (`why`: refused / debounced), because the caller is a search box:
// a refusal it could read as transient would be retried at keystroke rate.
export interface FolderScanRequest {
  started: boolean;
  why: "started" | "joined" | "debounced" | "refused";
  run_id: string | null;
  root: string;
}

// Deliberately not abortable: aborting the FETCH would not stop the scan it
// asked for, so a caller that dropped the reply would only lose the `why`.
export function requestFolderScan(fsPath: string): Promise<FolderScanRequest> {
  return mutateJson("POST", "/api/index/scan-folder", { path: fsPath });
}

// POST /api/index/query and /api/index/ask — read-only SQL over the index, and
// the same thing from a question in English (index/specs/query.md §5).
//
// NOT through mutateJson, for two reasons: it throws on a non-2xx, and a
// refused `ask` returns a 400 whose body carries the compiled `sql` the user
// needs to see — throwing would drop it. And deliberately NOT through
// noteAfter/noteFsMutation: a query changes nothing, so marking a folder dirty
// would drop it to a live walk for the rest of the session for no reason.
// The X-Fused header is still required (both routes execute a caller-shaped
// statement, so both are guarded).
export async function runIndexQuery(
  body: { sql: string; limit?: number },
): Promise<IndexQueryOutcome> {
  return indexQueryPost("/api/index/query", body);
}

export async function askIndex(
  body: { prompt: string; limit?: number },
): Promise<IndexQueryOutcome> {
  return indexQueryPost("/api/index/ask", body);
}

async function indexQueryPost(url: string, body: unknown): Promise<IndexQueryOutcome> {
  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Fused": "1" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    return { ok: false, sql: null, error: (e as Error).message };
  }
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    // outcomeFrom turns a null body into `HTTP <status>`, which is the honest
    // message when the server did not answer JSON at all.
  }
  return outcomeFrom(res.status, data);
}

export function deleteIndex(): Promise<{ deleted: boolean }> {
  // The corpus any open search fetched predates the delete; without this
  // signal nothing refetches it — the filesystem didn't change, so no
  // dir-watch refresh ever arrives (lib/index-freshness).
  return mutateJson<{ deleted: boolean }>("POST", "/api/index/delete", {}).then((r) => {
    noteIndexLifecycle();
    return r;
  });
}

// One hit from POST /api/search/files (the AI search's execution engine — one
// SQL query against the app's file index, the only engine). `path` is absolute.
export interface SearchFileEntry {
  path: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
}

export interface SearchFilesResult {
  entries: SearchFileEntry[];
  truncated: boolean;
}

// File search from a filter spec (see apps/explorer/lib/ai-search), scoped to
// whatever the index has scanned — home by default. Takes a signal because a new
// search must be able to abandon the previous one mid-flight. A missing or
// unreadable index is an ERROR here (503/502), never an empty result: see the
// server's search.py.
export async function searchFiles(
  spec: unknown,
  signal?: AbortSignal,
): Promise<SearchFilesResult> {
  const res = await fetch("/api/search/files", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Fused": "1" },
    body: JSON.stringify(spec),
    signal,
  });
  const data = await res.json();
  if (!res.ok) throw httpError(data, res.status);
  return data as SearchFilesResult;
}

// `signal` matters for callers that stat on a user's behalf and then navigate:
// a stat on a slow mount can resolve after the user has moved on, and acting on
// it would move them back. See FilesHome's path shortcut.
export function statPath(fsPath: string, signal?: AbortSignal): Promise<StatResult> {
  return getJson<StatResult>("/api/fs/stat?path=" + encodeURIComponent(fsPath), { signal });
}

// Deferred condition.py verdicts (CT-12): {mode: allowed} for every entry
// stat marked `conditional`. `error` carries the first broken gate's reason
// (that gate reports false — fail closed), mirroring stat's template_error.
export interface ConditionsResult {
  path: string;
  conditions: Record<string, boolean>;
  error?: string;
}

// Gates can be slow (remote I/O) and both the preview and the pane menu ask
// for the same path at the same time, so in-flight calls are shared: one
// request per path, dropped from the map once settled (a later call — e.g.
// after a nav back — re-evaluates, matching stat's freshness posture).
const inflightConditions = new Map<string, Promise<ConditionsResult>>();

export function resolveConditions(fsPath: string): Promise<ConditionsResult> {
  let p = inflightConditions.get(fsPath);
  if (!p) {
    p = getJson<ConditionsResult>(
      "/api/fs/conditions?path=" + encodeURIComponent(fsPath)
    ).finally(() => inflightConditions.delete(fsPath));
    inflightConditions.set(fsPath, p);
  }
  return p;
}

export function rawUrl(fsPath: string): string {
  return "/api/fs/raw?path=" + encodeURIComponent(fsPath);
}

// Bookmark store (server-side, ~/.fused-render/bookmarks.json). The tree shape
// is BookmarkItem[] (lib/bookmarks.ts); kept as unknown[] here so api.ts has no
// dependency on the bookmark data layer. `exists` is false only until the file
// is first written — the shell's one-time localStorage-import gate. `missing`
// is a side-channel, recomputed fresh on every GET: bookmark ids whose target
// is confirmed gone from disk — display-only, never written back through PUT.
export interface BookmarksResult {
  exists: boolean;
  bookmarks: unknown[];
  missing: string[];
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

// Write a portable `<name>.bookmark` file next to the bookmark's target(s)
// (SB-8). The frontend computes dir/filename/content (lib/bookmark-file.ts);
// the server validates and writes, overwriting any previous save.
export interface BookmarkExport {
  dir: string;
  filename: string;
  content: string;
}

export function exportBookmarkFile(payload: BookmarkExport): Promise<{ path: string }> {
  return postJson<{ path: string }>("/api/bookmarks/export", payload);
}

// Read a `.bookmark` file from disk (SB-9): the `_bookmark` sentinel resolves
// the record's relative paths against `dir` (the file's own directory) and
// redirects. The server validates (absolute path, exists, version 1) and reads.
export interface BookmarkFileResult {
  dir: string;
  bookmark: Record<string, unknown>;
}

export function getBookmarkFile(path: string): Promise<BookmarkFileResult> {
  return getJson<BookmarkFileResult>("/api/bookmark-file?path=" + encodeURIComponent(path));
}

// Recently opened files (fused_render/shell/recents.py). `url` is the shell
// /view/ url verbatim including its query string (D20 posture); entries whose
// file has since been deleted are already filtered out server-side.
export interface RecentEntry {
  url: string;
  openedAt: string;
  // The page's own <title>, when one was known at record time — preferred
  // over the file's basename for the sidebar row (see Sidebar.tsx).
  title?: string;
}

export interface RecentsResult {
  collapsed: boolean;
  entries: RecentEntry[];
}

export function getRecents(): Promise<RecentsResult> {
  return getJson<RecentsResult>("/api/recents");
}

// Server no-ops (recorded: false) for directory/sentinel/missing-file urls,
// so callers need not pre-classify the target.
export function postRecentOpen(url: string, title?: string | null): Promise<{ recorded: boolean }> {
  return postJson<{ recorded: boolean }>(
    "/api/recents/open",
    title ? { url, title } : { url }
  );
}

export function putRecentsCollapsed(collapsed: boolean): Promise<void> {
  return putJson<unknown>("/api/recents/collapsed", { collapsed }).then(() => undefined);
}

// -- Preferences (shell/prefs.py; SPEC §20) -----------------------------------

export interface EnginePrefs {
  selected: "builtin" | "fused";
  effective: "builtin" | "fused";
  // The raw FUSED_RENDER_ENGINE value when set — the process-level override
  // that beats the pref (the page shows the switch locked).
  forced_by: string | null;
  fused_available: boolean;
}

export interface Prefs {
  engine: EnginePrefs;
  // Whether the Reader (listen-to-files) accessibility mode is offered (opt-in,
  // default off).
  reader: { enabled: boolean };
  // The default Claude model, as one of the claude template's own short names
  // — "" means unset, and each consumer keeps its own default (the fused.ai
  // relay's haiku, the chat template's sonnet). `choices` is the server's own
  // value set, shipped with the value so the page renders exactly what a PUT
  // will accept rather than a second copy that can drift.
  model: { default: DefaultModel; choices: DefaultModel[] };
  // The app call log (fused_render/calls.py): capture state, how much of a
  // run's params is kept, retention window, and where the store lives.
  calls: CallsPrefs;
  // Which local-model backend serves each capability (D302). A DIFFERENT thing
  // from `engine` above, however similar the word: that one is /api/run's
  // executor, this one is the inference runner behind fused.ai's local models.
  engines: EnginesPrefs;
}

export interface EnginesPrefs {
  // One row per capability the registry knows, servable here or not — a
  // preference the user cannot see is one they cannot fix.
  capabilities: CapabilityEngine[];
  // The literal the server means by "let the registry decide". Shipped with
  // the value rather than hardcoded here, for the same reason `model.choices`
  // is: the page must not be able to send a value a PUT would reject.
  auto: string;
  // The models an engine PUT actually evicted, and ONLY on such a PUT — absent
  // from a GET, which describes state rather than reporting what a request
  // did. Residency is not otherwise in this payload, so the page cannot know:
  // switching engines with nothing loaded (the usual case on a fresh app)
  // unloads nothing, and the confirmation used to claim it had.
  unloaded?: string[];
}

export interface CapabilityEngine {
  // The Hub's own tag ("automatic-speech-recognition"), which is the vocabulary
  // the whole feature speaks.
  capability: string;
  // As STORED — `auto` or a runner code. Never rewritten to match reality: a
  // preference silently corrected on read is one the user cannot see or undo.
  selected: string;
  // What is actually resolving. Null when nothing can serve the capability
  // here. Differs from `selected` whenever a preference could not be honoured.
  effective: string | null;
  /** The FULL name, qualifier and all — for anything that has to match the
   *  engine picker's options word for word. */
  effectiveLabel: string | null;
  /** The same backend without the platform qualifier ("MLX LM"), for the
   *  summary line under the picker: it sits directly beneath options that
   *  already carry the qualifier, so repeating it there says nothing. */
  effectiveShortLabel: string | null;
  // Why the selection is not in force, in the registry's own words — null when
  // it is (including "auto", which is honoured by definition). A control whose
  // value does nothing, with nothing saying why, is what this field prevents.
  ignoredReason: string | null;
  choices: EngineChoice[];
}

export interface EngineChoice {
  code: string;
  label: string;
  /** What using this backend is LIKE, when there is something worth saying. */
  note: string | null;
  available: boolean;
  /** Why not — "needs Apple Silicon — MLX runs on Metal only (this is
   *  windows/amd64)". The page renders this beside a disabled control rather
   *  than writing its own copy, which it could not: this is a fact about the
   *  machine and the backend, and only the server knows it. */
  reason: string | null;
}

// Short model names, matching shell/prefs.py's VALID_DEFAULT_MODELS. "" is the
// unset member, not an absence — it is what the "Automatic" option writes.
export type DefaultModel = "" | "fable" | "opus" | "sonnet" | "haiku";

export type CallsParamsMode = "full" | "keys" | "off";

export interface CallsPrefs {
  // On by default: a diagnostic you have to switch on before the thing you
  // wanted to diagnose is worthless — the interesting call already happened.
  enabled: boolean;
  params: CallsParamsMode;
  retention_days: number;
  dir: string;
  // False until the first call is recorded: the writer creates the store
  // lazily, so `dir` names a path that may not exist yet. Browsing it before
  // then lands the explorer on a stat error, so the affordance waits.
  dir_exists: boolean;
  // What capture and retention are ACTUALLY doing (from the resolvers the
  // writer calls) versus the stored prefs above, which differ whenever a
  // process env var wins. `*_forced_by` is that raw env value when the variable
  // is genuinely in force, else null — a set-but-ignored value (an empty or
  // non-numeric retention window) reports null, because the writer keeps using
  // the pref and a control locked against a variable setting nothing is a dead
  // end. Only these two are overridable; the param mode has no env var.
  effective_enabled: boolean;
  enabled_forced_by: string | null;
  effective_retention_days: number;
  retention_forced_by: string | null;
}

export function getPrefs(): Promise<Prefs> {
  return getJson<Prefs>("/api/prefs");
}

export function putEnginePref(engine: "builtin" | "fused"): Promise<Prefs> {
  return putJson<Prefs>("/api/prefs", { engine });
}

export function putReaderEnabled(enabled: boolean): Promise<Prefs> {
  return putJson<Prefs>("/api/prefs", { reader_enabled: enabled });
}

export function putDefaultModel(model: DefaultModel): Promise<Prefs> {
  return putJson<Prefs>("/api/prefs", { default_model: model });
}

// One capability's inference engine. A MAP rather than a pair, because the
// server applies it key by key onto what is stored — so this changes one
// capability without echoing the others, and two open tabs cannot undo each
// other's choice.
export function putEngineForCapability(capability: string, code: string): Promise<Prefs> {
  return putJson<Prefs>("/api/prefs", { engines: { [capability]: code } });
}

export function putCallsEnabled(enabled: boolean): Promise<Prefs> {
  return putJson<Prefs>("/api/prefs", { calls_enabled: enabled });
}

export function putCallsParamsMode(mode: CallsParamsMode): Promise<Prefs> {
  return putJson<Prefs>("/api/prefs", { calls_params: mode });
}

export function putCallsRetentionDays(days: number): Promise<Prefs> {
  return putJson<Prefs>("/api/prefs", { calls_retention_days: days });
}

// Reveal a path in the OS file manager (same POST the breadcrumb button uses).
export function revealPath(fsPath: string): Promise<void> {
  return postJson<unknown>("/api/fs/reveal", { path: fsPath }).then(() => undefined);
}

// -- Filesystem mutations (fused_render/server.py; X-Fused write-guard) -------
// Create / delete / rename / copy entries, driven by the explorer's context
// menu. All share /api/fs/write's error contract, surfaced as the thrown
// Error's message: 400 (bad/relative path), 403 ("readonly" target), 404
// (missing src), 409 ("conflict" — destination exists, or a non-empty dir
// deleted without recursive).

// Every mutation below marks the paths it touched, so in-folder search stops
// answering from the index snapshot for that folder and walks it live — there
// is no filesystem watcher, so the corpus would otherwise keep offering the
// old name and never the new one (lib/index-freshness). It also empties the
// listing prefetch cache, whose whole hazard is repainting a directory as it
// stood BEFORE the mutation (see clearListPrefetch above).
//
// Both are recorded only on SUCCESS: a refused mutation changed nothing, so
// pessimising a folder over a 409 would drop it to the slow path for the rest
// of the session, and dropping the prefetch would throw away a listing that is
// still accurate.
//
// This is the one choke point for the mutations in this module — going through
// it is what stops a NEW wrapper from silently skipping either bookkeeping.
// `rescans` is asked of the RESULT, because whether the index will be
// rebuilt for a mutation is the server's decision and not always predictable
// from the request: a write creates a file or replaces one, and only the
// server knows which (see `created` in the /api/fs/write response).
function noteAfter<T>(
  paths: string | string[],
  p: Promise<T>,
  rescans: (out: T) => boolean = () => true,
): Promise<T> {
  return p.then((out) => {
    clearListPrefetch();
    const indexed = rescans(out);
    for (const path of Array.isArray(paths) ? paths : [paths]) {
      if (path) noteFsMutation(path, { rescans: indexed });
    }
    return out;
  });
}

// Create (or overwrite) a plain file. Used for "New File…" with empty content;
// the parent directory must already exist (the server does not mkdir -p).
// With create=true the write refuses (409 "conflict") when the path already
// exists, so "New File" can't silently clobber an existing file.
export function writeFile(path: string, content = "", create = false): Promise<StatResult> {
  return noteAfter(
    path,
    postJson<StatResult>("/api/fs/write", { path, content, create }),
    // An overwrite is not re-indexed (the index stores names), so the box must
    // not claim it is. An older server that does not answer `created` is
    // treated as having created something, which errs toward the caption.
    (out) => out.created !== false,
  );
}

// Create a single directory (no mkdir -p — a missing parent is a 400).
export function mkdir(path: string): Promise<StatResult> {
  return noteAfter(path, postJson<StatResult>("/api/fs/mkdir", { path }));
}

// Remove a file or directory. A non-empty directory needs recursive=true (the
// context menu passes it only after the confirm dialog spells that out).
// With trash=true the entry is moved to the OS bin instead — ~/.Trash on macOS,
// the freedesktop XDG trash on Linux, the Recycle Bin on Windows. Where THIS PATH
// cannot use the bin (a Linux cross-device move, a remote mount, a platform with
// no backend) the server replies 501 "trash unsupported" and the caller falls
// back to the irreversible hard delete.
//
// `trashed_to` is WHERE a trash move landed — present only when the server
// chose that path itself (its own os.rename into ~/.Trash), absent when Finder
// did the move and therefore picked the location. It is what makes a trash
// delete undoable: with it the delete is a rename pair like any other
// relocation (explorer/lib/fs-undo). Never present on a hard delete.
export function deleteEntry(
  path: string,
  recursive = false,
  trash = false
): Promise<{ deleted: string; trashed?: boolean; trashed_to?: string }> {
  return noteAfter(
    path,
    postJson<{ deleted: string; trashed?: boolean; trashed_to?: string }>("/api/fs/delete", {
      path,
      recursive,
      trash,
    })
  );
}

// Move/rename src -> dst (also the paste-of-a-cut move). An existing dst is a
// 409 unless overwrite=true.
export function renameEntry(src: string, dst: string, overwrite = false): Promise<StatResult> {
  return noteAfter([src, dst], postJson<StatResult>("/api/fs/rename", { src, dst, overwrite }));
}

// Move an entry INTO or OUT OF the OS bin. Same guards and same error contract
// as renameEntry (it delegates to the very same handler server-side), plus one
// thing a plain rename cannot do: it keeps the bin's own bookkeeping straight —
// on Linux the freedesktop `.trashinfo` sidecar is written when the entry moves
// into the trash and removed when it moves back out.
//
// This is the primitive undo/redo uses for a `"delete"` op, and the only reason
// it is separate from renameEntry: the sidecar is server-side knowledge, so the
// undo stack stays a list of plain path pairs and picks a primitive by kind
// (explorer/lib/fs-undo's applyFsOp) rather than learning what a trash is.
export function trashMove(from: string, to: string): Promise<StatResult> {
  return noteAfter([from, to], postJson<StatResult>("/api/fs/trash-move", { from, to }));
}

// Copy src -> dst (paste-of-a-copy, and Duplicate). Same 409-on-existing-dst
// rule as rename; a directory copied into itself/a descendant is a 400.
export function copyEntry(src: string, dst: string, overwrite = false): Promise<StatResult> {
  return noteAfter(dst, postJson<StatResult>("/api/fs/copy", { src, dst, overwrite }));
}

// The archive formats /api/fs/compress accepts. Kept as a union so a typo
// can't reach the server, which answers an unknown format with a 400.
export type ArchiveFormat = "zip" | "git-bundle" | "git-archive";

// Compress a FOLDER into `dest` (a sibling archive, named by the caller so a
// clash can be resolved against the listing first). Same 409-on-existing-dest
// and "readonly" contract as rename/copy. The git formats require `path` to be
// a repository root — see gitRepoInfo.
export function compressEntry(
  path: string,
  format: ArchiveFormat,
  dest: string
): Promise<StatResult> {
  return noteAfter(dest, postJson<StatResult>("/api/fs/compress", { path, format, dest }));
}

// Whether `path` is the work-tree ROOT of a git repository — the gate for the
// two git entries in the Compress submenu. It shells out to git, so it is
// fetched lazily on submenu hover, never as part of rendering a row.
export function gitRepoInfo(path: string): Promise<{ path: string; is_repo_root: boolean }> {
  return getJson("/api/fs/git-repo?path=" + encodeURIComponent(path));
}

// -- OS clipboard bridge (server/routers/clipboard.py) -----------------
// The webview can't read or write the native file flavors Finder/Explorer/
// Nautilus use, so the local backend does it for us and we trade in absolute
// paths. `token` is a content fingerprint of the ordered path list — the
// caller keeps the last one it SAW so an untouched clipboard never clobbers a
// pending in-app cut. `supported: false` means this machine has no bridge
// (no pyobjc, no xclip, a sandbox); it is a normal 200, not an error.
export interface OsClipboard {
  paths: string[];
  token: string;
  supported: boolean;
}

export function readOsClipboard(): Promise<OsClipboard> {
  return getJson<OsClipboard>("/api/clipboard/files");
}

export function writeOsClipboard(
  paths: string[]
): Promise<{ token: string; supported: boolean }> {
  return postJson<{ token: string; supported: boolean }>("/api/clipboard/files", { paths });
}

// -- Mounts (shell/mounts.py) ------------------------------------------
// Remote storage mounted as local paths via rclone rcd. Credentials live in
// rclone's config; mounts survive server restarts and are adopted on start.

export interface Mount {
  id: string;
  name: string;
  remote: string;
  mountpoint: string;
  // Health, not just presence:
  //  - "disconnected" = a kernel mount is (or was) there but its rclone daemon
  //    no longer serves it — listings show stale or empty data.
  //  - "stale" = the split-brain from the 2026-07-16 incident: rclone still
  //    lists the mount but the kernel dropped it (e.g. the user hit
  //    "Disconnect" on the macOS "Server connections interrupted" dialog).
  // Both are repaired via reconnectMount (force unmount + fresh mount).
  state: "mounted" | "stale" | "disconnected" | "unmounted";
  mounted: boolean; // state === "mounted"
  // The remote rejects writes (anonymous S3, an http backend, …), detected at
  // attach time. Files under the mountpoint stat as writable:false, so
  // templates open them read-only.
  read_only: boolean;
  // True for a bundled default mount (currently only Learn, D123) that the
  // server re-creates on every startup — the API rejects deleting it, so the
  // Mounts view hides Delete for it too (unmount still works).
  builtin: boolean;
  // Why restarting the rclone daemon would help this mount, else null:
  //  - "params" = the mount is live but its running options no longer match the
  //    record (e.g. read_only flipped) — a restart re-mounts to apply them.
  //  - "credentials" = a disconnected env_auth mount whose credentials probe
  //    valid again; the long-lived daemon still holds the stale keys, so only a
  //    restart (not Reconnect) re-reads the refreshed ones.
  // Both route the user to the single global Restart rclone button.
  restart_reason?: "params" | "credentials" | null;
  // The mount's async upload queue (D221). null means the question does not
  // APPLY — the mount is read-only or not healthy, so it can hold no queue.
  // A read that was attempted and failed comes back as {unknown: true}, which
  // is a different thing and must be shown, not swallowed: with a full VFS
  // cache a save completes locally and uploads afterwards, so "we don't know"
  // can hide files that never reached the remote.
  uploads?: MountUploads | null;
}

// Files written to a mount that haven't reached the remote yet. A discriminated
// union on `unknown` on purpose: the unknown case carries NO counts, so it
// cannot be read as zero by a caller that forgets to check.
export type MountUploads =
  | {
      unknown: false;
      pending: number;
      // Items whose upload already came back unsuccessfully (quota,
      // permissions). The number that matters — a save the user saw succeed
      // did not stick. Always <= pending; rclone re-queues a failed item
      // rather than dropping it, so it stays counted in both.
      failed: number;
      failed_names: string[]; // capped by the server; `failed` carries the rest
    }
  | { unknown: true; reason: string };

// How a remote is reached, which is what the Remote dropdown groups by:
// "public" = anonymous, no credentials at all; "detected" = the user's own
// AWS/gcloud credentials, read where they already live; "other" = a remote the
// user set up themselves (only a materialized remote can be this).
export type RemoteKind = "public" | "detected" | "other";

// The cloud behind a remote, from its rclone backend type — used to match a
// pasted s3:// or gs:// link to a remote that can actually serve it.
export type RemoteProvider = "s3" | "gcs" | "other";

// A remote we can offer from credentials already present in the user's
// dotfiles (AWS profiles/env, gcloud ADC). Materialized on first use into a
// keyless env_auth remote; `id` identifies the source to the detect endpoint.
export interface RemoteSuggestion {
  id: string;
  label: string;
  remote_name: string;
  kind: RemoteKind;
  provider: RemoteProvider;
  // Whether `remote_name` has ALREADY been materialized. The server returns
  // every suggestion either way, so the setup panels can show what is possible;
  // anything that CREATES from a suggestion (the "suggest:<id>" options in Add
  // mount) must offer only `!exists` ones or it 409s on a remote that's there.
  exists: boolean;
}

// An existing rclone remote. `name` is the verbatim rclone spec (incl trailing
// ':') used unchanged as the mount base; `label` is the friendly name to show —
// the same one its suggestion used, or the bare `name` for a custom remote.
export interface RcloneRemote {
  name: string;
  label: string;
  // Same two fields a RemoteSuggestion carries, meaning the same thing: the
  // server classifies a remote by PROVENANCE (its stored rclone config matched
  // against the suggestion that would have created it), so the client groups
  // and link-matches on facts rather than sniffing names and label substrings.
  // "other" = a remote the user brought themselves (custom S3, an OAuth account).
  kind: RemoteKind;
  provider: RemoteProvider;
}

export interface MountsResult {
  rclone: {
    available: boolean;
    version: string | null;
    remotes: RcloneRemote[];
    suggested: RemoteSuggestion[];
  };
  mounts: Mount[];
}

export function getMounts(): Promise<MountsResult> {
  return getJson<MountsResult>("/api/mounts");
}

// Lightweight health snapshot for the background mount-health poll (the global
// disconnect/reconnect toast, useMountHealth). Cheaper than getMounts — no
// rclone enumeration — and carries a bounded, append-only `events` log with
// monotonically increasing int ids the poller tracks a high-water mark against.
export interface MountHealth {
  id: string;
  name: string;
  state: Mount["state"];
  mountpoint: string;
}

export type MountEventKind = "disconnected" | "reconnected" | "reconnect_failed";

export interface MountEvent {
  id: number; // monotonic, append-only — the poll's high-water mark keys on it
  mount_id: string;
  name: string;
  kind: MountEventKind;
  ts: number; // epoch seconds
  detail: string;
}

export interface MountsHealthResult {
  mounts: MountHealth[];
  events: MountEvent[];
}

export function getMountsHealth(): Promise<MountsHealthResult> {
  return getJson<MountsHealthResult>("/api/mounts/health");
}

// Path-bar support: the local path a bucket URL (s3://, gs://, gcs://) maps to
// through the mount that covers it. Rejects with the server's message — "no
// mount covers s3://<bucket> …" — when nothing does; the caller shows it
// verbatim, since only the server knows the mount records and rclone config.
export function resolveCloudUrl(url: string): Promise<{ path: string }> {
  return getJson<{ path: string }>("/api/mounts/resolve?url=" + encodeURIComponent(url));
}

export function createMount(name: string, remote: string): Promise<Mount> {
  return postJson<Mount>("/api/mounts", { name, remote });
}

export function attachMount(id: string): Promise<Mount> {
  return postJson<Mount>(`/api/mounts/${id}/mount`, {});
}

// force=true is for a mount already shown as disconnected: its dead NFS
// mount rejects a plain unmount, so the backend escalates to a force unmount.
export function detachMount(id: string, force = false): Promise<Mount> {
  return postJson<Mount>(`/api/mounts/${id}/unmount${force ? "?force=1" : ""}`, {});
}

// Repair a disconnected mount: force-clear the dead mountpoint, remount.
export function reconnectMount(id: string): Promise<Mount> {
  return postJson<Mount>(`/api/mounts/${id}/reconnect`, {});
}

// Global recovery: restart the rcd daemon and re-mount everything. Briefly
// disconnects ALL mounts, but is the only fix for a stale-credential daemon
// (a fresh daemon re-reads refreshed keys) and for applying changed mount
// params. Returns the same shape as getMounts so the caller refreshes at once.
export function restartRclone(): Promise<MountsResult> {
  return postJson<MountsResult>("/api/mounts/restart", {});
}

export function deleteMount(id: string): Promise<void> {
  const res = fetch(`/api/mounts/${id}`, {
    method: "DELETE",
    headers: { "X-Fused": "1" },
  });
  return res.then(async (r) => {
    if (!r.ok) throw new Error((await r.json()).error || `HTTP ${r.status}`);
  });
}

// S3-compatible only: keys are written straight into rclone's own config.
// OAuth backends (Google Drive, …) have no keys to paste and go through
// startRemoteOAuth below instead.
export function createRemote(
  name: string,
  params: Record<string, string>
): Promise<{ ok: boolean; name: string }> {
  return postJson<{ ok: boolean; name: string }>("/api/mounts/remotes", {
    name,
    params,
  });
}

// Materialize a keyless remote from auto-detected credentials (idempotent).
// Returns the rclone remote name (e.g. "aws:") to mount against.
export function createDetectedRemote(id: string): Promise<{ ok: boolean; name: string }> {
  return postJson<{ ok: boolean; name: string }>("/api/mounts/remotes/detect", {
    id,
  });
}

// -- Browser sign-in: Google Drive, Dropbox, Box (D219, D223) -----------------
//
// The server spawns `rclone authorize "<backend>"`, which runs its own loopback
// callback server and opens the SYSTEM browser itself — unlike the Fused
// login there is no URL for us to window.open. So the client's whole job is
// to start it, poll, and report.
//
// The provider keys and their labels live in lib/oauth.ts; this module only
// moves the request and the status.

export interface RemoteOAuthStatus {
  in_flight: boolean;
  name: string | null;
  // Which provider the attempt is for ("drive" | "dropbox" | "box"), so a page
  // that polls a sign-in it did not start still labels it correctly.
  provider: string | null;
  backend: string | null;
  // Both null while in flight. `ok` false with a message is the failure the UI
  // must show — INCLUDING the child that exited having produced no token at
  // all (browser tab closed, consent never granted, timed out), which is
  // retryable and says so in `error`.
  ok: boolean | null;
  error: string | null;
}

// Starts the browser sign-in and returns immediately. 409 when one is already
// in flight (rclone's callback port can only be bound once), and 409 when
// `name` is already taken unless `replace` is set — config/create overwrites,
// so replacing a working remote takes an explicit opt-in rather than a stale
// client-side snapshot.
//
// `client` is the user's OWN OAuth client. It is REQUIRED for Drive (a 400
// otherwise): Google is retiring rclone's built-in shared client ID, so a Drive
// sign-in without one is refused before the browser ever opens. Dropbox and Box
// take none — omit it, and rclone uses its own.
export function startRemoteOAuth(
  name: string,
  opts: {
    provider?: string;
    replace?: boolean;
    clientId?: string;
    clientSecret?: string;
  } = {}
): Promise<{ ok: boolean; name: string; provider: string }> {
  return postJson<{ ok: boolean; name: string; provider: string }>(
    "/api/mounts/remotes/oauth",
    {
      name,
      provider: opts.provider ?? "drive",
      replace: opts.replace ?? false,
      client_id: opts.clientId ?? "",
      client_secret: opts.clientSecret ?? "",
    }
  );
}

// Open GET like getMounts — a pure in-memory read with no side effects.
export function getRemoteOAuthStatus(): Promise<RemoteOAuthStatus> {
  return getJson<RemoteOAuthStatus>("/api/mounts/remotes/oauth/status");
}

export function cancelRemoteOAuth(): Promise<{ ok: boolean; canceled: boolean }> {
  return postJson<{ ok: boolean; canceled: boolean }>("/api/mounts/remotes/oauth/cancel", {});
}

// -- Template management (fused_render/templates_api.py; TEMPLATE_MGMT_SPEC) --
//
// Two template dirs, modelled as an ordered list of "sources" (core is
// read-only/version-gated, user is editable). The registry maps a dot-key
// (extension pattern) to an ordered list of template names, first = default.

// A template dir. TODAY exactly two (core, user); modelled as a list so a
// third (org/project) can be appended later with no UI rework.
export interface TemplateSource {
  id: string; // "core" | "user"
  label: string;
  editable: boolean;
  precedence: number; // higher wins
  dir: string; // absolute path of this source's templates directory
}

// The four registry key shapes (grammar in server.py _key_segments).
export type KeyKind = "simple" | "compound" | "wildcard" | "directory";

// -- Inventory (GET /api/templates/inventory) --------------------------------

// One resolved template folder. If a user folder shadows a core folder of the
// same name, ONE entry is emitted with source="user" and shadowsCore=true.
export interface InventoryTemplate {
  name: string;
  source: string; // source id
  editable: boolean;
  hasIcon: boolean;
  hasCondition: boolean; // folder has a condition.py gate (SPEC CT-12)
  usedBy: string[]; // registry keys whose effective list contains this name
  shadowsCore: boolean;
  path: string; // absolute path of this template's folder on disk (core or user)
}

export interface TemplateInventory {
  sources: TemplateSource[];
  templates: InventoryTemplate[];
}

export function getTemplateInventory(): Promise<TemplateInventory> {
  return getJson<TemplateInventory>("/api/templates/inventory");
}

// -- Registry (GET/PUT /api/templates/registry) ------------------------------

// One name in an entry's effective ordered list, resolved to a folder. A name
// the registry references but that no folder backs has exists:false (broken).
export interface RegistryTemplateRef {
  name: string;
  source: string; // source id the folder comes from
  exists: boolean;
  hasIcon: boolean;
}

export interface RegistryEntry {
  key: string;
  keyKind: KeyKind;
  templates: RegistryTemplateRef[]; // effective ordered list, first = default
  resolvedSource: string; // which source supplied the effective value
  overridesCore: boolean; // the user registry defines this key
  disabled: boolean; // effective value is null (previews disabled)
  coreTemplates: string[] | null; // builtin registry's names for this key, or null
  userValue?: string[] | null; // raw user-registry value, present only if a user key exists
  error?: string | null; // set when this key's registry value is invalid (fails to resolve)
}

export interface RegistryResult {
  sources: TemplateSource[];
  entries: RegistryEntry[];
  builtin_registry: string; // path (back-compat)
  user_registry: string; // path (back-compat)
  error?: string | null;
}

export function getTemplateRegistry(): Promise<RegistryResult> {
  return getJson<RegistryResult>("/api/templates/registry");
}

// Upsert one USER-registry key. value = ordered names, or null to disable.
// Returns the recomputed entry.
export function putRegistryBinding(key: string, value: string[] | null): Promise<RegistryEntry> {
  return putJson<RegistryEntry>("/api/templates/registry", { key, value });
}

// Remove a user override (revert to core). Returns the recomputed entry, or a
// tombstone when no such key exists at all any more.
export interface RegistryRemoved {
  key: string;
  removed: true;
}

export function resetRegistryBinding(key: string): Promise<RegistryEntry | RegistryRemoved> {
  return postJson<RegistryEntry | RegistryRemoved>("/api/templates/registry/reset", { key });
}

// Which registry key (if any) governs previews for one path — the seam
// FallbackPreview uses to offer "restore default previews" instead of sending
// someone off to hand-edit registry.json. `{key: null}` means neither registry
// has a matching key at all (nothing to fix from here). Either error field can
// be set even alongside a resolved `key`: a registry FILE that fails to parse
// can hide a key that would otherwise have matched — a distinct problem from
// one key's own `error`. The two error fields are NEVER merged: `registryError`
// (the user's registry.json) is the one `repairTemplateRegistry` can act on;
// `coreRegistryError` (the packaged core registry) has no in-app fix — it's
// immutable package data, healed only by the app's own startup check — so a
// caller must not offer the repair action for it.
type RegistryFileErrors = { registryError?: string | null; coreRegistryError?: string | null };
export type RegistryEntryForPath = (RegistryEntry & RegistryFileErrors) | ({ key: null } & RegistryFileErrors);

export function getRegistryEntryForPath(path: string, isDir: boolean): Promise<RegistryEntryForPath> {
  const params = new URLSearchParams({ path, is_dir: isDir ? "true" : "false" });
  return getJson<RegistryEntryForPath>("/api/templates/registry/for-path?" + params.toString());
}

// Repair a USER registry.json that fails to parse: the unreadable file is
// backed up alongside itself (never deleted) and replaced with a fresh empty
// one. A no-op (`repaired: false`) when the file already parses or is absent.
export interface RegistryRepairResult {
  repaired: boolean;
  backupPath?: string;
}

export function repairTemplateRegistry(): Promise<RegistryRepairResult> {
  return postJson<RegistryRepairResult>("/api/templates/registry/repair", {});
}

// -- Export / import ---------------------------------------------------------
// Export works for ANY template (core or user); import always lands in the user
// source. Zips are folders only (no registry.json).

// GET url for the export zip (folders only, no registry.json). Names go out as
// repeated `names=` params (not comma-joined) so a folder name containing a
// comma round-trips intact.
export function exportTemplatesUrl(names: string[]): string {
  const qs = names.map((n) => "names=" + encodeURIComponent(n)).join("&");
  return "/api/templates/export?" + qs;
}

// Download the export zip via fetch + blob rather than a bare <a download>, so a
// non-2xx JSON error (unknown name, missing names) is surfaced to the caller
// instead of being silently saved as a corrupt `.zip`. Throws on failure.
export async function downloadTemplatesExport(names: string[]): Promise<void> {
  const res = await fetch(exportTemplatesUrl(names));
  if (!res.ok) {
    let message = `export failed (${res.status})`;
    try {
      const body = await res.json();
      if (body && typeof body.error === "string") message = body.error;
    } catch {
      /* non-JSON error body — keep the status-based message */
    }
    throw new Error(message);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = "fused-render-templates.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    // Give the click a tick to start the download before releasing the blob.
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }
}

// Delete one USER template folder (core templates are read-only, 404 here).
// With cleanRegistry the USER registry is also swept of bindings referencing
// the name (a user key whose value is emptied by the sweep is removed — revert
// to core, never left as [] which means disabled, D109); without it bindings
// are left untouched and resolve broken until rebound.
export function deleteTemplate(
  name: string,
  cleanRegistry: boolean,
): Promise<{ deleted: string; registryKeysCleaned?: string[] }> {
  return postJson<{ deleted: string; registryKeysCleaned?: string[] }>("/api/templates/delete", {
    name,
    cleanRegistry,
  });
}

// Author-recommended binding key for a staged template (from the bundle's
// recommendation.json). Status reflects this machine's registry:
//   new           — key not bound here yet (accepted by default)
//   already-bound — this template is already on that key (no-op, informational)
//   disabled      — the user disabled this key locally (off by default)
export type RecommendedKeyStatus = "new" | "already-bound" | "disabled";

export interface RecommendedKey {
  key: string;
  status: RecommendedKeyStatus;
}

// One candidate template found in an uploaded zip (a top-level directory).
export interface ImportItem {
  name: string;
  valid: boolean; // has template.html
  hasTemplateHtml: boolean;
  conflictsExisting: boolean; // a user folder of this name already exists
  fileCount: number;
  recommendedKeys?: RecommendedKey[];
}

// Step 1 of import: staged, not yet committed.
export interface ImportStageResult {
  importId: string;
  expiresInSec: number;
  items: ImportItem[];
  warnings: string[];
}

export type ImportResolution = "overwrite" | "skip" | "keep-both";

// Step 2 result: what the commit did per item.
export interface ImportCommitResult {
  imported: string[];
  skipped: string[];
  overwritten: string[];
  renamed: Record<string, string>;
  // Bindings the commit applied (key → FINAL template name, after any
  // keep-both rename). Absent/empty when no bindings were requested.
  bindingsApplied?: { key: string; template: string }[];
}

// Stage an import zip (step 1). Multipart — the browser sets the multipart
// boundary Content-Type, so we must NOT set it ourselves; the X-Fused header
// still forces the write-guard preflight (same guard as mutateJson).
export async function importTemplates(file: File): Promise<ImportStageResult> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/templates/import", {
    method: "POST",
    headers: { "X-Fused": "1" },
    body: form,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data as ImportStageResult;
}

// Commit a staged import (step 2): resolve conflicts and move into place.
// `bindings` maps ORIGINAL staged names (even for keep-both renames — the
// server maps to the final name) to the registry keys to bind.
export function commitImport(
  importId: string,
  resolutions: Record<string, ImportResolution>,
  bindings?: Record<string, string[]>,
): Promise<ImportCommitResult> {
  return postJson<ImportCommitResult>(
    "/api/templates/import/" + encodeURIComponent(importId) + "/commit",
    bindings ? { resolutions, bindings } : { resolutions },
  );
}

// -- New template (POST /api/templates/new) ----------------------------------
// Scaffold a new USER template folder and, for each extension, bind it as the
// default for that key. `bindings` lists the registry keys that were bound.
export interface NewTemplateResult {
  ok: true;
  name: string;
  path: string;
  bindings: string[];
}

// Extensions are dot-prefixed (e.g. ".csv"); [] scaffolds the folder with no
// bindings (add them later via the bindings UI).
export function createTemplate(name: string, extensions: string[]): Promise<NewTemplateResult> {
  return postJson<NewTemplateResult>("/api/templates/new", { name, extensions });
}

// Resolve a claude-cli:// deep link into a user template's folder. The
// caller navigates to the returned URL (window.location.href) so the OS
// hands it to Claude Code's registered scheme handler.
export function openTemplateInClaude(name: string): Promise<{ url: string }> {
  return postJson<{ url: string }>("/api/templates/open-in-claude", { name });
}

// -- Apps (GET /api/apps, POST /api/apps/new) ---------------------------------
// An app folder one to three levels under the workspace, found by a bounded
// recursive walk (the rules live in app_listing.workspace_apps: a page makes a
// folder an app — any *.html at depth 1 or 2, an index.html at depth 3, nothing
// deeper; a page-less folder is a shelf, walked but never listed).
// `tag` is the FIRST path segment — any folder qualifies, there is no fixed tag
// set, and a third-level app carries the same tag as its second-level
// neighbours. `entry_html` is the app's "/" route entry file (absolute path);
// the workspace walk only lists folders with a page, so it is non-null in
// practice. `title` comes from that file's <title>, null falls back to the
// folder name in the UI.
export interface AppInfo {
  name: string;
  tag: string;
  path: string;
  entry_html: string | null;
  // The file a card opens and previews — the entry HTML for an app of the
  // folder-with-a-page shape. Reported separately from `entry_html`, which is
  // the narrower claim that the entry is a renderable page and so the only one
  // the HTML-only /render iframe may be pointed at. Optional for older backends
  // that predate the key — read it through entryOf(), never directly.
  entry?: string | null;
  // The app's authored thumbnail: an absolute path to a `preview.png` at the
  // folder's root, or null when there is none (and undefined on backends that
  // predate the key). A card renders it through /api/fs/raw INSTEAD of the live
  // scaled iframe of `entry_html` — an author's chosen still beats whatever the
  // page happens to look like with no data in it.
  preview_image?: string | null;
  // The authored category from the app folder's `metadata.json` (the showcase
  // repo's per-app metadata shape), or null when absent/invalid. Undefined on
  // older backends. Apps without one only appear under the "All" filter.
  category?: string | null;
  title: string | null;
  // Last-modified time, epoch seconds. Optional/null for servers that don't
  // report it (older backends) — those sort last in the Home grid.
  updated_at?: number | null;
  // Last-opened time, epoch seconds, from the app recents store
  // (~/.fused-render/app_recents.json). Null for an app never opened, and
  // undefined on older backends — both fall back to updated_at in sortApps.
  opened_at?: number | null;
}

export function getApps(): Promise<{ apps: AppInfo[] }> {
  return getJson<{ apps: AppInfo[] }>("/api/apps");
}

// (postAppOpen is gone — D301: the SERVER records app opens when GET /render
// serves a page carrying the fused-app marker; no client post feeds opened_at
// any more. The endpoint survives server-side for older clients only.)

// The folder's app entry page (its first top-level .html carrying
// `<meta name="fused-app">`, resolved by the server's one copy of the rule) or
// null. Feeds the explorer's "Open app" button.
export function getAppEntry(path: string): Promise<{ entry: string | null }> {
  return getJson<{ entry: string | null }>(
    `/api/apps/entry?path=${encodeURIComponent(path)}`,
  );
}

// Scaffold a new app folder and (optionally) kick off a Claude session seeded
// with `prompt`. 409 = name collision, 400 = bad name — both surface via the
// thrown HttpError's message for inline display.
export interface NewAppResult {
  path: string;
  entry_html: string;
  // Whether a Claude session was actually kicked off for the prompt.
  session_started: boolean;
  // The live run, for attaching to the session that was just started; null
  // when no prompt was given or the spawn failed.
  run_id: string | null;
  // Why the session did not start (claude CLI missing, spawn failure). The
  // app itself was created either way — surface this so a prompt that went
  // nowhere isn't silent. Null when it started, or when there was no prompt.
  session_error: string | null;
}

export function createApp(name: string, prompt: string): Promise<NewAppResult> {
  return postJson<NewAppResult>("/api/apps/new", { name, prompt });
}

// -- Claude sessions (GET /api/claude-sessions) -------------------------------
// Project folders that hold Claude Code session transcripts, for the
// Explorer homepage's "Claude sessions" tab — one entry per folder, newest
// session first. `path` is the real project directory (read server-side from
// each transcript's own `cwd`, not decoded from ~/.claude/projects'
// filename), so it's ready to pass straight to navigate(path, {isDir:true}).
export interface ClaudeSessionFolder {
  path: string;
  lastActive: string;
}

export function getClaudeSessionFolders(): Promise<{ folders: ClaudeSessionFolder[] }> {
  return getJson<{ folders: ClaudeSessionFolder[] }>("/api/claude-sessions");
}

// -- Claude sessions, one row each (GET /api/claude-sessions/summaries) --------
// Every Claude Code session on this machine, for the Schedule page's task views
// (shell/ScheduleTaskViews.tsx). A scheduled task and a chat are the same kind
// of thing — work Claude did in a folder — so the tree and the board show both,
// and this is the chat half.
//
// `status` is the session collapsed into the board's own vocabulary
// (in_progress / done / archived), decided server-side so the client never
// re-derives it. `running` is a separate fact and cannot be folded into it: a
// session is `in_progress` whether or not a turn is in flight right now, and it
// is the in-flight one the live pulse draws.
export interface ClaudeSessionSummary {
  session_id: string;
  name: string;
  cwd: string;
  started_at: string;
  last_active: string;
  running: boolean;
  status: "in_progress" | "done" | "archived";
}

export function getClaudeSessionSummaries(): Promise<{ sessions: ClaudeSessionSummary[] }> {
  return getJson<{ sessions: ClaudeSessionSummary[] }>("/api/claude-sessions/summaries");
}

// The Board's drag: a chat card moved between In Progress / Done / Archive
// writes the SAME triage.json the sessions Inbox owns (the server merges, so
// the record's note/tags/read survive). Tasks never go through this — their
// column moves are the scheduler's own cancel/restore calls.
export function setSessionTriage(
  sessionId: string,
  status: "in_progress" | "done" | "archived",
): Promise<{ ok: boolean }> {
  return postJson<{ ok: boolean }>("/api/claude-sessions/triage", {
    session_id: sessionId,
    status,
  });
}

// -- Tasks (GET /api/tasks) ---------------------------------------------------
// One row per TASK, where a task IS a Claude session: same thing, one name. A
// task owns a THREAD, and the thread's MESSAGES are every prompt sent into it —
// typed in a chat, typed in the template's chat, or fired by the scheduler. The
// three sources differ only in how the message arrived; the thread does not
// care.
//
// `key` is the join everything else uses. A task that has run has a session id
// and uses it; a task that is only a future schedule entry has no session yet
// (Claude Code mints the id on the first turn) and uses `pending:<entry-id>`
// until it runs. The server rekeys it in place at that point, so `task_id`
// survives the transition — which is the whole reason ids are allocated at
// creation rather than derived from the session.
export interface TaskMessage {
  message_id: string; // MSG-001, per task, oldest first
  kind: "scheduled" | "chat";
  body: string;
  // TWO times, because a scheduled message has two and they are not the same
  // fact. Both are epoch seconds.
  //
  //   at     — what it was SCHEDULED FOR. The time the user picked, and the
  //            only thing the calendar places a chip by. It never moves.
  //   ran_at — when it ACTUALLY RAN: the transcript's own timestamp for the
  //            prompt, falling back to when the scheduler claimed it. 0 for a
  //            message that has not run (pending, cancelled, missed).
  //
  // They differ whenever the app was not open at the due minute. Catch-up is
  // unbounded, so a message scheduled for Thursday and caught up on Saturday is
  // ordinary, not exotic — `at` is Thursday and `ran_at` is Saturday. Placing
  // it by `ran_at` was the original bug: the chip left the day that was asked
  // for and appeared on the day the app happened to reopen.
  //
  // For a chat message the two are equal: a typed message was scheduled for the
  // moment it was typed.
  at: number;
  ran_at: number;
  state:
    | "pending"
    | "sending"
    | "sent"
    | "error"
    | "missed"
    | "cancelled"
    | "skipped";
  unread: boolean;
  entry_id: string; // schedule entry; "" for a chat message
  template_id: string; // the recurring message this is an occurrence of
  turn: "done" | "idle" | "unknown" | "";
  anchor: string; // transcript record uuid, for scroll-to; "" if unknown
}

export interface Task {
  key: string;
  task_id: string; // TASK-003 — numbered per project, allocated once, never reused
  project: string; // the FOLDER: a task on ~/x/foo.py belongs to project ~/x
  target: string; // what the task actually points at (may be that file)
  session_id: string; // "" until the first run
  title: string;
  // Which source won: the user's own title, Claude Code's own `ai-title`
  // record, the first line of the session's own first prompt (`message`), or —
  // with no readable transcript to take that from — the first line of a message
  // merely SCHEDULED at the session (`entry`). The last two are named apart
  // because only `entry` can be the message a form is composing right now; see
  // sessionTitleOf and tasks.py `_title`.
  title_source: "user" | "ai" | "message" | "entry";
  description: string;
  // Decided by the SERVER, once, for every view — List, Board and Calendar all
  // read this rather than each deriving a column from the newest message.
  //
  // `failed` is a status of its own and not a kind of `done`: a run that
  // started and broke is news, and filing it under done meant a view had to
  // remember to read the boolean below to say so — which is how a failed task
  // could simply not be shown.
  //
  // A SKIPPED occurrence is `archived`, not `failed`. It was filed away and
  // never attempted (the coalescer dropped it, or the user cancelled it), which
  // is a different thing from a run that tried and broke; only something that
  // actually ran can fail.
  status: "upcoming" | "in_progress" | "done" | "failed" | "archived";
  // Did the newest message's run break? `status` is the authority on which
  // column a task belongs in; this is the raw fact underneath it, and the two
  // disagree in exactly one direction — a task triaged to `done`, or one whose
  // session is live again, reads a different status while this stays true.
  // Anything asking "which column" should read `status`.
  failed: boolean;
  live: boolean;
  unread: number;
  last_active: number;
  message_count: number;
  // WHEN THIS NEXT RUNS, and WHICH schedule entry that run is: `min(at)` over
  // every PENDING entry the task has, epoch seconds, decided by the server
  // (tasks.py `_next_run`) over the whole set rather than over the three
  // messages below. 0 / "" when nothing is pending.
  //
  // They exist because the three-message window cannot answer the question. The
  // Board orders Upcoming by soonest-next-run, and `messages` is the three
  // newest by `at` — so an OVERDUE pending (ordinary here: past scheduling is
  // allowed and catch-up is unbounded) can be pushed out of it by two runs plus
  // next month's occurrence, leaving the lane to sort by a LATER time and bury
  // the work that should go first.
  //
  // `next_run_entry` is what makes the BUTTON agree with that order: run-now
  // sends an entry id, so a card promoted on a run the row could not name would
  // fire a different message than the one its place in the lane promised. The
  // two widen together or not at all.
  //
  // OPTIONAL because an older server does not send them. tasks-lib.nextRunAt and
  // tasks-lib.runNowTarget both fall back to reading the window, which is the
  // same (bounded) answer they gave before these existed.
  next_run?: number;
  next_run_entry?: string;
  // The three most recent, newest first. The rest need the endpoint below —
  // this list is built by a tail parse because it runs for every row, and a
  // full transcript parse per task would not survive a few hundred of them.
  messages: TaskMessage[];
}

export function getTasks(): Promise<{ tasks: Task[] }> {
  return getJson<{ tasks: Task[] }>("/api/tasks");
}

// "Show more": the whole thread, newest first. Deliberately a separate call —
// this one is allowed to parse the full transcript because it is one task, on
// demand, and never on the listing path.
export function getTaskMessages(key: string): Promise<{ messages: TaskMessage[] }> {
  return getJson<{ messages: TaskMessage[] }>(
    `/api/tasks/${encodeURIComponent(key)}/messages`,
  );
}

// Unread means "I have not seen the response to this message", so it is tracked
// per message, not per task, and clicking through to the transcript is what
// clears it. Marking one message read must leave older unread ones alone.
export function markTaskMessageRead(
  key: string,
  messageId: string,
): Promise<{ ok: boolean; unread: number }> {
  return postJson<{ ok: boolean; unread: number }>("/api/tasks/read", {
    key,
    message_id: messageId,
  });
}

// The whole task, in ONE request. Per-message is the right MODEL and stays the
// default (see above), but it was also the only way to clear a task, so "I have
// seen all of this" cost one click per row — 89 of them on the longest real
// thread. Same endpoint, wider object: the server enumerates the thread, marks
// the messages that are actually unread (a pending one is left alone, so it
// cannot fire already-read) and answers with what is left, which is 0 unless
// something arrived while the request was in flight.
export function markWholeTaskRead(
  key: string,
): Promise<{ ok: boolean; unread: number }> {
  return postJson<{ ok: boolean; unread: number }>("/api/tasks/read", {
    key,
    all: true,
  });
}

// Every scheduled message in a time window, which is the one question the
// listing above cannot answer: `Task.messages` holds only the three most recent,
// and a calendar draws a week. Without this the grid under-draws — a task whose
// runs fall outside its last three messages simply has no chips on those days.
//
// Separate from the listing rather than a parameter on it, deliberately: the
// window changes on every arrow press and the listing's poll does not, so
// folding them together would drag a 200-task tail parse behind each step.
// `from` inclusive, `to` exclusive, epoch seconds — local midnights, because the
// grid's columns are local days.
export function getTasksScheduled(
  from: number,
  to: number,
): Promise<{ items: { task_key: string; message: TaskMessage }[] }> {
  return getJson<{ items: { task_key: string; message: TaskMessage }[] }>(
    `/api/tasks/scheduled?from=${Math.floor(from)}&to=${Math.floor(to)}`,
  );
}

// -- The queue (GET /api/schedule/queue) --------------------------------------
// Nothing fires while the app is not running, and catch-up for a one-off is now
// unbounded — so opening the app after a week away can find real work waiting.
// Three lists, narrowing: `queued` is past due and not yet claimed, in the order
// it will run; `running` is claimed and spawning; `live` is a turn actually in
// flight — sent, with no verdict yet.
//
// `live` is the one a person needs most and the one nothing used to report. A
// run parked on a permission prompt looks identical to a slow one from outside,
// so until the dock could name it there was no way to find the prompt and
// answer it — the run just sat there.
//
// Nothing scheduled for LATER appears in any of them. "Queued" means about to
// run; a list that also held next Tuesday would be answering a different
// question, and the calendar already answers that one. The dock, bottom right,
// is where all three are drawn and cancelled.
export function getScheduleQueue(): Promise<{
  queued: ScheduledMessage[];
  running: ScheduledMessage[];
  live?: ScheduledMessage[];
}> {
  return getJson<{
    queued: ScheduledMessage[];
    running: ScheduledMessage[];
    live?: ScheduledMessage[];
  }>("/api/schedule/queue");
}

// Cancelling races the claim, and the server resolves it honestly: an entry
// already claimed for sending is refused rather than corrupted, and comes back
// in `refused` so the UI can say why instead of silently dropping it.
export function cancelQueued(
  entryIds: string[] | "all",
): Promise<{ ok: boolean; cancelled: string[]; refused: string[] }> {
  const body = entryIds === "all" ? { all: true } : { entry_ids: entryIds };
  return postJson<{ ok: boolean; cancelled: string[]; refused: string[] }>(
    "/api/schedule/queue/cancel",
    body,
  );
}

// -- AI Models (GET /api/ai-models) -------------------------------------
// What the Hugging Face cache holds on this machine, for the sidebar's "Local
// models" page (shell/AiModels.tsx). One entry per cached repo, biggest
// first; `size` is bytes actually on disk (the server measures blobs and skips
// the snapshot symlinks pointing at them), so the sizes sum to `totalSize`.
// `path` is the repo's cache folder, ready for navigate(path, {isDir:true}).
export interface AiModelRepo {
  id: string;
  /** Cache folder name ("models--org--name") — what a delete request names. */
  dir: string;
  kind: "model" | "dataset" | "space";
  path: string;
  size: number;
  files: number;
  /** Epoch seconds of the newest file in the repo folder, or null if unknown. */
  mtime: number | null;
  /** Newest atime — "last read", which is what pruning by age asks about. */
  lastUsed: number | null;
  /**
   * When the repo first landed on this machine (its oldest file). NOT the
   * model's release date: that is Hub metadata, and this page never goes to
   * the network.
   */
  added: number | null;
  /** What the model is for ("text generation", "image generation"), or null. */
  task: string | null;
  /** Where `task` was read from — a pipeline_tag is the Hub's own answer, an
   *  architecture is our reading of one, and the UI distinguishes them. */
  taskSource: string | null;
  /** One sentence on what the task MEANS (what goes in, what comes out), for
   *  the hover — the labels are the Hub's vocabulary, which is jargon until
   *  someone explains it. Null for a tag we have no sentence for. */
  taskHelp: string | null;
  library: string | null;
  /** Parameter count from the safetensors headers; null when the weights are in
   *  a format with no cheap header to read (.bin, .gguf). */
  params: number | null;
  /** True when `params` was recovered from PACKED weights — a 4-bit checkpoint
   *  stores eight weights per word, so the count rests on the declared bit
   *  width rather than on unpacked shapes, and the card marks it "≈". */
  paramsEstimated: boolean;
  /** What the checkpoint declares about its weight width ("4-bit"), or null. */
  quantization: string | null;
  /** Which capability could LOAD this locally, or null when nothing here
   *  serves it (a dataset, an embedding model, a VLM). Decided server-side so
   *  the page holds no second copy of the task→capability mapping. */
  capability: string | null;
  /** Which BACKEND would load this repo, read from the format on disk — the
   *  same check the runner's own `load()` makes, so the tag cannot promise a
   *  load that then fails. Null when nothing that ships reads this format
   *  (`openai/whisper-large-v3`, a GGUF-only repo), which is a different
   *  answer from a runner that exists and cannot run HERE — that one comes
   *  back with `available: false` and the registry's reason. */
  engine: {
    code: string;
    /** The FULL name, for anything that must match the Preferences picker. */
    label: string;
    /** Without the platform qualifier — what the card's tag shows. */
    shortLabel: string;
    available: boolean;
    reason: string | null;
  } | null;
  /**
   * Set when this repo is not a model at all but a PART of one — the quantized
   * transformer the Diffusers recipe swaps in, the Silero detector the MLX
   * whisper engine filters silence with. This app downloaded it; the user never
   * chose it, and nothing can load it on its own. Null for everything else.
   *
   * The card wears it instead of the engine tag: those repos read
   * `engine: null` and wore "no engine", which is true and explains nothing
   * about a 2.4GB row somebody is about to delete.
   */
  component: {
    /** This repo's own id, so the object is self-contained. */
    id: string;
    /** The repo it belongs to, or null when it belongs to an ENGINE (the VAD
     *  serves every transcription, whatever model is loaded). */
    of: string | null;
    /** What it is part of, in the words the rest of the UI uses. */
    owner: string;
    /** The noun: "quantized transformer", "speech detector". */
    part: string;
    /** The whole story, including what deleting it costs. */
    what: string;
    /** The one file fetched out of it. */
    file: string;
  } | null;
  revisions: number;
  refs: string[];
}

export interface AiModelsResult {
  cacheDir: string;
  hfHome: string;
  /** False when nothing has ever been downloaded — the cache dir isn't there. */
  exists: boolean;
  totalSize: number;
  repos: AiModelRepo[];
}

export function getAiModels(): Promise<AiModelsResult> {
  return getJson<AiModelsResult>("/api/ai-models");
}

// One repo's revisions, fetched when a row is expanded (the listing doesn't
// resolve every snapshot symlink for every repo). `size` is what deleting THIS
// revision would free — blobs no sibling revision references — and `shared` is
// what it holds in common with them and would leave behind.
export interface AiModelRevision {
  commit: string;
  refs: string[];
  size: number;
  shared: number;
  files: number;
  mtime: number | null;
}

export function getAiModelRevisions(
  dir: string,
): Promise<{ repo: string; revisions: AiModelRevision[] }> {
  return getJson<{ repo: string; revisions: AiModelRevision[] }>(
    "/api/ai-models/revisions?repo=" + encodeURIComponent(dir),
  );
}

// Delete cached repos and/or single revisions. A target with no `revision` is
// the whole repo folder. Targets are named by cache FOLDER NAME — the server
// builds every path itself from the cache dir it resolved (D250).
//
// The reply is the whole listing, re-read from disk after the deletions, plus
// what was freed and any per-target failures — so the page swaps in fresh state
// instead of patching rows it hopes are still true.
export interface AiModelDeleteTarget {
  dir: string;
  revision?: string | null;
}

export interface AiModelDeleteFailure {
  dir: string | null;
  revision: string | null;
  error: string;
}

export type AiModelsDeleteResult = AiModelsResult & {
  freed: number;
  failures: AiModelDeleteFailure[];
};

export function deleteAiModels(
  targets: AiModelDeleteTarget[],
): Promise<AiModelsDeleteResult> {
  return postJson<AiModelsDeleteResult>("/api/ai-models/delete", { targets });
}

// -- Hub search (POST /api/ai-models/hub/search) -------------------------------
// The other half of the AI models page: what the Hugging Face Hub has THAT THIS
// APP CAN RUN, with every result already told apart from what this disk holds
// (`local`). The server makes the outbound request — this module never talks to
// huggingface.co — so one place holds the token, the timeout and the cache.
//
// Every row is downloadable (D313): the server drops anything whose pipeline
// tag no runner serves, anything with no tag at all, and anything gated or
// private. `capability` is therefore never null, and it is what a Download
// button hands to `downloadAiModel`.
export interface HubModelLocal {
  /** "downloaded" has a materialised snapshot; "partial" is an interrupted pull. */
  state: "downloaded" | "partial" | "none";
  size?: number;
  files?: number;
  lastUsed?: number | null;
  /** Ready for navigate(path, {isDir:true}) — absent unless it is here. */
  path?: string;
  dir?: string;
}

export interface HubModel {
  id: string;
  /** Friendly task label — the SAME vocabulary the cached cards use. */
  task: string | null;
  taskHelp: string | null;
  pipelineTag: string | null;
  /** Which runner would load this. Never null — the server drops rows it
   *  cannot classify rather than guessing a capability for them. */
  capability: string;
  /** What stands between the reader and this repo, when anything does.
   *  `"auto"` — accept the licence while signed in and it is yours; `"manual"`
   *  — the owner grants access by hand; `null` — nothing. Gated repos are
   *  RESULTS (D316): the card says which gate rather than the search pretending
   *  the model does not exist. */
  gated: "auto" | "manual" | null;
  library: string | null;
  downloads: number | null;
  likes: number | null;
  updated: string | null;
  params: number | null;
  /** Bytes recovered from the dtype map — an estimate, and shown with "≈". */
  estimatedSize: number | null;
  local: HubModelLocal;
  url: string;
}

export interface HubSearchResult {
  models: HubModel[];
  query: { q: string; task: string; sort: string; limit: number };
  /** Present INSTEAD of results when the Hub could not be reached or refused. */
  error?: string;
  endpoint?: string;
  authenticated?: boolean;
}

export type HubSort = "downloads" | "likes" | "updated" | "created";

export function searchHubModels(opts: {
  q?: string;
  task?: string;
  sort?: HubSort;
  limit?: number;
}): Promise<HubSearchResult> {
  // A POST, unlike every other read in this file. Search is the one that leaves
  // the machine — the server calls the Hub with the user's token — so it takes
  // the shape its effect deserves and carries the D3 guard with it. See the
  // endpoint's docstring.
  return postJson<HubSearchResult>("/api/ai-models/hub/search", {
    q: opts.q,
    task: opts.task,
    sort: opts.sort,
    limit: opts.limit,
  });
}

export interface HubTask {
  /** The Hub's own pipeline tag — what the filter actually sends. */
  tag: string;
  label: string;
  help: string | null;
}

/** The task filters the page may offer — only the ones a registered runner
 *  serves, which is why this is asked of the server rather than listed here. */
export function getHubTasks(): Promise<{ tasks: HubTask[] }> {
  return getJson<{ tasks: HubTask[] }>("/api/ai-models/hub/tasks");
}

// -- Local inference (GET/POST /api/ai/runtime, /api/ai/catalog) ---------------
// What this machine is HOLDING IN MEMORY, as opposed to what it has on disk
// (the AI models endpoints above). A model here is a resident process with a
// cost, so the page can show that cost and give the memory back.
//
// load/download answer with a jobId rather than a finished model: a cold load is
// a multi-GB download, and it is watched through the download manager.
export interface AiRunner {
  code: string;
  capability: string;
  /** The FULL name — "MLX LM (Apple Silicon)". `label` means the full one
   *  everywhere on the wire; a surface that wants the short one asks for
   *  `shortLabel` by name rather than getting a quietly different string. */
  label: string;
  /** Without the platform qualifier — "MLX LM". What every surface but the
   *  Preferences engine picker shows. */
  shortLabel: string;
  /** What using this backend is like, when there is something worth saying. */
  note: string | null;
  available: boolean;
  /** Why not, in words — "needs Apple Silicon…". Null when it is available. */
  reason: string | null;
  /** Whether this is the runner the capability is ACTUALLY using — which since
   *  D302 is a different question from `available`. Two whisper runners are
   *  available on an Apple Silicon machine and exactly one is active; a reader
   *  that only sees availability cannot say which engine served it. False for
   *  every runner of a capability nothing can serve. */
  active: boolean;
}

export interface AiLoadedModel {
  model: string;
  capability: string;
  runner: string;
  /** venv | starting | downloading | loading | ready | error */
  state: string;
  detail: string | null;
  error: string | null;
  /** RSS of the worker process. Not the model's size — see SPEC AI-8. */
  residentBytes: number | null;
  /** "cuda" | "mps" | "cpu" — where the weights actually landed, as the worker
   *  reported it. Null from a runner that does not say. The page shows it
   *  because a model answering at a few words a second on a CPU is working
   *  perfectly and looks broken, and this is the whole explanation. */
  device: string | null;
  loadedAt: number | null;
  startedAt: number;
  /** The download-manager row for this model's bring-up. */
  jobId: string;
}

/** A weights-only fetch in flight: on disk, not in memory. The BYTES live in the
 *  job row (`jobId`); this only says the pull is still running — which is what
 *  keeps a Discover card from claiming "✓ downloaded" the moment it was asked. */
export interface AiDownload {
  model: string;
  capability: string;
  jobId: string;
  startedAt: number;
}

export interface AiRuntime {
  runners: AiRunner[];
  loaded: AiLoadedModel[];
  downloading: AiDownload[];
  totalResidentBytes: number | null;
}

export function getAiRuntime(): Promise<AiRuntime> {
  return getJson<AiRuntime>("/api/ai/runtime");
}

/** One curated suggestion. Deliberately says nothing about whether you HAVE it:
 *  the server's catalog is the curation, and what is on this disk is the cache
 *  listing's answer — joined by the page so both tabs mean one thing by it. */
export interface AiCatalogModel {
  id: string;
  label: string;
  /** The download in GB, or null when nobody has measured it — shown as "—"
   *  rather than as a number someone would plan a multi-GB fetch around. */
  size_gb: number | null;
  /** Why you would or would not pick this one. Null on a CACHED entry: nobody
   *  wrote a note for a repo the user found themselves, and null says so where
   *  prose generated from a repo id would claim otherwise. */
  note: string | null;
  /** Which half of the payload this came from (D323). "curated" is the
   *  hand-maintained shortlist; "cached" is a repo found on this disk that the
   *  curation has never heard of — downloaded from the Discover tab's Hub
   *  search, and previously invisible to every picker in the app.
   *
   *  The Discover tab's "Suggested models" grid renders the CURATED half only:
   *  the Local tab is already the answer to "what is on my disk", and the same
   *  repo in both grids would read as two different things. */
  source: "curated" | "cached";
  /** Whether it is on this disk. Always true for a cached entry; on a curated
   *  one it is what the checkmark means. */
  downloaded: boolean;
  /** Whether a worker is holding it RIGHT NOW — read live from the supervisor,
   *  unlike `downloaded`, which comes from a memoised disk scan. */
  loaded: boolean;
}

export interface AiCatalogCapability {
  capability: string;
  runner: string | null;
  /** The backend in words — "MLX LM (Apple Silicon)", "Transformers (PyTorch)".
   *  One capability can have more than one runner (text generation has two
   *  since D293), so which one this machine resolved is worth naming. */
  runnerLabel: string | null;
  /** The same, without the platform qualifier — what the Discover heading
   *  shows ("via MLX Whisper"). That caption says which backend these
   *  suggestions belong to, not which backend to pick. */
  runnerShortLabel: string | null;
  /** What using that backend is LIKE, when there is something worth saying —
   *  the CPU-speed warning for PyTorch. A standing fact about the runner, not a
   *  claim about this machine: the device a model actually got is on the loaded
   *  card, and is not knowable until one has run. */
  runnerNote: string | null;
  available: boolean;
  reason: string | null;
  default: string | null;
  models: AiCatalogModel[];
}

export function getAiCatalog(): Promise<{ capabilities: AiCatalogCapability[] }> {
  return getJson<{ capabilities: AiCatalogCapability[] }>("/api/ai/catalog");
}

export interface AiLoadStarted {
  jobId: string;
  model: string;
  state: string;
}

export function loadAiModel(model: string, capability?: string): Promise<AiLoadStarted> {
  return postJson<AiLoadStarted>("/api/ai/runtime/load", { model, capability });
}

export function downloadAiModel(model: string, capability?: string): Promise<AiLoadStarted> {
  return postJson<AiLoadStarted>("/api/ai/runtime/download", { model, capability });
}

export function unloadAiModel(model: string): Promise<AiRuntime & { stopped: boolean }> {
  return postJson<AiRuntime & { stopped: boolean }>("/api/ai/runtime/unload", { model });
}

// -- AI usage (GET /api/ai/metrics, SPEC AI-12) -------------------------------
// What `/api/ai` has generated in THIS server process: both tiers, in memory,
// gone on restart. `since` is what keeps that honest — every number here is
// "since the server started", never "today".

/** The counters, wherever they are counted: a bucket, the window, a model's
 *  row, a tier, or the whole process. */
export interface AiUsageCounts {
  /** Completions that reached a terminal frame. A cancelled local generation
   *  counts (it produced tokens); a call that failed or was abandoned
   *  mid-stream does not (nothing ever said how many tokens it made). */
  completions: number;
  /** Null means NOT REPORTED, never zero: a local worker counts what it
   *  generated and says nothing about the prompt it read (SPEC AI-3), so a row
   *  showing "0 read" for a local model would be inventing a fact. */
  input_tokens: number | null;
  output_tokens: number;
  /** Calls that reached for a model and got nothing back. NOT completions —
   *  and not malformed requests either, which never reached a model. */
  failures: number;
  /** Seconds the models spent generating, as the tiers themselves reported.
   *  Null when nothing in this row was timed. */
  seconds: number | null;
  /** `seconds` divided into the tokens that were TIMED — not into every token,
   *  since a cancelled generation reports tokens and no duration. Null when
   *  nothing was timed. */
  tokens_per_second: number | null;
}

/** One `bucket_seconds`-wide column of the graph. `t` is the bucket's START, in
 *  epoch SECONDS (not ms — it comes straight from the server's clock). */
export interface AiUsageBucket extends AiUsageCounts {
  t: number;
}

export interface AiUsageModel extends AiUsageCounts {
  /** The RESOLVED model id — "claude-opus-5", not the "opus" alias a caller may
   *  have sent — or "other models", the overflow row past the server's cap. */
  model: string;
  /** Which half served it — null on the "other models" overflow row, which is a
   *  mixture by construction and cannot claim either. */
  tier: AiUsageTier | null;
}

/** Which half of `/api/ai` served it, on the `/`-in-the-id seam AI-1 dispatches
 *  on — the server's own answer, not a guess made from the string here. */
export type AiUsageTier = "claude" | "local";

export interface AiUsage {
  /** When this process started counting, epoch seconds. */
  since: number;
  /** The server's clock when it answered — the right end of the axis. Used
   *  instead of Date.now() so a bucket never plots into the future. */
  now: number;
  bucket_seconds: number;
  /** The window actually served, after the server clamped what was asked. */
  window_minutes: number;
  /** How far back the store can ever answer, whatever `minutes` asks for. */
  retention_minutes: number;
  /** When the last completion landed, epoch seconds — null if none ever has.
   *  What tells "quiet for a while" from "never used". */
  last_completion_at: number | null;
  /** Since `since`. */
  totals: AiUsageCounts;
  /** The `window_minutes` the buckets cover. */
  window: AiUsageCounts;
  /** Since `since`, split by tier. Both keys are always present. */
  tiers: Record<AiUsageTier, AiUsageCounts>;
  /** Failures since `since`, by kind ("timeout", "ai_unavailable",
   *  "ai_error", "model_loading"), commonest first. "3 failed" and "3 timed
   *  out" send a user to different places. */
  failure_types: { type: string; count: number }[];
  /** Biggest generator first. */
  models: AiUsageModel[];
  /** Dense and oldest-first: every bucket in the window, zeros included, so a
   *  gap in traffic draws as a gap. Short of the full window only while the
   *  process is younger than it — nothing is emitted for time before counting
   *  began. */
  buckets: AiUsageBucket[];
}

export function getAiUsage(minutes: number, opts?: { signal?: AbortSignal }): Promise<AiUsage> {
  return getJson<AiUsage>("/api/ai/metrics?minutes=" + encodeURIComponent(String(minutes)), opts);
}

// -- Git repos (GET /api/git-repos) -------------------------------------------
// Git repositories on this machine, for the Explorer homepage's "Repos" tab.
// One entry per repo root, in path order; `path` is ready to pass straight to
// navigate(path, {isDir:true}).
//
// `indexed` is the state the tab has to distinguish: the list is derived from
// the file index, so a machine whose first scan has not finished yet is NOT the
// same as a machine with no repos, and `scanning` says whether one is in flight.
// Both come from the same vocabulary /api/index/status uses.
//
// `stale` means "this list may be out of date, reindexing" — a scan is running, or
// the index was built under older rules. It is NOT an error and NOT a reason to
// hide the list: an index is always slightly behind the filesystem, so a stale
// answer is the normal one. The server serves rows whenever it has them and only
// reports `indexed: false` when it genuinely cannot answer, in which case `reason`
// says which way ("no-index": nothing has ever been built; "outdated": the index
// predates repo detection, so its zero rows are not an answer).
export interface GitRepo {
  path: string;
}

export interface GitRepos {
  indexed: boolean;
  scanning: boolean;
  stale: boolean;
  reason?: "no-index" | "outdated" | null;
  repos: GitRepo[];
}

export function getGitRepos(): Promise<GitRepos> {
  return getJson<GitRepos>("/api/git-repos");
}

// -- AI completion (POST /api/ai) ---------------------------------------------
// The fused.ai relay: one non-streaming completion through the server's warm
// Claude Code CLI instance (server/ai.py). The shell uses this for small
// utility completions (e.g. naming a new app from its prompt on Home), not for
// anything conversational.
//
// No `model` is sent, deliberately: the server resolves one from the user's
// default-model preference and falls back to haiku when that is unset. A model
// named here would outrank the preference (that is the relay's precedence
// rule), so every one of these call sites must keep NOT naming one for the
// preference to mean anything.
export function aiComplete(prompt: string, system_prompt?: string): Promise<string> {
  return postJson<{ ok: boolean; result: { text: string } }>("/api/ai", {
    prompt,
    ...(system_prompt ? { system_prompt } : {}),
  }).then((r) => r.result.text);
}

// -- Scheduled Claude messages (/api/schedule) --------------------------------
// A durable list of "send this prompt to this target at this time", fired by the
// server's own loop (fused_render/schedule.py) so a scheduled turn runs in the
// app's environment rather than a cron job's. `state` is the whole story of one
// entry: `pending` until due, then `sent` (with `run_id`), or `missed` when the
// app was not running between the due time and the catch-up bound, or `error`
// with a reason. Terminal entries are kept — a message that did not send is
// exactly the one the user needs to be able to read afterwards.
export type ScheduledState =
  | "pending"
  | "sending"
  | "sent"
  | "missed"
  | "error"
  | "cancelled"
  // A recurring TEMPLATE — never sent itself. The server materializes its next
  // run as an ordinary `pending` entry carrying `template_id`, so a recurring
  // job appears here twice: once as the rule, once as the next concrete run.
  | "recurring";

// Structured recurrence — the server's recur.py schema, mirrored. Anchor is
// the entry's `due`: the first run, and the date every derived part (weekday,
// day-of-month, nth) is read from.
export interface RecurrenceRule {
  freq: "hour" | "day" | "week" | "month" | "year";
  interval?: number; // 1..99, default 1
  byday?: number[]; // week only; 0=Sunday
  monthly?: "day" | "nth-weekday"; // month only, default "day"
  until?: string; // "YYYY-MM-DD", local, inclusive
  count?: number; // total occurrences; exclusive with until
}

export interface ScheduledMessage {
  id: string;
  target: string;
  message: string;
  due: string;
  session_id: string;
  // WHERE `session_id` came from: true only when the server LEARNED it (a
  // repeating template's first run reported the session it opened, and that id
  // was written back). Absent or false means the user supplied it — a chat
  // handoff — which is the reading an entry stored before this field existed
  // gets, and the safe one: a repeat continues a learned thread but must never
  // continue the chat it was scheduled from.
  session_learned?: boolean;
  permission_mode: string;
  state: ScheduledState;
  created: string;
  fired: string;
  run_id: string;
  error: string;
  // `state` says whether the message was SENT; `turn` says how the session it
  // started then went. Two fields because they fail independently: a message can
  // send perfectly and its turn still die on the first tool call. "" until the
  // turn ends (and on entries stored before this field existed).
  // "unknown" = the watch ended without a verdict (the app stopped being able to
  // say). The work may well have finished; `run_id` is how to go and read it.
  turn?: "" | "ok" | "failed" | "cancelled" | "unknown";
  // The Claude Code session the turn actually ran in — filled in by the watcher,
  // and distinct from `session_id` (which is the input: resume this one, or ""
  // for a fresh one). This is the id the Inbox addresses a session by, so it is
  // what a row links to. Absent on entries stored before it existed.
  claude_session_id?: string;
  // The 5-field cron line on a `recurring` template; "" (or absent) elsewhere.
  repeats?: string;
  // The structured recurrence on a `recurring` template — the Google-Calendar
  // vocabulary cron cannot say (every 2 weeks, the second Wednesday, ends
  // after N). A template carries `repeats` OR `rule`, never both.
  rule?: RecurrenceRule;
  // On a rule template: occurrences materialized so far (drives `count` ends).
  made?: number;
  // On an occurrence: the template it was materialized from.
  template_id?: string;
  // On an occurrence: this is the ONE catch-up run of a rule whose anchor was
  // already in the past when it was created. Its `due` is the LATEST slot at or
  // before the moment it was made (the anchor sets the pattern; the run that
  // goes is this morning's, not last Saturday's), so it is overdue the instant
  // it exists and goes on the next tick — the same thing a past-dated one-off
  // does. The slots it collapsed past are never materialized and never run.
  catch_up?: boolean;
  // On a `recurring` template in GET /api/schedule only: projected occurrence
  // times (UTC ISO) over the next two weeks — server-side cron math, so the
  // calendar can draw future runs without a client cron parser. Not stored.
  upcoming?: string[];
  // The user's own one-liner for the task this message belongs to. Optional and
  // usually absent: left blank, the tasks endpoint falls back to Claude Code's
  // own `ai-title` record and then to the first line of the message, so a task
  // is named whether or not anyone named it. An explicit title beats both.
  title?: string;
  // Free text the user added when scheduling. Never auto-filled — Claude Code
  // writes a title into its transcripts but no summary, so there is nothing
  // honest to prefill this from.
  description?: string;
  // On a `recurring` template: mint a FRESH task for every run instead of
  // appending to one thread. The default (absent/false) is to append, which is
  // what a task being a session already means — the template's `session_id`
  // copies to each occurrence, so every run resumes the same conversation.
  // Ticking this copies "" instead, so each run starts its own.
  new_task_each_run?: boolean;
}

export interface ScheduleResult {
  entries: ScheduledMessage[];
  // The catch-up bound, in seconds (FUSED_RENDER_SCHEDULE_MAX_LATE server-side).
  // **null is the default now**: a missed one-off queues and runs however old,
  // so there is no bound to report. A number means an operator set the env var
  // and chose to reinstate one — which is the only case where a `missed` entry
  // needs explaining, and the only case where this is worth printing.
  max_late_seconds: number | null;
  permission_modes: string[];
}

export function getSchedule(): Promise<ScheduleResult> {
  return getJson<ScheduleResult>("/api/schedule");
}

// Exactly one of `due` (ISO 8601) or `delay_seconds` — the server refuses both,
// so a caller offering "in 30 minutes" never has to do timezone arithmetic.
// `repeats` (a 5-field cron line) replaces both: it already says every time it
// means, and the server refuses it alongside either.
export function scheduleMessage(body: {
  target: string;
  message: string;
  due?: string;
  delay_seconds?: number;
  repeats?: string;
  // Structured recurrence: requires `due` (the anchor/first run), exclusive
  // with `repeats` and `delay_seconds`.
  rule?: RecurrenceRule;
  session_id?: string;
  // Only ever sent alongside a `session_id` the entry being re-created had
  // LEARNED (an edit is cancel + re-create, so the marker has to be re-stated
  // or it dies with the old entry). Never sent for a chat handoff: the server
  // does not invent this, and a false claim here would let a repeating task
  // resume the conversation it was scheduled from.
  session_learned?: boolean;
  permission_mode?: string;
  // All three are omitted rather than sent empty: blank means "no opinion", and
  // for `title` that is a meaningful answer — the server names the task itself.
  title?: string;
  description?: string;
  // Only meaningful alongside `rule` or `repeats`; a one-off has no runs to
  // split apart.
  new_task_each_run?: boolean;
  // The id of the entry this one REPLACES — set only by an edit, which is
  // cancel + re-create and therefore mints a brand new entry id. A task that has
  // not run yet is NUMBERED on that entry id (`pending:<entry-id>`), so without
  // this the server allocated a second number and the task was renamed under the
  // user: TASK-078 became TASK-079 on a time change, with no duplicate left
  // behind to explain it. Sent so the number MOVES onto the new id instead.
  //
  // A no-op where there is nothing to move — a task whose session exists is
  // numbered on the session id, and that key is untouched by an edit.
  replaces?: string;
}): Promise<{ entry: ScheduledMessage }> {
  return postJson<{ entry: ScheduledMessage }>("/api/schedule", body);
}

// Un-skip a skipped recurring run: cancelled occurrence -> pending again.
// 404s unless it is a skipped run of a still-active schedule whose time has
// not passed — a skip is the one cancel that can honestly be walked back.
export function restoreScheduledMessage(id: string): Promise<{ entry: ScheduledMessage }> {
  return postJson<{ entry: ScheduledMessage }>("/api/schedule/restore", { id });
}

// Send a pending message NOW — what dragging a card from Upcoming to In
// Progress means on the Board.
//
// It does NOT move the entry's `due`. The schedule time is a fact about what
// was asked for, so the row reads as having run early (due then, fired now)
// rather than as having been scheduled for this minute — which is also what
// keeps its calendar chip on the day the user picked.
//
// Rejects rather than silently doing nothing: 404 when there is no such entry,
// 409 with a reason when there is one that cannot run — already sent, already
// sending, cancelled, or its conversation has a turn open right now (two
// `claude --resume` processes on one transcript is the one thing this must
// never do). The reason is written to be shown.
export function runScheduledNow(entryId: string): Promise<{ ok: boolean; entry: ScheduledMessage }> {
  return postJson<{ ok: boolean; entry: ScheduledMessage }>(
    "/api/schedule/run-now",
    { entry_id: entryId },
  );
}

// Ask again — the other half of Re-run, for the case run-now cannot serve.
//
// A run that already went and broke leaves NO pending entry to claim, so
// runScheduledNow has nothing to fire. This sends the same message as a NEW
// one: an ordinary one-off due now, resuming the session the original actually
// ran in, so the re-ask lands in the same thread. The original entry is left
// exactly as it was — its state, its due time and its error all stand, because
// that run really did happen and really did break.
//
// `entry` is the NEW message, not the original. `note` is a sentence to show
// beside a SUCCESS: the message may be queued rather than away (its
// conversation can be mid-turn), which is news but not a failure.
//
// Rejects with the server's own sentence: 404 for no such entry, 409 for one
// that cannot be re-sent — still pending or sending (use run-now, or wait),
// cancelled or missed (it never went, so there is nothing to send again).
export function resendScheduledMessage(
  entryId: string,
): Promise<{ ok: boolean; entry: ScheduledMessage; note?: string }> {
  return postJson<{ ok: boolean; entry: ScheduledMessage; note?: string }>(
    "/api/schedule/resend",
    { entry_id: entryId },
  );
}

// Rejects with the server's 404 message when the entry is no longer pending —
// a message that sent while the user was reaching for Cancel cannot be withdrawn.
export function cancelScheduledMessage(id: string): Promise<{ entry: ScheduledMessage }> {
  return postJson<{ entry: ScheduledMessage }>("/api/schedule/cancel", { id });
}

// The running narration of what scheduled messages DID — polled app-wide by
// useScheduleEvents and turned into toasts. A separate endpoint from the listing
// for the reason the mount-health log is separate: this poll runs forever in
// every shell and must not carry the page's payload.
//
// Append-only with monotonically increasing ids, so a poller both dedups and
// orders by tracking a high-water mark. Bounded server-side: it is a narration,
// not history — the schedule store holds every outcome durably.
export type ScheduleEventKind = "done" | "failed" | "missed";

export interface ScheduleEvent {
  id: number;
  kind: ScheduleEventKind;
  entry_id: string;
  target: string;
  // The prompt, not a summary: a toast saying "a scheduled message failed" sends
  // the user hunting, and the first words of what they asked for identify it.
  message: string;
  detail: string;
  ts: number;
}

// Undelivered events only — the SERVER remembers which those are, so a reload is
// quiet without the client guessing, and a `missed` verdict emitted by the
// scheduler's first tick (before any shell had loaded) still gets narrated.
export function getScheduleEvents(): Promise<{ events: ScheduleEvent[] }> {
  return getJson<{ events: ScheduleEvent[] }>("/api/schedule/events");
}

// Confirm every event up to `id` has been shown. Called AFTER narrating, so a
// client that dies in between gets a duplicate toast rather than a silent miss.
// A POST, not a drain-on-read: a GET with that side effect would let any page
// silently consume the user's notifications with a no-cors fetch.
export function ackScheduleEvents(id: number): Promise<{ delivered: number }> {
  return postJson<{ delivered: number }>("/api/schedule/events/ack", { id });
}
