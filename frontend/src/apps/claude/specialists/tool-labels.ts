// THE FIVE SPECIALIST TOOLS AS PRODUCT TEXT (docs/specialists R1): wherever the
// task chat names a tool call — the status line while it runs, the tool chip —
// `mcp__fused_render__specialist_check` reads "Asking the Canvas specialist to
// review the plan…", never "fused render: specialist check".
import { specialistNameNow } from "./useTaskEvents";

const TOOL_RE = /^mcp__fused[_-]render__specialist_(list|pull|check|note|breakout)$/;

export function isSpecialistTool(name: unknown): boolean {
  return TOOL_RE.test(String(name ?? ""));
}

/** The label for a specialist tool call, or null for any other tool.
 *  `key` is what the call names the specialist by (its input's `specialist`,
 *  or the status line's detail): a name, or an id resolved against the list. */
export function specialistToolLabel(
  name: unknown,
  input?: Record<string, unknown> | null,
  opts: { done?: boolean; key?: string } = {},
): string | null {
  const m = TOOL_RE.exec(String(name ?? ""));
  if (!m) return null;
  const raw = typeof input?.specialist === "string" ? input.specialist : opts.key || "";
  const spec = raw ? specialistNameNow(raw) : "";
  const who = spec ? `the ${spec} specialist` : "a specialist";
  const done = !!opts.done;
  switch (m[1]) {
    case "list":
      return done ? "Listed specialists" : "Listing specialists…";
    case "pull":
      return done ? `Got guidance from ${who}` : `Getting guidance from ${who}…`;
    case "check": {
      const what = input?.stage === "done" ? "the result" : "the plan";
      return done ? `Asked ${who} to review ${what}` : `Asking ${who} to review ${what}…`;
    }
    case "note":
      if (!raw) return done ? "Sent a note to the general inbox" : "Sending a note to the general inbox…";
      return done ? `Sent a note to ${who}` : `Sending a note to ${who}…`;
    case "breakout":
      return done ? `Offered a breakout to ${who}` : `Offering a breakout to ${who}…`;
  }
  return null;
}
