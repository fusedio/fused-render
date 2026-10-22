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
//     unremovable marketplace, a skill name that already exists), and several
//     of those shapes carry extra fields the UI acts on. Sections branch on
//     `ok` themselves.
//
// The kwargs are the SAME ones the modules' docstrings document, including
// the one that is JSON-in-a-string (`preferences.patch`'s payload) — the module
// parses it with json.loads, so the string is the contract.
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

// -- preferences --------------------------------------------------------------

export type PrefControl = "toggle" | "select" | "number" | "text";

// One catalog entry: the curated overlay (label/group/control/options/
// unsetLabel) plus the doc half (doc/default/minVersion).
export interface PrefEntry {
  key: string;
  label: string;
  group: string;
  control: PrefControl;
  options?: string[];
  // How an option is SAID, where its own spelling will not do. Most options are
  // already the word ("fable", "high", "acceptEdits") — which is why the map is
  // currently empty and the field optional. It exists for the ids that are not:
  // a full `--model` id is what the flag takes, not what a person reads.
  // Sparse by design: only the options that need words appear here, and
  // an unmapped one shows itself, so the map never has to be kept exhaustive
  // against a list the docs refresh may grow.
  optionLabels?: Record<string, string>;
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

// -- plugins ------------------------------------------------------------------

export interface Plugin {
  id: string;
  name: string;
  marketplace: string;
  enabled: boolean;
  installed: boolean;
  version?: string | null;
  gitSourced: boolean;
  /** The cloned marketplace catalog's one-liner; "" when it has none. */
  description: string;
  shareCommand: string;
}

// A plugin as its MARKETPLACE describes it (the catalog), not as this machine
// has it — a different source from `Plugin` above, hence a different type. The
// overlap is `installed`/`enabled`, joined on server-side so Discover can hide
// or mark what is already here.
export interface AvailablePlugin {
  id: string;
  name: string;
  marketplace: string;
  description: string;
  version?: string | null;
  author?: string | null;
  category?: string | null;
  keywords: string[];
  installed: boolean;
  enabled: boolean;
}

// `skipped` names the marketplaces whose catalog could not be read. One bad
// marketplace.json is not fatal, but it IS reportable: without this the page
// would silently show a short list.
export interface AvailablePlugins {
  plugins: AvailablePlugin[];
  skipped: string[];
}

// One thing a plugin contributes to a session — a skill, a command, an agent,
// a hook event, an MCP server. `path` is the absolute file that declares it,
// which is what makes the entry clickable: the panel opens it in FusedRender
// rather than paraphrasing it.
export interface PluginComponent {
  name: string;
  description: string;
  path: string;
}

// What ONE installed plugin puts in a session. Read on demand, per row — see
// plugins.py's `_contents` for why this is not folded into `list`.
export interface PluginContents extends OkResult {
  id?: string;
  root?: string;
  description?: string;
  skills?: PluginComponent[];
  commands?: PluginComponent[];
  agents?: PluginComponent[];
  hooks?: PluginComponent[];
  mcpServers?: PluginComponent[];
  // True once the server's per-read walk budget ran out (plugins.py's
  // _WalkBudget) — the lists above may be missing components rather than the
  // plugin genuinely shipping fewer, so the panel says so instead of reading
  // as a complete inventory of a plugin that is just unusually large.
  truncated?: boolean;
}

export interface UpdateResult extends OkResult {
  id?: string;
  stdout?: string;
}

export const plugins = {
  list: () => callModule<{ plugins: Plugin[] }>("plugins", { action: "list" }),
  available: () => callModule<AvailablePlugins>("plugins", { action: "available" }),
  toggle: (id: string, enabled: boolean) =>
    callModule<OkResult & { id?: string; enabled?: boolean }>("plugins", {
      action: "toggle",
      id,
      enabled,
    }),
  contents: (id: string) =>
    callModule<PluginContents>("plugins", { action: "contents", id }),
  update: (id: string) => callModule<UpdateResult>("plugins", { action: "update", id }),
  // Slow by nature (the CLI may clone a repo); the server allows it ~120s, so
  // the caller must show a busy state rather than assume a quick answer.
  install: (id: string) => callModule<UpdateResult>("plugins", { action: "install", id }),
  uninstall: (id: string) => callModule<UpdateResult>("plugins", { action: "uninstall", id }),
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

// A memory FILE within a project — round 2's unit for this tab. `description`
// is the file's own YAML frontmatter `description:` value; null when the
// file has none, or it didn't parse (never guessed at from content).
export interface MemoryFile {
  name: string;
  description: string | null;
}

export interface MemoryProject {
  // The projects/ directory name — a munged cwd. Still the IDENTIFIER every
  // other memory action is keyed on (the server path-guards on it), which is
  // why it stays on the wire even though `path` is what a human reads.
  project: string;
  // The real folder the slug stands for, or null when the server could not
  // confirm one (no transcript recorded a cwd, and nothing on disk matched).
  // The munge is lossy, so a null here means "we refuse to guess", not "none".
  path: string | null;
  pathConfirmed: boolean;
  files: MemoryFile[];
}

export const memory = {
  list: () => callModule<{ projects: MemoryProject[] }>("memory", { action: "list" }),
  open: (project: string) => callModule<OkResult>("memory", { action: "open", project }),
  clear: (project: string) => callModule<OkResult>("memory", { action: "clear", project }),
  read: (project: string, file: string) =>
    callModule<OkResult & { text: string }>("memory", { action: "read", project, file }),
  write: (project: string, file: string, text: string) =>
    callModule<OkResult>("memory", { action: "write", project, file, text }),
  deleteFile: (project: string, file: string) =>
    callModule<OkResult>("memory", { action: "delete_file", project, file }),
};

// -- skills -------------------------------------------------------------------

/** The coding agents whose skills directories the page reads. */
export type CliId = "claude" | "copilot" | "codex";

export interface Skill {
  slug: string;
  name: string;
  description: string;
  source: "user" | "plugin";
  plugin?: string;
  marketplace?: string;
  linked: boolean;
  path: string;
  /** The skills directory it lives in. */
  location: string;
  /** Every copy and link of it (a user skill), for the delete confirmation. */
  paths: string[];
  /** The CLIs that read it. */
  readers: CliId[];
}

/** A skill a cloned marketplace's plugin ships, not yet on this machine
 *  unless `installed`. */
export interface AvailableSkill {
  slug: string;
  name: string;
  description: string;
  plugin: string;
  marketplace: string;
  path: string;
  installed: boolean;
}

export interface AvailableSkills {
  skills: AvailableSkill[];
  /** Plugins whose source is not inside the clone, so their skills are not listed. */
  skipped: number;
}

/** What addresses one skill: slugs collide across plugins, between a user
 *  skill and a plugin skill, and between the user directories. */
export type SkillRef = Pick<Skill, "slug" | "source" | "plugin" | "marketplace" | "location">;

const ident = (r: SkillRef) => ({
  slug: r.slug,
  source: r.source,
  plugin: r.plugin ?? "",
  marketplace: r.marketplace ?? "",
  location: r.source === "user" ? r.location : "",
});

export const skills = {
  list: () => callModule<{ skills: Skill[] }>("skills", { action: "list" }),
  available: () => callModule<AvailableSkills>("skills", { action: "available" }),
  add: (s: Pick<AvailableSkill, "marketplace" | "plugin" | "slug">) =>
    callModule<OkResult & { slug?: string }>("skills", {
      action: "add",
      marketplace: s.marketplace,
      plugin: s.plugin,
      slug: s.slug,
    }),
  open: (r: SkillRef) => callModule<OkResult>("skills", { action: "open", ...ident(r) }),
  create: (slug: string) => callModule<OkResult & { slug?: string }>("skills", { action: "create", slug }),
  read: (r: SkillRef, path = "SKILL.md") =>
    callModule<OkResult & { text: string; files: string[] }>("skills", { action: "read", ...ident(r), path }),
  write: (r: SkillRef, path: string, text: string) =>
    callModule<OkResult>("skills", { action: "write", ...ident(r), path, text }),
  delete: (r: SkillRef) => callModule<OkResult & { removed?: string[] }>("skills", { action: "delete", ...ident(r) }),
};

// -- mcp ----------------------------------------------------------------------

export type McpStatus = "connected" | "needs-auth" | "failed" | "pending" | "unknown";
export type McpKind = "user" | "connector" | "plugin";

export interface McpServer {
  name: string;
  endpoint: string;
  transport: string;
  status: McpStatus;
  // The CLI's own explanation, where it gave one — "HTTP 400: … Authorization
  // header is badly formatted". Empty for the statuses that need none. Row
  // chrome shows the status; this is the part that tells you what to DO, so it
  // belongs in the expanded panel rather than on the line.
  statusDetail: string;
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

export type McpListResult = OkResult & {
  servers?: McpServer[];
  stale?: boolean;
  checkedAt?: number;
};

export const mcp = {
  // Without `fresh` the server may answer from its cache at once (`stale`);
  // `fresh: true` waits for a real `claude mcp list` (~10 s).
  list: (opts?: { fresh?: boolean }) =>
    callModule<McpListResult>("mcp", { action: "list", ...(opts?.fresh ? { fresh: true } : {}) }),
  login: (name: string) =>
    callModule<OkResult & { launched?: boolean }>("mcp", { action: "login", name }),
  logout: (name: string) => callModule<CliResult>("mcp", { action: "logout", name }),
  remove: (name: string) => callModule<CliResult>("mcp", { action: "remove", name }),
  // `json` is the server definition as a JSON string — the CLI's own
  // `add-json` argument, validated by the module before it delegates.
  add: (name: string, json: string) => callModule<CliResult>("mcp", { action: "add", name, json }),
  // Replaces the server with a new definition (remove, then add-json).
  edit: (name: string, json: string) => callModule<CliResult>("mcp", { action: "edit", name, json }),
  // The user-scope definition on disk, to prefill an edit.
  spec: (name: string) => callModule<OkResult & { spec?: unknown }>("mcp", { action: "spec", name }),
};
