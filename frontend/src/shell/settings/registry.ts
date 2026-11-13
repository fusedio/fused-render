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
// "SET ELSEWHERE" rows are pointers to a setting whose real control lives on
// another page (the Claude Code page's Claude model, AI Models' engines).
// They are in the list so a search for "model" lands on a door instead of on
// nothing — the page used to lose those settings silently when they moved.
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
  blurb: string;
  nav: NavSectionId;
  /** Worn beside the group's title on its page. */
  badge?: "Beta";
}

// In nav order: the sections above, and the groups inside each.
export const GROUPS: readonly SettingsGroup[] = [
  { id: "general", nav: "app", label: "General", blurb: "How the app looks, opens and stays up to date." },
  { id: "integrations", nav: "app", label: "Accounts", blurb: "Services this app signs in to." },
  {
    id: "connections",
    nav: "app",
    label: "Connections",
    blurb: "Sign in to a service once and its tools work in your apps, bots and tasks. Tokens stay on this machine.",
  },
  {
    id: "features",
    nav: "app",
    label: "Experimental flags",
    badge: "Beta",
    blurb: "Off by default and still settling. Each one adds a sidebar row or a page when you turn it on.",
  },
  { id: "ai", nav: "agents", label: "Coding agents", blurb: "Which coding agents run your chats and tasks." },
  {
    id: "tasks",
    nav: "agents",
    label: "Chats & tasks",
    blurb: "How sessions queue, get titled and tell you they are done.",
  },
  {
    id: "claude-cli",
    nav: "agents",
    label: "Claude Code",
    blurb: "The settings.json keys that shape the Claude sessions Fused Render runs.",
  },
  { id: "apps", nav: "apps", label: "Sharing & sync", blurb: "Git sync, sharing, and how app cards preview." },
  { id: "indexing", nav: "apps", label: "File index", blurb: "The file index behind Files search." },
  { id: "lan", nav: "apps", label: "Phone access", blurb: "Open your apps from a phone on the same Wi-Fi." },
  {
    id: "advanced",
    nav: "developer",
    label: "Debugging",
    blurb: "Call logging, the index query console, and diagnostics for bug reports.",
  },
];

export const ELSEWHERE = "Set elsewhere";

export interface SettingEntry {
  id: string;
  group: GroupId;
  section: string;
  label: string;
  description: string;
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
  /** A pointer to another page, not a control. */
  elsewhere?: true;
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
  /** GET /api/claude-config/status: ~/.claude exists on this machine; the
   *  model pointer leads to the Claude Code page's settings. */
  claudeConfig: boolean;
}

const claudeOnly = ({ claudeConfig }: SettingsContext) => claudeConfig;
const claudeMissing = ({ claudeConfig }: SettingsContext) => !claudeConfig;

const ENTRIES = [
  // ── General ──────────────────────────────────────────────────────────────
  {
    id: "appearance",
    group: "general",
    section: "Appearance",
    label: "Appearance",
    description: "Follow your desktop, or always use light or dark. Saved per browser profile.",
    keywords: "dark mode light night theme colour color system",
  },
  {
    id: "theme",
    group: "general",
    section: "Appearance",
    label: "Colour theme",
    description: "The app shell's palette. Each theme has a light and a dark variant; pages you open follow light or dark only.",
    keywords: "theme palette contrast high-contrast midnight color accessibility",
  },
  {
    id: "native-windows",
    needs: "native_windows",
    group: "general",
    section: "Windows",
    label: "Open in native windows",
    description: "Use the app's own windows instead of browser tabs. Turning this off closes them and opens everything in your default browser.",
    keywords: "browser tabs window desktop safari chrome",
  },
  {
    id: "launcher-hotkey",
    group: "general",
    section: "Keyboard",
    label: "Open the launcher",
    description: "Global shortcut for the search panel over your apps. Click the key, then press the new combination; Esc cancels.",
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
    section: "Updates",
    label: "Download updates automatically",
    description: "New versions download in the background. You still choose when to restart.",
    keywords: "update upgrade version download install auto release check now",
  },

  // ── Coding agents ────────────────────────────────────────────────────────
  {
    id: "agent-claude",
    group: "ai",
    section: "Agents",
    label: "Claude Code",
    description: "Runs chats, tasks and bots.",
    keywords: "claude anthropic agent backend cli sign in login",
  },
  {
    id: "agent-copilot",
    group: "ai",
    section: "Agents",
    label: "GitHub Copilot CLI",
    description: "Runs chats and tasks. Bots keep using Claude.",
    keywords: "github copilot agent backend cli sign in login",
  },
  {
    id: "agent-codex",
    group: "ai",
    section: "Agents",
    label: "OpenAI Codex CLI",
    description: "Runs chats and tasks with your ChatGPT or OpenAI account. Bots keep using Claude.",
    keywords: "openai codex chatgpt gpt agent backend cli sign in login",
  },
  {
    id: "agent-gemini",
    group: "ai",
    section: "Agents",
    label: "Google Gemini CLI",
    description: "Runs chats and tasks with your Google account or a Gemini API key. Bots keep using Claude.",
    keywords: "google gemini agent backend cli sign in login api key",
  },
  {
    id: "agent-opencode",
    group: "ai",
    section: "Agents",
    label: "OpenCode",
    description: "Runs chats and tasks with any provider OpenCode knows, or a local model with no account at all. Bots keep using Claude.",
    keywords: "opencode open code agent backend cli sign in login api key local model provider",
  },
  {
    id: "agent-default",
    group: "ai",
    section: "Agents",
    label: "Default for new chats and tasks",
    description: "With more than one agent on, new chats and tasks start here. A conversation keeps the agent it started with.",
    keywords: "default agent backend",
    child: true,
    // Two or more of the five on: a default only means something when there
    // is a choice to make.
    available: ({ prefs }) =>
      [prefs.agents?.claude, prefs.agents?.copilot, prefs.agents?.codex, prefs.agents?.gemini, prefs.agents?.opencode]
        .filter(Boolean).length >= 2,
  },
  {
    id: "claude-model",
    group: "ai",
    section: ELSEWHERE,
    label: "Default Claude model",
    description: "One setting in Claude Code's settings.json, shared by the chat composer, the New task card and the CLI.",
    keywords: "model opus sonnet haiku fable default fused.ai thinking effort",
    elsewhere: true,
    available: claudeOnly,
  },
  {
    id: "engines",
    group: "ai",
    section: ELSEWHERE,
    label: "Local model engines",
    description: "Which backend serves each local-model capability, and when idle models unload.",
    keywords: "engine inference local model gguf mlx llama unload idle memory",
    elsewhere: true,
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
    description: "~/.claude was not found on this machine. Install Claude Code and its settings.json keys appear here.",
    keywords: "claude code cli install settings.json",
    available: claudeMissing,
  },

  // ── Tasks ────────────────────────────────────────────────────────────────
  {
    id: "project-queue",
    group: "tasks",
    section: "Running",
    label: "One task at a time per folder",
    description: "Anything else asked for in a busy folder — a chat message, Run now, a scheduled message coming due — waits its turn and starts the moment the folder frees. A waiting task reads Queued on the Tasks page.",
    keywords: "project queue queued concurrency parallel serial",
    badge: "Beta",
  },
  {
    id: "chat-recap",
    group: "tasks",
    section: "Titles",
    label: "Chat recap",
    description: "When you come back to a chat, show a one-line recap of what happened while you were away. Uses one Haiku call per recap.",
    keywords: "recap summary away return fold haiku chat",
  },
  {
    id: "task-summary-titles",
    group: "tasks",
    section: "Titles",
    label: "Stable AI titles",
    description: "Name each task with a short AI title for what the conversation is about. Refreshed a minute after each turn ends and every few messages. Uses Haiku.",
    keywords: "summary title name fixed stable rename ai stable haiku",
  },
  {
    id: "claude-strict-mcp",
    group: "tasks",
    section: "Running",
    label: "Lean Claude sessions",
    description: "Chat and tasks start without the MCP servers from your global Claude config and your claude.ai connectors, so many sessions can run at once (about 200 MB less each). MCP servers from your enabled plugins still load. Scheduled runs, canvas agents and App Doctor are always lean.",
    keywords: "mcp memory connectors strict lean",
  },
  {
    id: "canvas-screenshots",
    group: "tasks",
    section: "Canvas screenshots",
    label: "Screen permission",
    description: "Canvas screenshots — yours and your agent's — are taken natively, in under a second. On macOS that needs screen permission. The app photographs only its own workbench frame, only when a screenshot is asked for; the picture is saved to the app's screenshots folder and attached to that chat, nowhere else.",
    keywords: "screenshot screen recording permission canvas capture native fast claude",
    macOnly: true,
  },
  {
    id: "canvas-screenshots-fallback",
    group: "tasks",
    section: "Canvas screenshots",
    label: "Fall back to in-canvas capture",
    description: "When the native screenshot cannot be taken, let the workbench draw the canvas itself instead of failing. Slow: 5–30 s with the canvas locked meanwhile. Off by default on macOS, where allowing screen permission is the fix.",
    keywords: "screenshot fallback slow canvas capture html-to-image",
    macOnly: true,
  },
  {
    id: "claude-default-permission",
    group: "tasks",
    section: "Running",
    label: "Default approvals for new chats",
    description: "What a new Claude chat starts on: Plan only, Ask every time, Accept edits, or Auto. Each chat's own pick still wins and is remembered.",
    keywords: "approvals permission mode default auto plan ask accept edits",
  },
  {
    id: "breakout",
    group: "tasks",
    section: "Running",
    label: "Breakout side chats",
    description: "Open a research-only side chat forked from any point of a conversation (a message older than the chat's last compaction starts from that summary). It reads and searches; it cannot run commands or edit files, except Markdown notes in the chat's handoff folder. Drafts come back into the composer.",
    keywords: "breakout fork side chat research read-only",
    badge: "Beta",
  },
  {
    id: "claude-max-sessions",
    group: "tasks",
    section: "Running",
    label: "Concurrent Claude sessions",
    description: "Tasks past this many running Claude sessions wait in the queue. Chat you start yourself is never held.",
    keywords: "concurrent sessions limit cap memory queue parallel",
  },
  {
    id: "notify-terminal",
    needs: "terminal",
    group: "tasks",
    section: "Notifications",
    label: "Notify for terminal sessions",
    description: "Also send Finished notifications for a claude session you started by hand in a terminal.",
    keywords: "notification notify finished terminal claude alert done",
  },

  // ── Sharing & sync ───────────────────────────────────────────────────────
  {
    id: "git-sync",
    group: "apps",
    section: "Git",
    label: "Sync app changes with Git",
    description: "Push the commits the app makes itself and fast-forward when you open an app. Default branch only; nothing is merged or forced, and you get a notification if it cannot.",
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
    section: "Sharing",
    label: "Public link",
    description: "Share an app as a public link on your Fused account. The Share button offers it next to the .fused file.",
    keywords: "share export link publish .fused file public download canvas account",
  },
  {
    id: "live-previews",
    group: "apps",
    section: "Thumbnails",
    label: "Live previews in thumbnails",
    description: "Cards without a preview.png show the running app, and hovering swaps it in. Each one boots a whole page in the background.",
    keywords: "thumbnail preview iframe cards live hover image",
  },
  // Community: Remix on its own switch (docs/CONTRIB_LOOP.md §1), off by
  // default. The Community store and the contribution loop stopped being
  // switches on 2026-10-29 — always on, no row.
  {
    id: "contrib-remix",
    group: "apps",
    section: "Community",
    label: "Remix",
    description: "Publish your changed version as an app of your own, credited to the original.",
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
    description: "Keep a local index of your files' names, sizes and dates (no contents), so searching a folder is instant. Off, search walks folders live.",
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
    description: "Keep the index quiet on a busy machine: longer waits before rescans and one worker at a time. Results may lag a minute or two.",
    keywords: "battery cpu background slow priority fan quiet",
  },
  {
    id: "skipped-folders",
    needs: "index",
    group: "indexing",
    section: "Scope",
    label: "Skipped folders",
    description: "Folders the index never looks inside, such as dependency and build caches.",
    keywords: "ignore exclude skip node_modules gitignore rules",
  },
  {
    id: "rebuild-index",
    needs: "index",
    group: "indexing",
    section: "Maintenance",
    label: "Rebuild the index",
    description: "Re-index picks up changes. A full scan starts over. Delete removes the index; search walks folders until the next scan.",
    keywords: "reindex rescan delete rebuild reset full scan",
  },

  // ── Experimental flags ─────────────────────────────────────────────────────────────
  {
    id: "canvases",
    needs: "fused",
    group: "features",
    section: "Sidebar",
    label: "Canvases",
    description: "Your Fused Workbench canvases, opened locally with the live workbench embedded. Appears in the sidebar once you are signed in to Fused.",
    keywords: "canvas workbench udf fused",
  },
  {
    id: "canvases-embed-proxy",
    needs: "fused",
    group: "features",
    section: "Sidebar",
    label: "Load a custom deployment's workbench through a local proxy",
    description: "For a deployment in ~/.fused/settings.toml whose workbench needs a browser login. The embedded workbench loads through a loopback proxy that adds your Fused token. Prod and Unstable canvases are unaffected. canvases_embed_proxy_enabled under [render] in that file overrides this switch.",
    keywords: "proxy embed custom deployment settings.toml render workbench login gateway bearer token",
    child: true,
    // Only means something while Canvases is offered at all.
    available: ({ prefs }) => prefs.canvases?.enabled === true,
  },
  // THE CLAUDE RAIL'S PREVIEW (shell/claude-rail): one rail on every page
  // with a Claude panel — the canvas page, the app page, the explorer's file
  // and folder views (2026-11-01) — so the switch is its own row, not a
  // child of Canvases, and needs no Fused sign-in. The pref keeps its
  // `canvas_rail_peek` name on the wire.
  {
    id: "canvases-rail-peek",
    group: "features",
    section: "Sidebar",
    label: "Preview chats from the collapsed Claude rail",
    description: "With the Claude panel hidden — on an app page, in the explorer, on a canvas — hovering a chat on the rail at the right edge previews it as a floating card beside it; clicking keeps the card open, ready to type, and lets you resize it from its left corners. Click elsewhere, press Escape, or use its × or Open to close it.",
    keywords: "rail hover click preview peek card chat collapsed claude panel popover canvas app explorer",
  },
  // FAVOURITE CHATS (owner, 2026-11-03): the star on that preview card, so
  // a child of it — nothing to star without the card. Always shown, though:
  // a stored "on" under a preview switched off must stay reachable.
  {
    id: "tasks-favourites",
    group: "features",
    section: "Sidebar",
    label: "Favourite chats",
    description: "A star beside the id in every list of chats, and on the rail's preview card, keeps a chat in a Favourites section at the foot of the rail, and its own smaller card open at the bottom right — close it with its ×, reopen it from its ring. With favourites on, the rail stands at the right edge of every app page, explorer view and canvas whether the Claude panel is shown or hidden, as long as it has a chat or a favourite to show.",
    keywords: "favourite favorite star pin keep chat rail card bottom always panel",
    child: true,
  },
  // TASK ICONS (owner, 2026-11-10): a face per FAVOURITE, in place of the
  // ring (2026-11-11: only a starred task wears one).
  {
    id: "tasks-icons",
    group: "features",
    section: "Sidebar",
    label: "Task icons",
    description: "A favourite wears its own small picture — an animal, a food, a thing — in place of the status ring, in the ring's colour, on the rail, the Tasks list and the task's head. Star a chat to give it one; remove the star and the ring is back. While editing a favourite's title, click the picture to choose another. Needs Favourite chats on.",
    keywords: "icon glyph picture emoji animal face ring task rail avatar favourite favorite star",
  },
  // ONE HEADER FOR EVERY TASK VIEW (shell/TaskHead, task-head-flag). After the
  // rail preview's two children, not between them: a row set there read as
  // the parent of "Favourite chats".
  {
    id: "tasks-unified-head",
    group: "features",
    section: "Sidebar",
    label: "Unified task header",
    description: "One header for every task view: the Tasks page panel, the chat panes, the rail cards and the New task card.",
    keywords: "task header unified head close back prev next status star open kebab card panel rail",
  },
  {
    id: "monitor",
    group: "general",
    section: "Status bar",
    label: "Process monitor",
    description: "A System chip in the status bar with what this app and its processes cost in CPU and memory, and a live Monitor page.",
    keywords: "cpu memory ram process system activity performance",
  },
  {
    id: "reader",
    group: "general",
    section: "Accessibility",
    label: "Reader",
    description: "Adds a Reader mode to text files and PDFs that reads them aloud.",
    keywords: "read aloud speech tts listen accessibility pdf voice",
  },
  {
    id: "chromium-windows",
    group: "features",
    section: "Windows",
    label: "Chromium engine for windows",
    description:
      "New app windows are drawn by Chromium instead of the system WebKit view — faster for heavy apps. Off, new windows use WebKit; the menu-bar dock, launcher and pinned popover always do.",
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
    description: "Open your apps from a phone on the same network. Only devices you pair with the code get in; a paired device can open and run your apps and read or change their files, and nothing else on this computer is reachable.",
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
    description: "Used by the Share sheet and Canvases. Signing in opens fused.io in your browser.",
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
    description: "Faster model downloads and access to gated or private repos. The token is stored by huggingface_hub, the same way hf auth login stores it.",
    keywords: "huggingface hf token hub model download gated login sign in",
  },
  {
    id: "agent-signin",
    group: "integrations",
    section: ELSEWHERE,
    label: "Claude Code, Copilot, Codex, Gemini and OpenCode sign-in",
    description: "Shown with each agent's health under Coding agents.",
    keywords: "claude login anthropic copilot github codex openai chatgpt gemini google opencode sign in",
    elsewhere: true,
  },

  // ── Advanced ─────────────────────────────────────────────────────────────
  {
    id: "call-log",
    group: "advanced",
    section: "Call log",
    label: "Record API calls",
    description: "Log every runPython, readFile, stat and writeFile your pages make, with duration, result size, output and any traceback. Read it with the fused-render calls command or the Monitor's Network tab.",
    keywords: "call log api runpython logging record debug",
  },
  {
    id: "call-params",
    group: "advanced",
    section: "Call log",
    label: "Parameters",
    description: "A run's parameters are usually the whole repro. Switch to names only if a page passes a secret as a parameter.",
    keywords: "secret redact privacy params",
    child: true,
  },
  {
    id: "call-retention",
    group: "advanced",
    section: "Call log",
    label: "Keep for",
    description: "Older calls are deleted automatically.",
    keywords: "retention days delete cleanup",
    child: true,
  },
  {
    id: "index-query",
    needs: "index",
    group: "advanced",
    section: "Index query",
    label: "Query the file index",
    description: "Read-only SQL over the index, or ask in plain English. Nothing here can write or read a file outside the index.",
    keywords: "sql query index database duckdb",
  },
  {
    id: "diagnostics",
    group: "advanced",
    section: "Diagnostics",
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

/** The rows this machine draws. */
export function visibleSettings(ctx: SettingsContext): (SettingEntry & { id: SettingId })[] {
  return SETTINGS.filter((e) => isVisible(e, ctx));
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
