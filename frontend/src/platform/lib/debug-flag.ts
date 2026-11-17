// The shell's one developer DEBUG FLAG, set from the console:
//
//   localStorage.setItem("fused-render.debug", "1")   // then reload
//
// Read by the chat's "what was sent" view (apps/claude/ui/debug-sent.ts) and
// the load timelines' console summary (platform/lib/load-timeline.ts).
export const DEBUG_KEY = "fused-render.debug";

export function debugEnabled(): boolean {
  try {
    return typeof localStorage !== "undefined" && localStorage.getItem(DEBUG_KEY) === "1";
  } catch {
    return false;
  }
}
