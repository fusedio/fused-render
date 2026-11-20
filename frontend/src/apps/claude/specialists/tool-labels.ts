// The three specialist task tools as plain words (docs/specialists.md), for
// the tool row and the status line: "Used the Canvas specialist", never
// "fused render: specialist pull".
const TOOL_RE = /^mcp__fused[_-]render__specialist_(pull|note|breakout)$/;
/** The tool whose allowed card opens a breakout with the specialist it names. */
export const SPECIALIST_BREAKOUT_TOOL = "mcp__fused_render__specialist_breakout";

export function isSpecialistTool(name: unknown): boolean {
  return TOOL_RE.test(String(name ?? ""));
}

/** The label for a specialist tool call, or null for any other tool. `name` is
 *  the specialist the call names (its input's `specialist`, or the status
 *  line's detail, which is the same); only pull may name none. */
export function specialistToolLabel(tool: unknown, name: unknown, done: boolean): string | null {
  const m = TOOL_RE.exec(String(tool ?? ""));
  if (!m) return null;
  const who = `the ${typeof name === "string" ? name : ""} specialist`;
  if (m[1] === "pull" && !name) return done ? "Looked up the specialists" : "Looking up the specialists";
  if (m[1] === "pull") return done ? `Used ${who}` : `Using ${who}`;
  if (m[1] === "note") return done ? `Sent a note to ${who}` : `Sending a note to ${who}`;
  return `Breakout with ${who}`;
}
