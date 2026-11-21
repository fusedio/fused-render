// The Preferences page's table of contents: every setting it shows, which
// group and section it sits in, and the words a search finds it by.
//
// PURE DATA, no React. The page renders a group by walking this list in
// order, and the search box matches against the same list, so a setting can
// never be findable but missing from its group (or the reverse). Each id
// below has exactly one control in `settings/controls.tsx`; the
// `CONTROLS` map there is typed against `SettingId` so a row added here
// without a control is a type error, not a blank card.
//
// GROUPS ARE NAMED BY WHAT A READER IS LOOKING FOR, not by which part of the
// codebase owns the flag. The page used to be one "Render preferences" tab of
// twenty sections in the order their flags shipped; the audit that replaced it
// is the design artifact claude.ai/artifact/NdzDR2rpNpFU3yBgQd5sJk.
//
// SECTIONS: a group with one section draws no section heading (the group's
// title already names it), and a section of one row is merged into a
// neighbour rather than drawn as a card of its own (2026-11-20). Rows that
// only pointed at another page ("Set elsewhere") were dropped then too.
import type { Prefs } from "@platform/lib/api";

// URL ids. Three keep the `?tab=` value their old tab had (`ai`, `indexing`,
// `lan`), so every link already pointing at one — the explorer's "index is
// off" card, a new-device notification, AGENT_PREFS_URL, the server's
// index-scan job page — still lands in the right place. `general` is the
// default and has the clean URL, the way "render" did. `account` (the old
// Accounts tab) reads as `integrations` — see `resolveGroup`.
export type GroupId =
  | "general"
  | "features"
  | "ai"
  | "claude-cli"
  | "tasks"
  | "apps"
  | "indexing"
  | "lan"
  | "connections"
  | "integrations"
  | "advanced";

// THE NAV IS SECTIONED BY WHAT THE SETTING CHANGES: the app itself, the
// coding agents and the sessions they run, the apps and files you work on,
// and the developer tools for when something breaks. A heading never repeats
// the one page under it, and a page is named for what is on it. A group
// belongs to exactly one section; a section with no visible group is not
// drawn.
export type NavSectionId = "app" | "agents" | "apps" | "developer";

export interface NavSection {
  id: NavSectionId;
  label: string;
}

export const NAV_SECTIONS: readonly NavSection[] = [
  { id: "app", label: "App" },
  { id: "agents", label: "Agents" },
  { id: "apps", label: "Apps & files" },
  { id: "developer", label: "Developer" },
];

export interface SettingsGroup {
  id: GroupId;
  label: string;
  /** A line under the group's title — only where it says something the
   *  title does not ("General: how the app looks…" was a definition). */
  blurb?: string;
  nav: NavSectionId;
  /** Worn beside the group's title on its page. */
  badge?: "Beta";
}

// In nav order: the sections above, and the groups inside each.
export const GROUPS: readonly SettingsGroup[] = [
  { id: "general", nav: "app", label: "General" },
  { id: "integrations", nav: "app", label: "Accounts" },
  {
    id: "connections",
    nav: "app",
    label: "Connections",
    blurb: "Sign in once and a service's tools work in your apps, bots and tasks. Tokens stay on this machine.",
  },
  {
    id: "features",
    nav: "app",
    label: "Experimental flags",
    badge: "Beta",
    blurb: "Still settling, and off until you turn them on.",
  },
  { id: "ai", nav: "agents", label: "Coding agents" },
  { id: "tasks", nav: "agents", label: "Chats & tasks" },
  {
    id: "claude-cli",
    nav: "agents",
    label: "Claude Code",
    blurb: "Keys in Claude Code's settings.json, read by every Claude session Fused Render runs.",
  },
  { id: "apps", nav: "apps", label: "Sharing & sync" },
  { id: "indexing", nav: "apps", label: "File index" },
  { id: "lan", nav: "apps", label: "Phone access" },
  { id: "advanced", nav: "developer", label: "Debugging" },
]

export interface SettingEntry {
  id: string;
  group: GroupId;
  section: string;
  label: string;
  /** What the setting does, shown behind the ⓘ beside its label. Empty when
   *  the label says it all (Appearance) — no ⓘ is drawn then. */
  description: string;
  /** Show the description under the label instead of behind an ⓘ: a row
   *  that is a message, not a control (Claude Code isn't installed). */
  inlineDescription?: true;
  /** Extra words a search should find this by: synonyms, the old name, the
   *  thing a reader would type rather than what the label says. */
  keywords: string;
  /** Marked with a macOS badge; also hidden off a Mac when `available` does
   *  not already answer for it. */
  macOnly?: true;
  badge?: "Beta";
  /** Drawn indented under the row above it — a setting that only means
   *  something while its parent is on. */
  child?: true;
  /** The setting this one needs on. While it is off the row is left out of
   *  its group, but search still finds it — drawn inert, saying which
   *  setting to turn on (see `parentOff`). */
  parent?: { id: string; on: (prefs: Prefs) => boolean };
  /** Whether the running app can honour this at all (a server-reported
   *  capability). Absent = always. */
  available?: (ctx: SettingsContext) => boolean;
  /** A device capability the row depends on. When the server lists it under
   *  `prefs.unavailable`, the row stays visible but disabled, with the reason
   *  in place of its status line (see `unavailableReason`). Unlike `available`,
   *  which hides a row for logic, this is for "this machine lacks it". */
  needs?: DeviceFeature;
}

/** The keys of `prefs.unavailable` (fused_render/shell/prefs.py). */
export type DeviceFeature = "index" | "hf" | "fused" | "update" | "native_windows" | "terminal";

export interface SettingsContext {
  prefs: Prefs;
  mac: boolean;
  /** GET /api/claude-config/status: ~/.claude exists on this machine. */
  claudeConfig: boolean;
}

const claudeMissing = ({ claudeConfig }: SettingsContext) => !claudeConfig;

const ENTRIES = [
  // ── General ──────────────────────────────────────────────────────────────
  {
    id: "appearance",
    group: "general",
    section: "Appearance",
    label: "Appearance",
    description: "",
    keywords: "dark mode light night theme colour color system",
  },
  {
    id: "theme",
    group: "general",
    section: "Appearance",
    label: "Colour theme",
    description: "",
    keywords: "theme palette contrast high-contrast midnight color accessibility",
  },
  {
    id: "native-windows",
    needs: "native_windows",
    group: "general",
    section: "Windows & updates",
    label: "Open in native windows",
    description: "Open apps and pages in their own app windows. Off, they open as tabs in your default browser, and any app windows already open close.",
    keywords: "browser tabs window desktop safari chrome",
  },
  {
    id: "launcher-hotkey",
    group: "general",
    section: "Keyboard",
    label: "Open the launcher",
    description: "A global shortcut for the search panel over your apps. To change it, click the key and press the new combination; Esc cancels.",
    keywords: "hotkey shortcut keyboard spotlight search apps bots launcher option space",
    macOnly: true,
    available: ({ prefs }) => !!prefs.launcher?.available,
  },
  {
    id: "launcher-row",
    group: "general",
    section: "Keyboard",
    label: "Open the Nth app",
    description: "Hold this and press 1–9 to open a recent app from anywhere. 0 opens the home window.",
    keywords: "shortcut modifier number recent hotkey keyboard digit",
    macOnly: true,
    available: ({ prefs }) => !!prefs.launcher?.available,
  },
  {
    id: "auto-update",
    needs: "update",
    group: "general",
    section: "Windows & updates",
    label: "Download updates automatically",
    description: "In the background. You still choose when to restart.",
    keywords: "update upgrade version download install auto release check now",
  },

  // ── Coding agents ────────────────────────────────────────────────────────
  {
    id: "agent-claude",
    group: "ai",
    section: "Agents",
    label: "Claude Code",
    description: "",
    keywords: "claude anthropic agent backend cli sign in login",
  },
  {
    id: "agent-copilot",
    group: "ai",
    section: "Agents",
    label: "GitHub Copilot CLI",
    description: "",
    keywords: "github copilot agent backend cli sign in login",
  },
  {
    id: "agent-codex",
    group: "ai",
    section: "Agents",
    label: "OpenAI Codex CLI",
    description: "",
    keywords: "openai codex chatgpt gpt agent backend cli sign in login",
  },
  {
    id: "agent-gemini",
    group: "ai",
    section: "Agents",
    label: "Google Gemini CLI",
    description: "",
    keywords: "google gemini agent backend cli sign in login api key",
  },
  {
    id: "agent-opencode",
    group: "ai",
    section: "Agents",
    label: "OpenCode",
    description: "",
    keywords: "opencode open code agent backend cli sign in login api key local model provider",
  },
  {
    id: "agent-cursor",
    group: "ai",
    section: "Agents",
    label: "Cursor CLI",
    description: "",
    keywords: "cursor cursor-agent anysphere agent backend cli sign in login api key",
  },
  {
    id: "agent-default",
    group: "ai",
    section: "Agents",
    label: "Default for new chats and tasks",
    description: "",
    keywords: "default agent backend",
    child: true,
    // Two or more of the six on: a default only means something when there
    // is a choice to make.
    available: ({ prefs }) =>
      [prefs.agents?.claude, prefs.agents?.copilot, prefs.agents?.codex, prefs.agents?.gemini, prefs.agents?.opencode,
        prefs.agents?.cursor]
        .filter(Boolean).length >= 2,
  },

  // ── Claude Code ──────────────────────────────────────────────────────────
  // The settings.json rows are data the server ships and are spliced in at
  // runtime (settings/claude.tsx); this row is the group's only static one,
  // shown on a machine without ~/.claude.
  {
    id: "claude-cli-missing",
    group: "claude-cli",
    section: "settings.json",
    label: "Claude Code isn't installed",
    inlineDescription: true,
    description: "Install Claude Code and its settings.json keys appear here.",
    keywords: "claude code cli install settings.json",
    available: claudeMissing,
  },

  // ── Tasks ────────────────────────────────────────────────────────────────
  {
    id: "project-queue",
    group: "tasks",
    section: "Running",
    label: "One task at a time per folder",
    description: "A chat message, Run now or a scheduled message in a busy folder waits and starts as soon as the folder frees. Waiting tasks show as Queued on the Tasks page.",
    keywords: "project queue queued concurrency parallel serial",
    badge: "Beta",
  },
  {
    id: "chat-recap",
    group: "tasks",
    section: "Summaries & notifications",
    label: "Chat recap",
    description: "When you come back to a chat, a one-line recap of what happened while you were away. One Haiku call per recap.",
    keywords: "recap summary away return fold haiku chat",
  },
  {
    id: "task-summary-titles",
    group: "tasks",
    section: "Summaries & notifications",
    label: "Stable AI titles",
    description: "A short Haiku-written title for what each task is about, refreshed a minute after each turn and every few messages.",
    keywords: "summary title name fixed stable rename ai stable haiku",
  },
  {
    id: "claude-strict-mcp",
    group: "tasks",
    section: "Running",
    label: "Lean Claude sessions",
    description: "Chats and tasks start without the MCP servers from your global Claude config and claude.ai connectors — about 200 MB less per session, so many can run at once. Plugin MCP servers still load. Scheduled runs, canvas agents and App Doctor are always lean.",
    keywords: "mcp memory connectors strict lean",
  },
  {
    id: "canvas-screenshots",
    group: "tasks",
    section: "Canvas screenshots",
    label: "Screen permission",
    description: "Native canvas screenshots take under a second, and on macOS need screen permission. Only the app's own workbench frame is captured, only on request; the picture is saved to the screenshots folder and attached to that chat, nowhere else.",
    keywords: "screenshot screen recording permission canvas capture native fast claude",
    macOnly: true,
  },
  {
    id: "canvas-screenshots-fallback",
    group: "tasks",
    section: "Canvas screenshots",
    label: "Fall back to in-canvas capture",
    description: "When a native screenshot fails, the workbench draws the canvas itself instead. Slow: 5–30 s with the canvas locked. Off by default on macOS, where allowing screen permission is the fix.",
    keywords: "screenshot fallback slow canvas capture html-to-image",
    macOnly: true,
  },
  {
    id: "claude-default-permission",
    group: "tasks",
    section: "Running",
    label: "Default approvals for new chats",
    description: "Each chat's own pick still wins and is remembered.",
    keywords: "approvals permission mode default auto plan ask accept edits",
  },
  {
    id: "breakout",
    group: "tasks",
    section: "Running",
    label: "Breakout side chats",
    description: "A research-only side chat forked from any point of a conversation. It can read and search but not run commands or edit files, except Markdown notes in the chat's handoff folder. Drafts come back into the composer.",
    keywords: "breakout fork side chat research read-only",
    badge: "Beta",
  },
  {
    id: "claude-max-sessions",
    group: "tasks",
    section: "Running",
    label: "Concurrent Claude sessions",
    description: "Tasks beyond this many wait in the queue. Chats you start yourself are never held.",
    keywords: "concurrent sessions limit cap memory queue parallel",
  },
  {
    id: "notify-terminal",
    needs: "terminal",
    group: "tasks",
    section: "Summaries & notifications",
    label: "Notify for terminal sessions",
    description: "Also notify when a claude session you started by hand in a terminal finishes.",
    keywords: "notification notify finished terminal claude alert done",
  },

  // ── Sharing & sync ───────────────────────────────────────────────────────
  {
    id: "git-sync",
    group: "apps",
    section: "Sharing & sync",
    label: "Sync app changes with Git",
    description: "Push the app's own commits, and fast-forward when you open an app. Default branch only; nothing is merged or forced, and you're notified if a sync can't happen.",
    keywords: "git push pull commit fast-forward sync github remote auto",
  },
  // Owner 2026-10-27: what Share offers — Public link (this row), the
  // GitHub card + install link (always, since 2026-10-29) and Remix. The
  // Share button is always there (share-app-flag.ts `useAppShareOffered`);
  // .fused file export always.
  {
    id: "share-sheet",
    needs: "fused",
    group: "apps",
    section: "Sharing & sync",
    label: "Public link",
    description: "Share an app as a public link on your Fused account, from the Share button.",
    keywords: "share export link publish .fused file public download canvas account",
  },
  {
    id: "live-previews",
    group: "apps",
    section: "Sharing & sync",
    label: "Live previews in thumbnails",
    description: "Cards without a preview.png show the running app, and hovering swaps it in. Each one boots a page in the background.",
    keywords: "thumbnail preview iframe cards live hover image",
  },
  // Community: Remix on its own switch (docs/CONTRIB_LOOP.md §1), off by
  // default. The Community store and the contribution loop stopped being
  // switches on 2026-10-29 — always on, no row.
  {
    id: "contrib-remix",
    group: "apps",
    section: "Sharing & sync",
    label: "Remix",
    description: "Publish your changed version of an app as your own, credited to the original.",
    keywords: "community contribute remix fork publish credit original",
    badge: "Beta",
  },

  // ── File index ───────────────────────────────────────────────────────────
  {
    id: "indexing",
    needs: "index",
    group: "indexing",
    section: "Index",
    label: "File indexing",
    description: "Indexes file names, sizes and dates (never contents) so searching a folder is instant. Off, search walks folders live.",
    keywords: "index search scan spotlight files find",
  },
  {
    id: "ranked-search",
    needs: "index",
    group: "indexing",
    section: "Index",
    label: "Rank results by relevance",
    description: "Best matches first. Off, matches are listed shallowest-first, then alphabetically.",
    keywords: "search ranking relevance order sort",
  },
  {
    id: "low-impact",
    needs: "index",
    group: "indexing",
    section: "Index",
    label: "Low-impact indexing",
    description: "For a busy machine: longer waits before rescans and one worker at a time. Results may lag a minute or two.",
    keywords: "battery cpu background slow priority fan quiet",
  },
  {
    id: "skipped-folders",
    needs: "index",
    group: "indexing",
    section: "Index",
    label: "Skipped folders",
    description: "The index never looks inside these, such as dependency and build caches.",
    keywords: "ignore exclude skip node_modules gitignore rules",
  },
  {
    id: "rebuild-index",
    needs: "index",
    group: "indexing",
    section: "Index",
    label: "Rebuild the index",
    description: "Re-index picks up changes. Full scan starts over. Delete removes the index; search walks folders until the next scan.",
    keywords: "reindex rescan delete rebuild reset full scan",
  },

  // ── Experimental flags ─────────────────────────────────────────────────────────────
  // TASK ICONS (owner, 2026-11-10): a face per FAVOURITE, in place of the
  // ring (2026-11-11: only a starred task wears one).
  {
    id: "tasks-icons",
    group: "features",
    section: "Chats & rail",
    label: "Task icons",
    description: "A favourite shows its own small picture — an animal, a food, a thing — instead of the status ring, on the rail, the Tasks list and the task header. Click it while editing the title to pick another.",
    keywords: "icon glyph picture emoji animal face ring task rail avatar favourite favorite star",
  },
  // A FILE'S OTHER MODES AS DOORS ON THE RAIL (shell/claude-rail
  // rail-mode-doors-flag; owner, 2026-11-20).
  {
    id: "rail-mode-doors",
    group: "features",
    section: "Chats & rail",
    label: "File views on the rail",
    description: "On a file, its other views (Code, Reader and so on) show as icons on the rail above Git. Hover one to preview the file that way in a floating card; click to keep it open, Open switches the page to that view.",
    keywords: "mode view preview code reader rail door icon file template switch hover card git",
  },
  // FREE PINS ON THE RAIL (shell/claude-rail/rail-free-pins-flag).
  {
    id: "rail-free-pins",
    group: "features",
    section: "Chats & rail",
    label: "Free rail pins",
    description: "A pinned rail window keeps its exact place and size. Other chats open in a second window beside the rail, so up to four can stand at once: pinned and unpinned, for the list and for favourites.",
    keywords: "pin pinned window rail float overlap move resize four windows push task favourite bot",
  },
  {
    id: "canvases",
    needs: "fused",
    group: "features",
    section: "Other",
    label: "Canvases",
    description: "Your Fused Workbench canvases, opened locally with the live workbench embedded. Shows in the sidebar once you're signed in to Fused.",
    keywords: "canvas workbench udf fused",
  },
  {
    id: "canvases-embed-proxy",
    needs: "fused",
    group: "features",
    section: "Other",
    label: "Load a custom deployment's workbench through a local proxy",
    description: "For a custom deployment in ~/.fused/settings.toml whose workbench needs a browser login: it loads through a local proxy that adds your Fused token. Prod and Unstable are unaffected. canvases_embed_proxy_enabled under [render] in that file overrides this.",
    keywords: "proxy embed custom deployment settings.toml render workbench login gateway bearer token",
    child: true,
    // Only means something while Canvases is offered at all.
    available: ({ prefs }) => prefs.canvases?.enabled === true,
  },
  // EXPERTS (docs/experts.md §17; owner, 2026-11-19): off by default. Off
  // hides every expert bot and stops what runs for them; their memory stays
  // on disk and is there again when this is turned back on.
  {
    id: "experts",
    group: "features",
    section: "Other",
    label: "Expert bots",
    description: "Bots that each keep one memory file about one subject — Canvas Reference and Data Expert ship with the app — and that your Claude, Codex and Copilot tasks can consult. Off hides them and stops their learning; their memory is kept.",
    keywords: "expert experts memory canvas reference data expert bot learn ask",
  },
  // SPECIALISTS (docs/specialists.md): a kind of bot on the Bots page, and
  // the task tools and the breakout's "With". Off by default; off, nothing of
  // it exists. Beside Expert bots, under Other: the two solve the same
  // problem two ways.
  {
    id: "specialists",
    group: "features",
    section: "Other",
    label: "Specialists",
    description: "A kind of bot on the Bots page that knows one subject and keeps its own memory and skills. Tasks get its guidance, send it notes, and can open a breakout with it. Once a day it updates its memory from what they did.",
    keywords: "specialist bot memory skills breakout notes update learn",
  },
  {
    id: "monitor",
    group: "general",
    section: "Tools",
    label: "Process monitor",
    description: "A System chip in the status bar with this app's CPU and memory use, and a live Monitor page.",
    keywords: "cpu memory ram process system activity performance",
  },
  {
    id: "reader",
    group: "general",
    section: "Tools",
    label: "Reader",
    description: "Reads text files and PDFs aloud.",
    keywords: "read aloud speech tts listen accessibility pdf voice",
  },
  {
    id: "chromium-windows",
    group: "features",
    section: "Other",
    label: "Chromium engine for windows",
    description: "New app windows use Chromium instead of the system WebKit view — faster for heavy apps. The menu-bar dock, launcher and pinned popover always use WebKit.",
    keywords: "chromium cef webkit engine rendering performance blink",
    macOnly: true,
    available: ({ prefs }) => !!prefs.chromium_windows?.available,
  },

  // ── Phone access ─────────────────────────────────────────────────────────
  {
    id: "lan",
    group: "lan",
    section: "Local network",
    label: "Share my apps on this Wi-Fi",
    description: "Only devices you pair with the code get in. A paired device can open and run your apps and read or change their files; nothing else on this computer is reachable.",
    keywords: "lan wifi phone iphone network qr pair mobile local devices revoke",
  },


  // ── Connections ──────────────────────────────────────────────────────────
  // The group draws its own page (shell/Connections.tsx: a card per provider
  // the server lists, which the registry cannot know ahead of time); this row
  // is what search finds, a pointer into it.
  {
    id: "connections",
    group: "connections",
    section: "Services",
    label: "Connections",
    description: "Notion, GitHub, Slack and other services whose tools your apps, bots and tasks can use.",
    keywords: "connect connection oauth mcp sign in login api key notion github slack service tools",
  },

  // ── Accounts ─────────────────────────────────────────────────────────────
  {
    id: "fused-account",
    needs: "fused",
    group: "integrations",
    section: "Sign-ins",
    label: "Fused account",
    description: "Used by Share and Canvases. Signing in opens fused.io in your browser.",
    keywords: "fused login sign in log out account workbench",
  },
  {
    id: "workbench-env",
    needs: "fused",
    group: "integrations",
    section: "Sign-ins",
    label: "Default environment",
    description: "The canvas catalog opens on this environment; `fused` in chats outside a canvas clone and share uploads target it. Canvases keep the environment they were cloned from.",
    keywords: "default environment unstable prod production staging workbench canvases fused env",
    child: true,
  },
  {
    id: "huggingface",
    needs: "hf",
    group: "integrations",
    section: "Sign-ins",
    label: "Hugging Face",
    description: "Faster model downloads and access to gated or private repos. The token is stored by huggingface_hub, as hf auth login does.",
    keywords: "huggingface hf token hub model download gated login sign in",
  },

  // ── Advanced ─────────────────────────────────────────────────────────────
  {
    id: "call-log",
    group: "advanced",
    section: "Debugging",
    label: "Record API calls",
    description: "Every runPython, readFile, stat and writeFile your pages make, with duration, result size, output and any traceback. Read it with fused-render calls or the Monitor's Network tab.",
    keywords: "call log api runpython logging record debug",
  },
  {
    id: "call-params",
    group: "advanced",
    section: "Debugging",
    label: "Parameters",
    description: "A run's parameters are usually the whole repro. Choose names only if a page passes a secret as a parameter.",
    keywords: "secret redact privacy params",
    child: true,
    parent: { id: "call-log", on: (prefs) => prefs.calls?.effective_enabled === true },
  },
  {
    id: "call-retention",
    group: "advanced",
    section: "Debugging",
    label: "Keep for",
    description: "Older calls are deleted automatically.",
    keywords: "retention days delete cleanup",
    child: true,
    parent: { id: "call-log", on: (prefs) => prefs.calls?.effective_enabled === true },
  },
  {
    id: "index-query",
    needs: "index",
    group: "advanced",
    section: "Debugging",
    label: "Query the file index",
    description: "Read-only SQL over the index, or ask in plain English. Nothing here can write, or read a file outside the index.",
    keywords: "sql query index database duckdb",
  },
  {
    id: "udf-saved",
    group: "advanced",
    section: "Debugging",
    label: "Saved UDF cache",
    description: "The last good answer of each UDF you ran, kept so a UDF opens at once. Clearing them makes the next open of each UDF run it fresh.",
    keywords: "udf cache saved results clear disk fused workbench",
  },
  {
    id: "diagnostics",
    group: "advanced",
    section: "Debugging",
    label: "Save diagnostics",
    description: "Logs, crash reports, memory samples and the list of open apps, zipped to your Downloads folder. No chat transcripts, credentials or mount passwords.",
    keywords: "bug report logs crash zip support debug system log",
  },
] as const satisfies readonly SettingEntry[];

export type SettingId = (typeof ENTRIES)[number]["id"];

export const SETTINGS: readonly (SettingEntry & { id: SettingId })[] = ENTRIES;

const GROUP_BY_ID = new Map(GROUPS.map((g) => [g.id, g]));

export function groupOf(id: GroupId): SettingsGroup {
  return GROUP_BY_ID.get(id)!;
}

export function navSectionOf(id: NavSectionId): NavSection {
  return NAV_SECTIONS.find((s) => s.id === id)!;
}

/** Whether a row is drawn on this machine. */
export function isVisible(entry: SettingEntry, ctx: SettingsContext): boolean {
  if (entry.macOnly && !entry.available && !ctx.mac) return false;
  if (entry.available && !entry.available(ctx)) return false;
  return true;
}

/** Why this row is disabled on this device, or null when it is usable. */
export function unavailableReason(
  entry: Pick<SettingEntry, "needs">,
  ctx: Pick<SettingsContext, "prefs"> | null | undefined,
): string | null {
  if (!entry.needs) return null;
  return ctx?.prefs?.unavailable?.[entry.needs] ?? null;
}

/** The label of the setting this row needs on, while that one is off; null
 *  when the row has no parent or its parent is on. */
export function parentOff(entry: Pick<SettingEntry, "parent">, prefs: Prefs | null | undefined): string | null {
  if (!entry.parent || !prefs || entry.parent.on(prefs)) return null;
  return settingById(entry.parent.id)?.label ?? null;
}

/** Everything search can find on this machine: child rows included even
 *  while their parent is off (2026-11-20). */
export function searchableSettings(ctx: SettingsContext): (SettingEntry & { id: SettingId })[] {
  return SETTINGS.filter((e) => isVisible(e, ctx));
}

/** The rows a group draws: what search can find, less the child rows whose
 *  parent is off. */
export function visibleSettings(ctx: SettingsContext): (SettingEntry & { id: SettingId })[] {
  return searchableSettings(ctx).filter((e) => !parentOff(e, ctx.prefs));
}

/** Groups that have at least one visible row, in page order. */
export function visibleGroups(entries: readonly SettingEntry[]): SettingsGroup[] {
  return GROUPS.filter((g) => entries.some((e) => e.group === g.id));
}

/** The nav: each section with the visible groups under it; empty sections dropped. */
export function navSections(groups: readonly SettingsGroup[]): { section: NavSection; groups: SettingsGroup[] }[] {
  return NAV_SECTIONS.map((section) => ({ section, groups: groups.filter((g) => g.nav === section.id) })).filter(
    (s) => s.groups.length > 0,
  );
}

/** A group's rows bucketed by section, sections in first-appearance order. */
export function sectionsOf<T extends SettingEntry>(
  entries: readonly T[],
  group: GroupId,
): { section: string; entries: T[] }[] {
  const out: { section: string; entries: T[] }[] = [];
  for (const e of entries) {
    if (e.group !== group) continue;
    let bucket = out.find((s) => s.section === e.section);
    if (!bucket) {
      bucket = { section: e.section, entries: [] };
      out.push(bucket);
    }
    bucket.entries.push(e);
  }
  return out;
}

/** `?tab=` → the group to show. "render" was the old default tab's name,
 *  "account" the Accounts tab's (Integrations since 2026-10-20); anything
 *  unknown (a dead `?tab=engines` bookmark) or with no visible row falls
 *  back to the first group rather than an empty
 *  panel. */
export function resolveGroup(tab: string | null, groups: readonly SettingsGroup[]): GroupId {
  const wanted =
    tab === "render" ? "general" : tab === "account" ? "integrations" : tab;
  const hit = groups.find((g) => g.id === wanted);
  return (hit ?? groups[0] ?? GROUPS[0]).id;
}

export function settingById(id: string | null): (SettingEntry & { id: SettingId }) | undefined {
  return id ? SETTINGS.find((e) => e.id === id) : undefined;
}
