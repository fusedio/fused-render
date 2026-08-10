// Typed client for the Claude-config bridge.
//
// The transport is one endpoint: POST /api/claude-config/{module} with a JSON
// body that the server forwards as kwargs to that module's `main()`, and the
// module's JSON return as the response body. So there is exactly one fetch
// helper here (`callModule`) and one thin wrapper per documented action, named
// after the action — a section component never spells an endpoint or an action
// string, and the kwargs a module accepts are pinned by a signature rather
// than by a comment.
//
// Two error channels, deliberately kept apart:
//
//   * TRANSPORT / server errors (404 unknown module, 400 bad kwargs, 500) are
//     thrown with the server's `error` message, exactly as @platform/lib/api's
//     helpers do. Callers let those reach the toast.
//   * A module's OWN in-band refusal — `{ok: false, error: "..."}` on a 200 —
//     is NOT thrown. It is part of each action's documented return (an
//     unremovable marketplace, a dirty tree blocking a profile switch, a git
//     ref git rejects), and several of those shapes carry extra fields the UI
//     acts on. Sections branch on `ok` themselves.
//
// The kwargs are the SAME ones the modules' docstrings document, including the
// two that are JSON-in-a-string (`preferences.patch`'s payload and
// `profiles.import`'s paths) — the modules parse those with json.loads, so the
// string is the contract, not an artefact of the old runPython transport.
//
// The transport itself is @platform/lib/api's postJson/getJson rather than a
// local fetch: these endpoints enforce the D3 write guard and answer 403 without
// an `X-Fused` header, which those helpers already send. A second hand-rolled
// fetch here would be one more place for that to be forgotten.
import { getJson, postJson } from "@platform/lib/api";

const BASE = "/api/claude-config";

// Availability of the whole feature. The Preferences page gates its tab on this
// (see ./index.ts).
export interface ClaudeConfigStatus {
  available: boolean;
}

function callModule<T>(module: string, kwargs: Record<string, unknown> = {}): Promise<T> {
  return postJson<T>(`${BASE}/${module}`, kwargs);
}

export function getStatus(): Promise<ClaudeConfigStatus> {
  return getJson<ClaudeConfigStatus>(`${BASE}/status`);
}

// -- shared shapes ------------------------------------------------------------

// Every action that writes returns this shape at minimum.
export interface OkResult {
  ok: boolean;
  error?: string;
}

// One entry of a change preview (git_ops drift/diff). `status` is a git
// name-status letter (A/M/D/R).
export interface FileDelta {
  status: string;
  path: string;
}

export interface SettingDelta {
  key: string;
  from: unknown;
  to: unknown;
}

// The `{files, settings}` pair every preview modal renders. `error` appears
// instead when git rejected the ref (git_ops.diff with an unknown target).
export interface ChangePreview {
  files?: FileDelta[];
  settings?: SettingDelta[];
  error?: string;
}

// -- preferences --------------------------------------------------------------

export type PrefControl = "toggle" | "select" | "number" | "text";

// One catalog entry: the curated overlay (label/group/control/options/
// unsetLabel) plus the doc-snapshot half refresh_catalog rewrites.
export interface PrefEntry {
  key: string;
  label: string;
  group: string;
  control: PrefControl;
  options?: string[];
  doc?: string | null;
  // The DOCUMENTED default, shown when the key is unset. Any JSON scalar, and
  // null both for "no documented default" and for a documented `null`.
  default?: unknown;
  minVersion?: string | null;
  unsetLabel?: string;
}

export interface PreferencesGet {
  schema: PrefEntry[];
  // Current on-disk value per catalog key; null/absent = unset (using Claude's
  // own default). Typed `unknown` rather than a scalar union on purpose: the
  // catalog is curated but settings.json is not, so a key can hold anything
  // and the UI coerces at the point of display.
  prefs: Record<string, unknown>;
}

export interface PatchResult extends OkResult {
  changed?: string[];
}

export const preferences = {
  get: () => callModule<PreferencesGet>("preferences", { action: "get" }),
  // `payload` is a JSON OBJECT STRING {key: value|null} — null resets the key
  // to Claude's default by deleting the leaf.
  patch: (patch: Record<string, unknown>) =>
    callModule<PatchResult>("preferences", { action: "patch", payload: JSON.stringify(patch) }),
};

export interface RefreshCatalogResult extends OkResult {
  updated?: number;
  total?: number;
  undocumented?: string[];
  // The user-override catalog the refresh wrote — the bundled catalog is
  // read-only, so a refresh lands in the user's own copy and this says where.
  path?: string;
}

// The catalog refresh is its own module with no actions — main() takes nothing.
export const refreshCatalog = () => callModule<RefreshCatalogResult>("refresh_catalog");

// -- plugins ------------------------------------------------------------------

export interface Plugin {
  id: string;
  name: string;
  marketplace: string;
  enabled: boolean;
  installed: boolean;
  version?: string | null;
  gitSourced: boolean;
  shareCommand: string;
}

export interface UpdateResult extends OkResult {
  id?: string;
  stdout?: string;
}

export const plugins = {
  list: () => callModule<{ plugins: Plugin[] }>("plugins", { action: "list" }),
  toggle: (id: string, enabled: boolean) =>
    callModule<OkResult & { id?: string; enabled?: boolean }>("plugins", {
      action: "toggle",
      id,
      enabled,
    }),
  update: (id: string) => callModule<UpdateResult>("plugins", { action: "update", id }),
};

// -- marketplaces -------------------------------------------------------------

export interface MarketplaceSource {
  source?: string;
  repo?: string;
  url?: string;
}

export interface Marketplace {
  name: string;
  source: MarketplaceSource;
  editable: boolean;
  autoUpdate: boolean;
  shareCommand: string | null;
}

export type MarketplaceKind = "github" | "git";

export const marketplaces = {
  list: () => callModule<{ marketplaces: Marketplace[] }>("marketplaces", { action: "list" }),
  add: (name: string, kind: MarketplaceKind, value: string) =>
    callModule<OkResult & { name?: string }>("marketplaces", { action: "add", name, kind, value }),
  remove: (name: string) =>
    callModule<OkResult & { name?: string }>("marketplaces", { action: "remove", name }),
};

// -- memory -------------------------------------------------------------------

export interface MemoryProject {
  project: string;
  files: string[];
  changes: FileDelta[];
}

export const memory = {
  list: () => callModule<{ projects: MemoryProject[] }>("memory", { action: "list" }),
  open: (project: string) => callModule<OkResult>("memory", { action: "open", project }),
  commit: (project: string) =>
    callModule<OkResult & { committed?: string | null }>("memory", { action: "commit", project }),
  clear: (project: string) =>
    callModule<OkResult & { committed?: string | null }>("memory", { action: "clear", project }),
};

// -- claude_md ----------------------------------------------------------------

export interface ClaudeMdFile {
  path: string;
  dir: string;
  name: string;
  size: number;
  // Epoch SECONDS (os.stat st_mtime), not milliseconds.
  mtime: number;
  empty: boolean;
  scope: "global" | "project" | "disk";
  // First few lines of the file (char-capped server-side) for the card preview.
  snippet: string;
}

export const claudeMd = {
  list: () => callModule<{ files: ClaudeMdFile[]; engine: string }>("claude_md", { action: "list" }),
  open: (path: string) => callModule<OkResult>("claude_md", { action: "open", path }),
  remove: (path: string) =>
    callModule<OkResult & { committed?: string | null }>("claude_md", { action: "delete", path }),
};

// -- skills -------------------------------------------------------------------

export interface Skill {
  slug: string;
  name: string;
  description: string;
  linked: boolean;
  source: string | null;
  shareCommand: string | null;
}

export const skills = {
  list: () => callModule<{ skills: Skill[] }>("skills", { action: "list" }),
  open: (slug: string) => callModule<OkResult>("skills", { action: "open", slug }),
};

// -- statusline ---------------------------------------------------------------

export interface StatuslineScript {
  path: string;
  tracked: boolean;
  size: number;
  // ISO-8601 with offset (statusline.py's _iso), not an epoch.
  modified: string;
  description: string;
  fields: string[];
  otherFields: string[];
}

export interface StatuslineGet {
  configured: boolean;
  type: string | null;
  command: string | null;
  script: StatuslineScript | null;
}

export interface StatuslinePreview {
  ok: boolean;
  // Raw stdout, ANSI escapes intact (see ./ansi.tsx).
  output: string;
  error?: string;
}

export const statusline = {
  get: () => callModule<StatuslineGet>("statusline", { action: "get" }),
  preview: () => callModule<StatuslinePreview>("statusline", { action: "preview" }),
};

// -- profiles -----------------------------------------------------------------

export interface Profile {
  name: string;
  current: boolean;
  isDefault: boolean;
}

// A refusal because the working tree is dirty. `files` is a list of PATHS
// (lib.status), not the {status, path} pairs a change preview carries — the
// two git shapes differ and the UI maps one onto the other.
export interface DirtyRefusal extends OkResult {
  dirty?: boolean;
  files?: string[];
}

export interface ExportResult extends OkResult {
  // Filename STEM only — the page stamps the date and appends .zip, because
  // main() has no wall clock.
  filename?: string;
  b64?: string;
}

export interface ZipEntry {
  path: string;
  isDir: boolean;
  size: number;
}

export interface ImportResult extends DirtyRefusal {
  branch?: string;
  imported?: string[];
}

export const profiles = {
  list: () => callModule<{ profiles: Profile[]; current: string }>("profiles", { action: "list" }),
  create: (name: string) => callModule<OkResult & { name?: string }>("profiles", {
    action: "create",
    name,
  }),
  // `message` is what turns a dirty-tree refusal into "commit, then switch":
  // omitted, a dirty tree refuses with {dirty, files}; supplied, that message
  // becomes the commit git makes first.
  switch: (name: string, message?: string) =>
    callModule<DirtyRefusal & { current?: string }>("profiles", {
      action: "switch",
      name,
      ...(message ? { message } : {}),
    }),
  remove: (name: string) =>
    callModule<OkResult & { name?: string }>("profiles", { action: "delete", name }),
  export: (name: string) => callModule<ExportResult>("profiles", { action: "export", name }),
  inspect: (b64: string) =>
    callModule<OkResult & { entries?: ZipEntry[] }>("profiles", { action: "inspect", b64 }),
  // `paths` is a JSON ARRAY STRING (the module json.loads it).
  import: (b64: string, branch: string, paths: string[], message?: string) =>
    callModule<ImportResult>("profiles", {
      action: "import",
      b64,
      branch,
      paths: JSON.stringify(paths),
      ...(message ? { message } : {}),
    }),
};

// -- mcp ----------------------------------------------------------------------

export type McpStatus = "connected" | "needs-auth" | "failed" | "pending" | "unknown";
export type McpKind = "user" | "connector" | "plugin";

export interface McpServer {
  name: string;
  endpoint: string;
  transport: string;
  status: McpStatus;
  kind: McpKind;
  connected: boolean;
  needsAuth: boolean;
  canAuth: boolean;
  removable: boolean;
}

// The `claude mcp` CLI's own output is the error detail for add/logout/remove,
// so stderr rides along and is what the UI reports in preference to `error`.
export interface CliResult extends OkResult {
  name?: string;
  stdout?: string;
  stderr?: string;
}

export const mcp = {
  list: () => callModule<OkResult & { servers?: McpServer[] }>("mcp", { action: "list" }),
  login: (name: string) =>
    callModule<OkResult & { launched?: boolean }>("mcp", { action: "login", name }),
  logout: (name: string) => callModule<CliResult>("mcp", { action: "logout", name }),
  remove: (name: string) => callModule<CliResult>("mcp", { action: "remove", name }),
  // `json` is the server definition as a JSON string — the CLI's own
  // `add-json` argument, validated by the module before it delegates.
  add: (name: string, json: string) => callModule<CliResult>("mcp", { action: "add", name, json }),
};

// -- git_ops ------------------------------------------------------------------

export interface LogEntry {
  sha: string;
  date: string;
  message: string;
}

export interface GitStatus {
  dirty: boolean;
  // Paths, not {status, path} pairs (see DirtyRefusal).
  files: string[];
}

export const gitOps = {
  log: () => callModule<{ log: LogEntry[] }>("git_ops", { action: "log" }),
  status: () => callModule<GitStatus>("git_ops", { action: "status" }),
  drift: () => callModule<ChangePreview>("git_ops", { action: "drift" }),
  diff: (target: string) => callModule<ChangePreview>("git_ops", { action: "diff", target }),
  commit: () => callModule<OkResult & { committed?: string | null }>("git_ops", {
    action: "commit",
  }),
  restore: (target: string) =>
    callModule<OkResult & { sha?: string }>("git_ops", { action: "restore", target }),
};
