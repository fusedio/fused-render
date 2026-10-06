// The empty hero's setup lines (components/SetupLines.tsx), from GET /api/bots/setup.
import type { SetupReply } from "./api";

/** One sentence per missing piece; none while unknown (null reply, or `found: null` = could not tell). */
export function setupIssues(s: SetupReply | null): string[] {
  if (!s) return [];
  const out: string[] = [];
  if (s.chrome?.found === false) out.push("Chrome not found: bots drive Google Chrome, so install it first.");
  if (s.claude?.found === false) out.push("Claude Code not set up: bots think with it.");
  else if (s.claude?.found && s.claude.signed_in === false) out.push("Claude Code not set up: sign in to it first.");
  return out;
}
