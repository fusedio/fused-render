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
    // Same X-Fused D3 guard as putJson above.
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
  updated_at: string;
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
  page: string | null;
}

// What deploying a page would publish, resolved fresh from on-disk state —
// shown BEFORE the Deploy click. Non-empty `errors` means the page cannot be
// exported as-is (Deploy would fail with exactly these).
export interface DeployPreview {
  page: string;
  entrypoints: { path: string; name: string }[];
  assets: { path: string; name: string }[];
  errors: string[];
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

export function getDeployPreview(fsPath: string): Promise<DeployPreview> {
  return getJson<DeployPreview>("/api/deploy/preview?path=" + encodeURIComponent(fsPath));
}

export function deployPage(fsPath: string, env: string): Promise<Deployment> {
  return postJson<Deployment>("/api/deploy", { page: fsPath, env });
}

export function revokeDeployment(fsPath: string): Promise<Deployment> {
  return postJson<Deployment>("/api/deploy/revoke", { page: fsPath });
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
  log: { path: string; dir: string };
}

export function getPrefs(): Promise<Prefs> {
  return getJson<Prefs>("/api/prefs");
}

export function putEnginePref(engine: "builtin" | "fused"): Promise<Prefs> {
  return putJson<Prefs>("/api/prefs", { engine });
}

// Reveal a path in the OS file manager (same POST the breadcrumb button uses).
export function revealPath(fsPath: string): Promise<void> {
  return postJson<unknown>("/api/fs/reveal", { path: fsPath }).then(() => undefined);
}

// -- Template registry view (server.py /api/templates/registry; SPEC §20) -----

export interface RegistryEntry {
  pattern: string;
  templates: string[];
  disabled: boolean; // a null binding: previews disabled for this pattern
  source: "builtin" | "user" | "user-override";
  error: string | null;
}

export interface RegistryResult {
  entries: RegistryEntry[];
  builtin_registry: string;
  user_registry: string;
  error: string | null;
}

export function getTemplateRegistry(): Promise<RegistryResult> {
  return getJson<RegistryResult>("/api/templates/registry");
}
