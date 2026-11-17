// THE "what was sent" PANEL IS A DEBUG VIEW (Akshil, 2026-09-20).
//
// `SentPop` shows the exact wire the agent received — `<pane-shot>`,
// `<annotations>`, the JSON behind them. That is machinery a reader never
// typed and never needs; showing it under a "Click to see exactly what was
// sent" door read as the app leaking its own plumbing. So the door is only
// hung when a developer asks for it, from the console:
//
//   localStorage.setItem("fused-render.debug", "1")   // then reload
//
// Nothing else changes: an attachment's receipt row still opens the picture
// viewer (`Receipts` falls back to `onOpenShot` when there is no `onShowSent`),
// and the wire itself is still recorded on every turn (`UserTurn.raw`).
export const DEBUG_KEY = "fused-render.debug";

export function debugSentEnabled(): boolean {
  try {
    return typeof localStorage !== "undefined" && localStorage.getItem(DEBUG_KEY) === "1";
  } catch {
    return false;
  }
}
