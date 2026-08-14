// WHEN a moved selection reaches the preview pane — the pane's half of "settle
// before acting on the lead", which the `?sel=` URL write already does with its
// own debounce (SEL_URL_DELAY_MS in useListingSelection).
//
// **A PANE MOUNT IS NOT FREE, and for one side it is not even cheap.** The pane is
// keyed on what it is about (pane-side's paneKey), so every change of the lead
// tears its iframe down and loads a new one. For the `claude` side that iframe
// runs `agent.py` through `/api/run` before it can draw anything —
// `templates/claude/template.html` asks for its model defaults on load,
// unconditionally — so one keystroke is one subprocess spawn. Holding ↓ through a
// folder of forty subdirectories was forty of them, and `/api/run` spawning is
// the path with this repo's fork/PROJ-atfork crash history, so a burst per
// keystroke is not something to leave running.
//
// That cost used to be opt-in: a folder row previewed as its embedded listing,
// which is a shell component and runs nothing, and the chat only mounted if the
// user asked for it. Making `claude` a folder's default (D279/D280) is what turned
// arrow-key browsing into subprocess spawning, so the debounce ships with it
// rather than after it.
//
// Deliberately NOT a throttle, and not a plain trailing debounce either:
//
//   from rest    -> MOUNT NOW. A click, or the first press of a held key, is a
//                   destination. Delaying it would be latency the user feels for
//                   no saving — nothing is pending to coalesce with.
//   mid-burst    -> WAIT. Each further move re-arms, so the rows passed THROUGH
//                   cost nothing and only the row the user stops on is mounted.
//
// The rule is pure and the clock is the caller's (useSettledLead), so the
// behaviour can be pinned without a fake timer.
export const PANE_SETTLE_MS = 250;

export type SettleAction = "mount" | "wait";

// `msSinceLastChange` is the gap between this move and the previous one —
// `Infinity` when there was no previous one. A gap AT the window counts as rest:
// the window is how long a burst has to be quiet before it is over, so its own
// boundary is the quiet side.
export function settleAction(
  msSinceLastChange: number,
  settleMs: number = PANE_SETTLE_MS,
): SettleAction {
  return msSinceLastChange >= settleMs ? "mount" : "wait";
}
