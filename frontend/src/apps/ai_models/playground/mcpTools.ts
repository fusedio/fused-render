// MCP servers configured for the TEXT stage's chat — the playground's own,
// separate from Claude Code's global list (`apps/claude_config/sections/
// McpSection.tsx`, `claude mcp add/list/remove`). That list is what Claude
// Code's OWN agentic sessions read; this one is what a `/api/ai` chat turn
// (either tier — `server/ai.py`'s own note on why this is not Claude-only)
// is handed as `mcpServers`, and the two are deliberately not the same
// list: a server worth giving Claude Code shell/file access is not
// automatically one you want every model tried in this playground calling
// out to, unattended, mid-generation.
//
// Definitions live in the BROWSER's localStorage, never the URL
// (`params.ts`'s `prompt`/`temp`/… — every other piece of stage setup):
// `env`/`headers` can carry secrets (an API key an MCP server needs), and a
// URL is a thing this app already treats as shareable and end up in
// browser history — the one field on this stage that must not ride it.

export interface McpServerConfig {
  name: string;
  type: "stdio" | "http" | "sse";
  /** stdio only. */
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  /** http/sse only. */
  url?: string;
  headers?: Record<string, string>;
}

const STORAGE_KEY = "fused-render:playground:mcp-servers";

/** The configured list, or `[]` on anything that goes wrong reading it — a
 *  private browsing window, cleared site data, a hand-edited value that is
 *  no longer valid JSON. Never throws: a broken stored value must not break
 *  the stage that reads it, only mean "start from nothing". */
export function loadMcpServers(): McpServerConfig[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as McpServerConfig[]) : [];
  } catch {
    return [];
  }
}

/** Best-effort — a full private-browsing store or a blocked one must not
 *  throw out of a settings change; the list simply stays session-only that
 *  time. */
export function saveMcpServers(servers: McpServerConfig[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(servers));
  } catch {
    // see docstring
  }
}

/** What is wrong with a hand-typed server JSON blob, or null. The same
 *  shape `mcp_client.validate_server` (server/ai.py) checks — restated here
 *  so a typo is caught the moment "Add" is clicked, not on the first chat
 *  send, which is a worse place to learn a definition was never usable. */
export function mcpServerProblem(definition: unknown): string | null {
  if (typeof definition !== "object" || definition === null || Array.isArray(definition)) {
    return "must be a JSON object";
  }
  const def = definition as Record<string, unknown>;
  if (def.type === "stdio") {
    if (typeof def.command !== "string" || !def.command.trim()) {
      return "a stdio server needs a 'command' string";
    }
  } else if (def.type === "http" || def.type === "sse") {
    if (typeof def.url !== "string" || !def.url.trim()) {
      return "an http/sse server needs a 'url' string";
    }
  } else {
    return "'type' must be \"stdio\", \"http\" or \"sse\"";
  }
  return null;
}
