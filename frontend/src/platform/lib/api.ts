// Server API wrappers. Non-ok responses throw with the server's error message.
export interface Config {
  start_dir: string;
  home: string;
  // The Fused workspace dir (~/Documents/Fused) — the sidebar's "Fused" entry.
  fused_dir: string;
  version: string;
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
  // Session restore keys off this: a non-writable file can never have had a
  // sidecar written, so its restore is skipped rather than blocking on a cold,
  // guaranteed-null GET /api/session (see useSessionRestore).
  writable?: boolean;
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

async function getJson<T>(url: string, headers?: Record<string, string>): Promise<T> {
  const res = await fetch(url, headers ? { headers } : undefined);
  const data = await res.json();
  if (!res.ok) throw httpError(data, res.status);
  return data as T;
}

// One mutating-request helper for both PUT and POST — they differ only in the
// method. X-Fused forces a CORS preflight so a foreign page can't write blind
// (the D3 guard the reveal/write/deploy endpoints require).
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
const postJson = <T>(url: string, body: unknown) => mutateJson<T>("POST", url, body);

export function getConfig(): Promise<Config> {
  return getJson<Config>("/api/config");
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
const LIST_PREFETCH_TTL_MS = 5000;
const listPrefetch = new Map<string, { promise: Promise<ListResult>; ts: number }>();

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

export function statPath(fsPath: string): Promise<StatResult> {
  return getJson<StatResult>("/api/fs/stat?path=" + encodeURIComponent(fsPath));
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

// -- Deploy (hosted publish through the fused CLI; fused_render/deploy.py) ----

// Availability of the fused CLI in the server's environment, and whether the
// server can pip-install it (the pinned [fused] extra) on request.
export interface DeployCli {
  found: boolean;
  command: string | null;
  installable: boolean;
  reason: string | null;
  install_hint: string;
}

// A hosted environment from the fused CLI's own store (~/.openfused/envs.json):
// backend "fused" (managed) or "aws" (self-provisioned serving plane).
export interface DeployEnv {
  name: string;
  backend: string;
}

export interface DeployConfig {
  cli: DeployCli;
  envs: DeployEnv[];
  default_env: string | null;
  envs_file: string;
  // What to type in a terminal for one-time CLI setup (`… env create`,
  // `… cloud setup`): plain "fused" normally; inside the packaged macOS app,
  // the absolute path of the bundle's own CLI wrapper.
  setup_cli: string;
  // Whether the fused CLI's control-plane credentials exist on disk (a
  // `fused cloud login` has happened). Presence-only — the CLI stays the
  // authority at action time; this powers the before-the-click warning when
  // a managed env is targeted with no login at all.
  fused_logged_in: boolean;
}

// The thin per-page deployment pointer (~/.fused-render/deployments.json).
// url is null when the backend never returned one (AWS prints token+path only).
export interface Deployment {
  page: string;
  env: string;
  backend: string;
  token: string;
  url: string | null;
  status: "active" | "revoked";
  entrypoints: string[];
  // The file selection this deployment was published with (persisted on the
  // record, not a sidecar): extra files bundled beyond the auto-scan, and files
  // dropped from it. Reopening the modal reloads these so the selection sticks.
  // Optional — records written before this feature omit them (read as []).
  include?: string[];
  exclude?: string[];
  // Whether viewers may download this page's source bundle. Persisted like the
  // caching choice, but the MOUNT is the authority — reopening the modal
  // reconciles this against `share list` (ShareMount.allow_clone) so a posture
  // changed elsewhere isn't masked by a stale local value. Optional: records
  // written before the feature omit it and read as false (fail closed — a page's
  // source must never look downloadable because a field was missing).
  allow_clone?: boolean;
  // The caching choice this deployment was published with — "0s" (off) or a
  // duration like "5m"/"1h" (fused/agent_core/caching.py's cache_max_age format).
  // Reopening the modal reloads it, same as include/exclude. Optional — records
  // written before this feature omit it (read as "0s").
  cache_max_age?: string;
  // Whether this mount's token is a user-chosen name (a deliberately guessable
  // public URL) vs the default crypto-random opaque one — so the modal shows
  // "custom name" vs "unguessable" without re-deriving it from the token string.
  // Optional — records written before this feature omit it (read as false).
  named?: boolean;
  updated_at: string;
}

// `POST /api/deploy/clear-cache`'s result — the fused CLI's `share cache-clear`
// output verbatim (see deploy.py's clear_cache_deployment).
export interface CacheClearResult {
  token: string;
  deleted: number;
  scope: string;
  prefix?: string;
}

export interface DeployStatusResult {
  deployment: Deployment | null;
  // false when the pointer was NOT checked against `share list` (reconcile not
  // requested, or the deploy env was unreachable) — last-known state only.
  reconciled: boolean;
  // The mount's raw `share list` classification when reconciled, else null.
  // "absent" (gone from the list entirely, e.g. after an infra teardown) is
  // persisted as status "revoked" but redeploys as a FRESH create with a new
  // URL — the modal's action label branches on this.
  live: "active" | "revoked" | "absent" | null;
}

// One mount from `fused share list` on an env, joined back to the local page
// that deployed it (null for mounts this app doesn't track). `share list`
// itself carries no URLs; url is the pointer's recorded link, else derived
// from the env's base URL when a recorded link reveals it, else null.
export interface ShareMount {
  token: string;
  status: string;
  type: string | null;
  url: string | null;
  // The mount's LIVE clone posture — what the Deploy modal reconciles against.
  allow_clone: boolean;
  page: string | null;
}

// How a bundled asset is exposed / why it's in the bundle (export.Asset.source):
//   reference — a literal fused.rawUrl()/readFile() the HTML scan resolved (the
//               page fetches it via rawUrl/readFile)
//   manifest  — declared in the page's <script type="application/fused-bundle">
//               include, the reproducible way to back a *computed* rawUrl/readFile path
//   include   — added by hand via the modal's include ("Add all in folder")
// Every asset is served read-only on the hosted `_asset` route regardless; the
// distinction drives the row's label so the list mentions rawUrl/readFile exposure.
export type AssetSource = "reference" | "manifest" | "include";

// What deploying a page would publish, resolved fresh from on-disk state —
// shown BEFORE the Deploy click. Non-empty `errors` means the page cannot be
// exported as-is (Deploy would fail with exactly these).
export interface DeployPreview {
  page: string;
  entrypoints: { path: string; name: string }[];
  assets: { path: string; name: string; source: AssetSource }[];
  // The auto-detected default set (literal runPython/rawUrl/readFile paths, before
  // include/exclude). Lets the modal distinguish an auto file (removing → exclude,
  // shown under "Excluded" with restore) from a manual include (removing → just drop).
  auto: string[];
  errors: string[];
  // Advisory, non-blocking: a computed rawUrl/readFile path (bundle its target
  // via include), or an exclude that drops a file the page references. Distinct
  // from `errors`, which disable Deploy.
  warnings: string[];
}

export interface SharesResult {
  env: string;
  mounts: ShareMount[];
}

export function getDeployConfig(): Promise<DeployConfig> {
  return getJson<DeployConfig>("/api/deploy/config");
}

export function getDeployStatus(fsPath: string, reconcile: boolean): Promise<DeployStatusResult> {
  const url =
    "/api/deploy/status?path=" + encodeURIComponent(fsPath) + (reconcile ? "&reconcile=1" : "");
  return getJson<DeployStatusResult>(url);
}

export function getDeployPreview(
  fsPath: string,
  include: string[],
  exclude: string[],
): Promise<DeployPreview> {
  // POST (not GET) so the include/exclude selection travels in the body — arrays
  // don't fit a query string cleanly. Read-only server-side (no files written).
  return postJson<DeployPreview>("/api/deploy/preview", { path: fsPath, include, exclude });
}

export function deployPage(
  fsPath: string,
  env: string,
  include: string[],
  exclude: string[],
  cacheMaxAge: string,
  // May viewers download this page's source bundle? Always sent explicitly (the
  // server states it to the CLI either way), because the toggle in the dialog is
  // a definite statement — omitting it on a redeploy would silently preserve
  // whatever the mount had, so unticking the box would not turn it off.
  allowClone: boolean,
  forceNew?: boolean,
  // A chosen link name for a FRESH `share create` (see deploy.py's
  // deploy_page) — omit for the default auto-generated opaque token. Ignored
  // server-side on a redeploy that reuses an existing token (repoint/recreate).
  token?: string,
): Promise<Deployment> {
  return postJson<Deployment>("/api/deploy", {
    page: fsPath,
    env,
    include,
    exclude,
    cache_max_age: cacheMaxAge,
    allow_clone: allowClone,
    force_new: forceNew ?? false,
    ...(token ? { token } : {}),
  });
}

export function revokeDeployment(fsPath: string): Promise<Deployment> {
  return postJson<Deployment>("/api/deploy/revoke", { page: fsPath });
}

// Clears every cached result for the page's deployed mount (`fused share
// cache-clear <token>`) — forces the next request to recompute instead of
// waiting out cache_max_age. Doesn't change the deployment's status/URL/caching
// setting.
export function clearCacheDeployment(fsPath: string): Promise<CacheClearResult> {
  return postJson<CacheClearResult>("/api/deploy/clear-cache", { page: fsPath });
}

// -- cloning a DEPLOYED page (app_clone.py) ---------------------------------
// Distinct from the GitHub deep-link clone (deeplink.py): no git, no identity, no
// update-in-place — every clone lands in a fresh folder under ~/Documents/Fused.

export interface ClonePreviewFile {
  path: string;
  bytes: number | null;
}

export interface ClonePreview {
  // The canonical `…/<token>/_clone` URL derived from what the user pasted.
  url: string;
  name: string;
  files: ClonePreviewFile[];
  // Uncompressed total across the archive's members.
  bytes: number | null;
  // What the download actually costs (base64 of the compressed archive). Null on
  // an older serve path that doesn't report it — show nothing rather than a guess.
  download_bytes: number | null;
  dest: string;
  folder: string;
  // True when `folder` had to be suffixed because something already occupies the
  // page's own name — surfaced so the confirm step can say so up front.
  renamed: boolean;
}

export interface CloneResult {
  dest: string;
  folder: string;
  page: string;
  // The /view path to open the cloned page at.
  view: string;
  files: number;
}

export function cloneAppInfo(src: string): Promise<ClonePreview> {
  return getJson<ClonePreview>(`/api/clone-app/info?src=${encodeURIComponent(src)}`);
}

// `folder` is the destination the preview showed, passed back so the clone lands where the
// user was told it would. Omitted, the backend derives it — the response is authoritative
// either way.
export function cloneApp(src: string, folder?: string): Promise<CloneResult> {
  return postJson<CloneResult>("/api/clone-app", { src, folder });
}

export function installFused(): Promise<void> {
  return postJson<unknown>("/api/deploy/install", {}).then(() => undefined);
}

export function listShares(env: string): Promise<SharesResult> {
  return getJson<SharesResult>("/api/deploy/shares?env=" + encodeURIComponent(env));
}

// Revoke a mount by env+token (the Preferences page's share list — covers
// mounts with no local pointer too; the CLI's owner-binding still applies).
export function revokeMount(env: string, token: string): Promise<void> {
  return postJson<unknown>("/api/deploy/revoke", { env, token }).then(() => undefined);
}

// -- Deployed error viewing (`fused share errors`; the fused repo's -----------
// error-reporting.md). Owner-only diagnostics behind a deployed mount's opaque
// 500s — the page's own viewers never see any of this.

// One row of the newest-first list: identity plus the first line of the error.
// `error` is a single line here; the full traceback lives on the record fetched
// by `getDeployErrorDetail`.
export interface DeployErrorSummary {
  err_id: string;
  occurred_at: string;
  token: string;
  entrypoint: string | null;
  kind: string; // "user-code" | "bad-result" | "invoke-failure"
  error: string;
  truncated: boolean;
}

// The full captured record — the traceback (`error`), output tails, and the
// params that triggered it. Free-text fields are size-capped at capture and
// `truncated` marks a record that was cut. Fields beyond the summary are
// optional: an `invoke-failure` carries no streams, `bad-result` no traceback.
export interface DeployErrorRecord {
  version: number;
  err_id: string;
  occurred_at: string;
  env: string;
  token: string;
  app?: string | null;
  entrypoint?: string | null;
  entrypoint_kind?: string | null;
  kind: string;
  http_method?: string;
  duration_ms?: number | null;
  error?: string;
  stdout_tail?: string;
  stderr_tail?: string;
  params?: unknown;
  params_preview?: string;
  params_truncated?: boolean;
  truncated: boolean;
}

export interface DeployErrorsResult {
  env: string;
  token: string;
  errors: DeployErrorSummary[];
}

export interface DeployErrorDetailResult {
  env: string;
  token: string;
  record: DeployErrorRecord;
}

export interface DeployErrorFilters {
  limit?: number;
  since?: string;
  until?: string;
  kind?: string;
  entrypoint?: string;
}

export function listDeployErrors(
  env: string,
  token: string,
  filters: DeployErrorFilters = {},
): Promise<DeployErrorsResult> {
  const q = new URLSearchParams({ env, token });
  if (filters.limit != null) q.set("limit", String(filters.limit));
  if (filters.since) q.set("since", filters.since);
  if (filters.until) q.set("until", filters.until);
  if (filters.kind) q.set("kind", filters.kind);
  if (filters.entrypoint) q.set("entrypoint", filters.entrypoint);
  return getJson<DeployErrorsResult>("/api/deploy/errors?" + q.toString());
}

export function getDeployErrorDetail(
  env: string,
  token: string,
  errId: string,
): Promise<DeployErrorDetailResult> {
  const q = new URLSearchParams({ env, token, err_id: errId });
  return getJson<DeployErrorDetailResult>("/api/deploy/error?" + q.toString());
}

// -- Fused account (account.py; SPEC §27) -------------------------------------

// One org/env the signed-in account can target (`fused cloud orgs`).
export interface AccountOrg {
  org: string | null;
  env: string | null;
  provision_state: string | null;
  role: string | null;
}

// The deeper signed-in check (`fused cloud orgs`), run only with ?probe=1:
// unlike the presence-only logged_in flag it exercises the token, so a stale
// credential shows up here as ok=false with the CLI's own message.
export interface AccountProbe {
  ok: boolean;
  admitted: boolean | null;
  orgs: AccountOrg[];
  error: string | null;
}

// One env from the raw store view (any backend; `hosted` = can be a deploy
// target). Distinct from DeployEnv, which is the hosted-only picker list.
export interface StoreEnv {
  name: string;
  backend: string;
  hosted: boolean;
}

export interface AccountStatus {
  cli: DeployCli;
  // Presence of the CLI's credentials file — cheap and optimistic (the CLI
  // refreshes an expired token itself); `probe` is the authoritative check.
  logged_in: boolean;
  // A `fused cloud login` child is currently waiting on its browser round-trip.
  login_in_flight: boolean;
  // Fingerprint of the credentials file (mtime, or null when absent). The
  // account page drops its cached orgs probe when this changes — a re-login
  // as a different account that never flips logged_in false in this tab.
  creds_stamp: number | null;
  envs_file: string;
  // The raw env store for the management table: every backend, plus the
  // store's own default pointer. (The deploy picker's derived view lives on
  // DeployConfig, not here.)
  store: { envs: StoreEnv[]; default: string | null };
  probe: AccountProbe | null;
}

// The one tracked `fused cloud setup` job (account.py). `detail` carries the
// CLI's own progress lines while running, its final line when done, and the
// mapped error message when failed.
export interface AccountSetupStatus {
  state: "idle" | "running" | "done" | "failed";
  job_id: string | null;
  env_name: string | null;
  detail: string | null;
}

export function getAccountStatus(probe = false): Promise<AccountStatus> {
  // probe=1 EXECUTES server-side (spawns a `fused cloud orgs` control-plane
  // call), so unlike the plain status read it carries the D36 guard header.
  return probe
    ? getJson<AccountStatus>("/api/account/status?probe=1", { "X-Fused": "1" })
    : getJson<AccountStatus>("/api/account/status");
}

// Start (or join — one login at a time) the CLI's browser sign-in and return
// the authorize URL; OPENING it is the caller's job (window.open — the server
// never drives a browser). returnUrl must be a loopback URL (normally
// location.href): the post-login callback 302s the browser back to it.
export function startAccountLogin(returnUrl: string): Promise<{ authorize_url: string }> {
  return postJson<{ authorize_url: string }>("/api/account/login", { return_url: returnUrl });
}

export function cancelAccountLogin(): Promise<void> {
  return postJson<unknown>("/api/account/login/cancel", {}).then(() => undefined);
}

// Sign out (killing any in-flight sign-in first, server-side) and return the
// fresh status.
export function accountLogout(): Promise<AccountStatus> {
  return postJson<AccountStatus>("/api/account/logout", {});
}

// Start the one-shot managed-env setup (`fused cloud setup`) as a tracked
// background job — 202 with the job to poll via getAccountSetup. org/env go
// together (a specific workspace); omitting both lets the CLI discover the
// account's org (or self-create a personal one). env_name defaults
// server-side to flow's convention (`fused` / `fused-<env>`).
export function startAccountSetup(opts: {
  org?: string;
  env?: string;
  env_name?: string;
}): Promise<{ job_id: string; env_name: string }> {
  return postJson<{ job_id: string; env_name: string }>("/api/account/setup", opts);
}

export function getAccountSetup(): Promise<AccountSetupStatus> {
  return getJson<AccountSetupStatus>("/api/account/setup");
}

// `fused env default NAME` — the store's global default pointer.
export function setDefaultEnv(name: string): Promise<AccountStatus> {
  return postJson<AccountStatus>("/api/account/envs/default", { name });
}

// `fused env delete NAME --yes` — forgets the LOCAL pointer only; cloud
// resources and stored keys are untouched (the CLI's semantics).
export function deleteStoreEnv(name: string): Promise<AccountStatus> {
  return postJson<AccountStatus>("/api/account/envs/delete", { name });
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
  // Whether the preview-header Deploy button is shown (opt-in, default off).
  deploy: { enabled: boolean };
  // Whether the Reader (listen-to-files) accessibility mode is offered (opt-in,
  // default off).
  reader: { enabled: boolean };
  // The app call log (fused_render/calls.py): capture state, how much of a
  // run's params is kept, retention window, and where the store lives.
  calls: CallsPrefs;
}

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

export function putDeployEnabled(enabled: boolean): Promise<Prefs> {
  return putJson<Prefs>("/api/prefs", { deploy_enabled: enabled });
}

export function putReaderEnabled(enabled: boolean): Promise<Prefs> {
  return putJson<Prefs>("/api/prefs", { reader_enabled: enabled });
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

// Create (or overwrite) a plain file. Used for "New File…" with empty content;
// the parent directory must already exist (the server does not mkdir -p).
// With create=true the write refuses (409 "conflict") when the path already
// exists, so "New File" can't silently clobber an existing file.
export function writeFile(path: string, content = "", create = false): Promise<StatResult> {
  return postJson<StatResult>("/api/fs/write", { path, content, create });
}

// Create a single directory (no mkdir -p — a missing parent is a 400).
export function mkdir(path: string): Promise<StatResult> {
  return postJson<StatResult>("/api/fs/mkdir", { path });
}

// Remove a file or directory. A non-empty directory needs recursive=true (the
// context menu passes it only after the confirm dialog spells that out).
// With trash=true the entry is moved to the user's Trash instead (macOS only);
// where that's unsupported the server replies 501 "trash unsupported" and the
// caller falls back to a hard delete.
export function deleteEntry(
  path: string,
  recursive = false,
  trash = false
): Promise<{ deleted: string; trashed?: boolean }> {
  return postJson<{ deleted: string; trashed?: boolean }>("/api/fs/delete", {
    path,
    recursive,
    trash,
  });
}

// Move/rename src -> dst (also the paste-of-a-cut move). An existing dst is a
// 409 unless overwrite=true.
export function renameEntry(src: string, dst: string, overwrite = false): Promise<StatResult> {
  return postJson<StatResult>("/api/fs/rename", { src, dst, overwrite });
}

// Copy src -> dst (paste-of-a-copy, and Duplicate). Same 409-on-existing-dst
// rule as rename; a directory copied into itself/a descendant is a 400.
export function copyEntry(src: string, dst: string, overwrite = false): Promise<StatResult> {
  return postJson<StatResult>("/api/fs/copy", { src, dst, overwrite });
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
  return postJson<StatResult>("/api/fs/compress", { path, format, dest });
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
// to start it, poll, and report; the same shape as lib/account.ts otherwise.
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
// An app folder two levels under the workspace: <fused_dir>/<tag>/<name>/.
// `tag` is just the top-level folder's name — any folder qualifies, there is
// no fixed tag set. `entry_html` is the app's "/" route entry file (absolute
// path), null when the folder has no single resolvable .html entry; `title`
// comes from that file's <title>, null falls back to the folder name in the
// UI.
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
  title: string | null;
  // Last-modified time, epoch seconds. Optional/null for servers that don't
  // report it (older backends) — those sort last in the Home grid.
  updated_at?: number | null;
}

export function getApps(): Promise<{ apps: AppInfo[] }> {
  return getJson<{ apps: AppInfo[] }>("/api/apps");
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

// -- Linked apps (registry-backed apps living anywhere on disk) ---------------
// A folder outside the workspace registered as an app under the virtual
// "linked" tag (~/.fused-render/linked_apps.json — see fused_render/
// linked_apps.py for why a registry, not a symlink).

// How a folder relates to the app system: "workspace" (under the Fused
// workspace — is/can be a real app already), "linked" (registered, `name`
// carries its registry name), "unlinked" (linkable).
export interface AppLinkStatus {
  status: "workspace" | "linked" | "unlinked";
  name: string | null;
}

// Resolve a linked app's registry name to its real folder (null = unknown
// name) — backs the shell's /apps/linked/<name> route, which can't use the
// fused_dir codec the other tags do.
export function getLinkedAppPath(name: string): Promise<{ path: string | null }> {
  return getJson<{ path: string | null }>(
    "/api/apps/linked-path?name=" + encodeURIComponent(name)
  );
}

export function getAppLinkStatus(path: string): Promise<AppLinkStatus> {
  return getJson<AppLinkStatus>(
    "/api/apps/link-status?path=" + encodeURIComponent(path)
  );
}

// Register a folder as a linked app. 409 = name/folder already linked,
// 400 = not a folder / inside the workspace / bad name.
export function linkApp(path: string, name?: string): Promise<{ app: AppInfo }> {
  return postJson<{ app: AppInfo }>("/api/apps/link", { path, name });
}

export function unlinkApp(name: string): Promise<{ removed: boolean }> {
  return postJson<{ removed: boolean }>("/api/apps/unlink", { name });
}

// -- AI completion (POST /api/ai) ---------------------------------------------
// The fused.ai relay: one non-streaming completion through the server's warm
// Claude Code CLI instance (server/ai.py). Model defaults to haiku server-side;
// the shell uses this for small utility completions (e.g. naming a new app
// from its prompt on Home), not for anything conversational.
export function aiComplete(prompt: string, system_prompt?: string): Promise<string> {
  return postJson<{ ok: boolean; result: { text: string } }>("/api/ai", {
    prompt,
    ...(system_prompt ? { system_prompt } : {}),
  }).then((r) => r.result.text);
}
