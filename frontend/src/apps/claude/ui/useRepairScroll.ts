// THE OTHER HALF OF P4-10: the renderer's answer to the repair nonce.
//
// A run that finished while the frame was away appends a whole turn in ONE
// commit — there is no `running` → `idle` edge for the settle-scroll to hang
// off, and the follow-tail rule beside the transcript only pins a reader who is
// already at the bottom. So the reader this case is ABOUT — one who had
// scrolled up and came back to a finished run — saw nothing appear at all. T
// calls `scrollBottom()` there whatever the reader was doing (T:17851), and so
// does this.
//
// A HOOK RATHER THAN SIX LINES INSIDE `ClaudeChat` (batch review, test gap 1):
// the controller half of P4-10 is pinned in `run-controller.pr4.test.ts`, and
// the renderer half had nothing at all — `ui/follow.test.tsx` covers only the
// banner's flag-guarded `followBottom`, which is the OPPOSITE rule. Mounting
// the whole chat to reach an effect this small is a suite nobody would keep, so
// the effect moved out to where it can be driven directly.
import { useEffect, useRef } from "react";

/** The one node this writes to, and it is a raw write BY DESIGN: the transcript
 *  lends a flag-guarded `followBottom` for corrections the reader did not ask
 *  for, and a repair is the case where the reader must be moved regardless. */
export const LOG_SELECTOR = ".chat-logwrap";

/** Just enough of the scrollport for the write; `Element` in the app, a stub in
 *  a suite. */
export interface ScrollPort {
  scrollTop: number;
  readonly scrollHeight: number;
}

export interface RepairScrollRoot {
  querySelector(sel: string): ScrollPort | null;
}

/**
 * Scroll the log to the bottom every time `repaired` moves.
 *
 * Keyed on the nonce and not on a boolean, so two repairs in a row are two
 * scrolls; the mount's own value is skipped, because arriving with a nonce of 3
 * (a remount over a controller that has already repaired three turns) is not a
 * repair this reader watched happen.
 */
export function useRepairScroll(
  repaired: number,
  rootRef: { current: RepairScrollRoot | null },
): void {
  const seen = useRef(repaired);
  useEffect(() => {
    if (seen.current === repaired) return;
    seen.current = repaired;
    const log = rootRef.current?.querySelector(LOG_SELECTOR);
    if (log) log.scrollTop = log.scrollHeight;
    // `rootRef` is declared because the rule asks for it and it costs nothing:
    // a ref object is stable for the life of the mount, so the nonce remains
    // the whole of the trigger.
  }, [repaired, rootRef]);
}
