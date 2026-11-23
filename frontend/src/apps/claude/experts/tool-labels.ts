// The three expert task tools as plain words (docs/experts.md), for
// the tool row and the status line: "Used the Canvas expert", never
// "fused render: expert pull".
const TOOL_RE = /^mcp__fused[_-]render__expert_(pull|note|breakout)$/;
/** The tool whose allowed card opens a breakout with the expert it names. */
export const EXPERT_BREAKOUT_TOOL = "mcp__fused_render__expert_breakout";

export function isExpertTool(name: unknown): boolean {
  return TOOL_RE.test(String(name ?? ""));
}

/** The label for an expert tool call, or null for any other tool. `name` is
 *  the expert the call names (its input's `expert`, or the status
 *  line's detail, which is the same); only pull may name none. */
export function expertToolLabel(tool: unknown, name: unknown, done: boolean): string | null {
  const m = TOOL_RE.exec(String(tool ?? ""));
  if (!m) return null;
  const who = `the ${typeof name === "string" ? name : ""} expert`;
  if (m[1] === "pull" && !name) return done ? "Looked up the experts" : "Looking up the experts";
  if (m[1] === "pull") return done ? `Used ${who}` : `Using ${who}`;
  if (m[1] === "note") return done ? `Sent a note to ${who}` : `Sending a note to ${who}`;
  return `Breakout with ${who}`;
}
