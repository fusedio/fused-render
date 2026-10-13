// The empty hero's setup lines (components/SetupLines.tsx), from GET /api/bots/setup.
import type { SetupReply } from "./api";

/** One sentence per missing piece; none while unknown (null reply, or `found: null` = could not tell). */
export function setupIssues(s: SetupReply | null): string[] {
  if (!s) return [];
  const out: string[] = [];
  if (s.chrome?.found === false) out.push("Chrome not found: bots drive Google Chrome, so install it first.");
  if (s.claude?.found === false) out.push("Claude Code not set up: bots think with it.");
  else if (s.claude?.found && s.claude.signed_in === false) out.push("Claude Code not set up: sign in to it first.");
  const o = ownerLine(s);
  if (o) out.push(o.text);
  return out;
}

/** Who runs the bots (one owner process per Mac, bots/registry.py): null when it is this server, which is the
 *  normal case and needs no line. `warn`: nothing runs them (routines will not fire) or the owner has stalled. */
export function ownerLine(s: SetupReply | null): { text: string; warn: boolean } | null {
  if (!s || s.owner === undefined) return null;  // an older server: no owner field, nothing to say
  const o = s.owner;
  if (o === null) return { text: "Nothing is running the bots or their routines: open Fused Render (or Fused Bot).", warn: true };
  if (o.this) return null;
  if (!o.known) return { text: "Another Fused app on this Mac runs the bots; update it so this one can reach them.", warn: true };
  const who = `${o.app} ${o.version}`.trim() || "another Fused app";
  if (o.stalled) return { text: `Bots run in ${who}, which has not ticked for a while; routines may be stuck there.`, warn: true };
  return { text: `Bots and their routines run in ${who}; this window forwards to it.`, warn: false };
}
