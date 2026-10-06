// Which status-bar terminal the user means, for Claude. Two halves:
//
//   * `reportFocusedTerminal` — the drawer tells the server (PUT
//     /api/terminal/focus) which tab is in front, so the MCP `terminal_read`
//     with no id resolves to it. Called when the active tab changes and when
//     the drawer opens; the last tab is kept (not cleared) when the drawer
//     closes, because "the terminal I was just using" is still the one meant.
//   * `terminalHint` — a short METADATA-ONLY JSON string the native chat sends
//     with each message (`terminal_hint`), which agent.py turns into one
//     leading `<terminal-hint>` line. Never terminal contents: Claude reads
//     those itself with `terminal_read`.
//
// No DOM/React here, so it is testable against a stubbed `fetch`.
import { getJson, mutateJson } from "@platform/lib/api";
import { IS_EMBED } from "@platform/lib/router";
import { isWindows } from "@platform/lib/platform";

interface ListedSession {
  id: string;
  alive?: boolean;
  shell?: string;
  cwd?: string | null;
  lastCommand?: string | null;
  lastExit?: number | null;
  lastActivity?: number | null;
  focused?: boolean;
}

let focused: { id: string; label?: string } | null = null;
let lastSent: string | null | undefined;

/** Tell the server (and remember here) which terminal tab is in front. A
 * repeat of the last report is not re-sent; a failed PUT is forgotten so the
 * next change retries. `id === null` means no terminal (drawer has none). */
export function reportFocusedTerminal(id: string | null, label?: string): void {
  focused = id ? { id, label } : null;
  if (IS_EMBED || isWindows) return;
  if (lastSent === id) return;
  lastSent = id;
  mutateJson("PUT", "/api/terminal/focus", { id }).catch(() => {
    if (lastSent === id) lastSent = undefined;
  });
}

/** The metadata object for the focused live terminal, or null when the drawer
 * holds no live terminal (or the list is unreachable). With several live
 * terminals and none known to be focused there is no honest answer, so null. */
export async function terminalHintObject(now: number = Date.now() / 1000): Promise<Record<string, unknown> | null> {
  if (IS_EMBED || isWindows) return null;
  let sessions: ListedSession[];
  try {
    sessions = ((await getJson<{ sessions?: ListedSession[] }>("/api/terminal")).sessions ?? []).filter(
      (s) => s.alive !== false,
    );
  } catch {
    return null;
  }
  if (sessions.length === 0) return null;
  const pick =
    sessions.find((s) => s.id === focused?.id) ??
    sessions.find((s) => s.focused) ??
    (sessions.length === 1 ? sessions[0] : undefined);
  if (!pick) return null;
  const label = pick.id === focused?.id ? focused?.label : undefined;
  return {
    id: pick.id,
    title: label || pick.shell || "terminal",
    ...(pick.cwd ? { cwd: pick.cwd } : {}),
    ...(pick.lastCommand ? { lastCommand: pick.lastCommand } : {}),
    ...(typeof pick.lastExit === "number" ? { lastExit: pick.lastExit } : {}),
    ...(typeof pick.lastActivity === "number" ? { ageSec: Math.max(0, Math.round(now - pick.lastActivity)) } : {}),
  };
}

/** `terminalHintObject` as the string the agent endpoint takes ("" = none). */
export async function terminalHint(): Promise<string> {
  const hint = await terminalHintObject();
  return hint ? JSON.stringify(hint) : "";
}

/** The seeded chat prompt for a drawer tab's "Ask Claude": names the terminal
 * so the model reads it with `terminal_read` rather than guessing. */
export function askClaudeTerminalPrompt(tab: { id: string; label: string; cwd?: string }): string {
  const where = tab.cwd ? ` — ${tab.cwd}` : "";
  return (
    `[terminal ${tab.id}: ${tab.label}${where}]\n` +
    `Look at this terminal (use terminal_read with id "${tab.id}") and tell me ` +
    `what is going on. Do not type into it or change anything yet.`
  );
}

/** Test-only: reset module state between tests. */
export function resetTerminalFocusForTests(): void {
  focused = null;
  lastSent = undefined;
}
