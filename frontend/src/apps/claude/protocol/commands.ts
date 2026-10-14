// SLASH COMMANDS (SPEC C-1/C-3). Pure TS, no React.
//
// A message whose first non-space character is `/` followed by a command token
// `[A-Za-z0-9:_-]+` is a slash command, and it is one of three kinds:
//
//   * LOCAL — the chat UI answers it and nothing is sent (`LOCAL_COMMANDS`);
//   * CLI — a name the folder's list knows: sent verbatim, the CLI expands it;
//   * UNKNOWN — any other name: STILL sent verbatim (the CLI says there is no
//     such command; we do not second-guess it), the menu just shows no match.
//
// The list itself comes from the agent's `commands` action — the last
// session's `init` row when one was captured, a disk scan otherwise.
import { runAgent, type RunOpts } from "./agent";
import type { CommandRow, CommandsResponse } from "./types";

export type CommandKind = "local" | "cli" | "unknown";

export interface ClassifiedCommand {
  kind: CommandKind;
  /** The token without its slash, as typed. */
  name: string;
  /** Everything after the token, trimmed. "" when nothing follows. */
  arg: string;
}

export interface LocalCommand {
  name: string;
  description: string;
  /** The argument, as the menu prints it after the name ("" = none). */
  hint: string;
}

/** The commands this page answers itself, in the order the menu lists them. */
export const LOCAL_COMMANDS: readonly LocalCommand[] = [
  { name: "clear", description: "Start a new conversation", hint: "" },
  { name: "help", description: "List the available commands", hint: "" },
  { name: "model", description: "Set the model for the next turn", hint: "<name>" },
  { name: "effort", description: "Set the effort for the next turn", hint: "<low|medium|high|max>" },
  { name: "worktree", description: "Switch to or create a git worktree", hint: "[name]" },
  { name: "tasks", description: "Show background tasks", hint: "" },
];

const LOCAL_NAMES = new Set(LOCAL_COMMANDS.map((c) => c.name));

/** `/token` then whitespace or the end — so a path like `/Users/me` is prose. */
const COMMAND_RE = /^\s*\/([A-Za-z0-9:_-]+)(?=\s|$)([\s\S]*)$/;

/**
 * What a composed message is, or null when it is not a slash command at all.
 * `known` is the folder's command list (names, no slash); a local name is local
 * whatever the list says.
 */
export function classifyCommand(
  text: string,
  known: Iterable<string> | ReadonlySet<string> = [],
): ClassifiedCommand | null {
  const m = COMMAND_RE.exec(text);
  if (!m) return null;
  const name = m[1];
  const arg = (m[2] ?? "").trim();
  if (LOCAL_NAMES.has(name)) return { kind: "local", name, arg };
  const set = known instanceof Set ? known : new Set(known);
  return { kind: set.has(name) ? "cli" : "unknown", name, arg };
}

/** One row of the command menu. */
export interface MenuCommand {
  name: string;
  description: string;
  hint: string;
  local: boolean;
}

/**
 * The menu's list: the local commands first, then the folder's own in the order
 * the agent gave them. A name the page answers itself is never listed twice —
 * the agent's fixed table carries the local names too.
 */
export function menuCommands(fetched: readonly CommandRow[] | null | undefined): MenuCommand[] {
  const out: MenuCommand[] = LOCAL_COMMANDS.map((c) => ({ ...c, local: true }));
  const seen = new Set(LOCAL_NAMES);
  for (const row of fetched ?? []) {
    const name = String(row?.name ?? "").replace(/^\//, "");
    if (!name || seen.has(name)) continue;
    seen.add(name);
    out.push({ name, description: String(row.description ?? ""), hint: "", local: false });
  }
  return out;
}

/**
 * Narrow a list by what was typed after the slash: names that START with the
 * query first, then names that merely contain it, each group in the list's own
 * order. Case-insensitive; an empty query is the whole list.
 */
export function filterCommands<T extends { name: string }>(list: readonly T[], query: string): T[] {
  const q = query.replace(/^\//, "").toLowerCase();
  if (!q) return list.slice();
  const prefix: T[] = [];
  const inner: T[] = [];
  for (const row of list) {
    const n = row.name.toLowerCase();
    if (n.startsWith(q)) prefix.push(row);
    else if (n.includes(q)) inner.push(row);
  }
  return prefix.concat(inner);
}

/** The folder's command list (`commands {file}`). Throws on a handler error. */
export async function fetchCommands(file: string, opts: RunOpts = {}): Promise<CommandsResponse> {
  const out = await runAgent("commands", { file }, opts);
  if (!out || ("error" in out && out.error)) {
    throw new Error(("error" in (out ?? {}) && (out as { error: string }).error) || "no command list");
  }
  const res = out as CommandsResponse;
  return {
    commands: Array.isArray(res.commands) ? res.commands : [],
    agents: Array.isArray(res.agents) ? res.agents : [],
    stale: !!res.stale,
    from: res.from === "cli" ? "cli" : "disk",
  };
}

/** A sent message split for the transcript's chip: whatever preceded the
 *  slash, the `/token` itself, and the rest verbatim — so the three pieces join
 *  back into exactly the text (the bubble's text is what the re-attach probe
 *  reads). Null when the message is not a slash command. */
export function splitCommand(text: string): { lead: string; token: string; rest: string } | null {
  const m = /^(\s*)(\/[A-Za-z0-9:_-]+)(?=\s|$)([\s\S]*)$/.exec(text);
  return m ? { lead: m[1], token: m[2], rest: m[3] } : null;
}
