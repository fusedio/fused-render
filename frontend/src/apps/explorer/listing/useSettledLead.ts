// The lead row path AS THE PANE SEES IT: the live one while the selection is at
// rest, the previous one while it is moving. The rule and the reason a pane mount
// has to be earned are on `pane-settle.ts`; this is only the clock and the timer
// around it.
//
// It returns the PATH rather than the row so the caller looks the row up in its
// live map: a settled path whose row has since gone (a delete, a refetch that
// dropped it) resolves to `undefined` and the pane falls to its placeholder,
// instead of this hook holding a row object nothing renders any more.
import { useEffect, useRef, useState } from "react";
import { PANE_SETTLE_MS, settleAction } from "@apps/explorer/listing/pane-settle";

export function useSettledLead(lead: string | null, settleMs = PANE_SETTLE_MS): string | null {
  const [settled, setSettled] = useState<string | null>(lead);
  // When the lead last CHANGED — **not when this effect last ran**, which is the
  // distinction the first cut got wrong. The effect also runs on MOUNT, on a
  // `settleMs` change, and twice over under StrictMode's double invocation; none of
  // those is a selection moving, and stamping the clock for them left a fresh
  // Listing looking mid-burst, so the user's first click within 250 ms of opening a
  // folder waited the whole window instead of landing from rest.
  const lastChangeRef = useRef<number>(-Infinity);
  // The lead this effect has already accounted for, so a re-run with the same value
  // can be told from a real move. Seeded to the initial lead, which `settled`
  // already holds.
  const prevLeadRef = useRef<string | null>(lead);
  useEffect(() => {
    const now = Date.now();
    // Stamp the clock ONLY for a real move. A re-run that is not a change must
    // leave it alone, which is what keeps the NEXT real move a move from rest.
    const changed = prevLeadRef.current !== lead;
    const sincePrevMove = now - lastChangeRef.current;
    if (changed) {
      prevLeadRef.current = lead;
      lastChangeRef.current = now;
    }
    // Already showing it — including the commit right after a settle lands, which
    // is what brings this effect back with nothing to do.
    if (settled === lead) return;
    // A move made FROM REST goes straight through. Only a move can qualify: on a
    // bare re-run there is nothing new to mount, so the pending timer below is the
    // right answer even if the clock happens to be cold.
    if (changed && settleAction(sincePrevMove, settleMs) === "mount") {
      setSettled(lead);
      return;
    }
    // Mid-burst: arm for the REMAINDER of the window since the last move, and let
    // the cleanup cancel it. Each further move re-arms, so a held key spends one
    // mount at the end rather than one per row — and because the wait is measured
    // from the last move rather than from this render, a re-run mid-wait resumes
    // it instead of restarting or dropping it.
    const remaining = Math.max(0, settleMs - (now - lastChangeRef.current));
    const timer = setTimeout(() => setSettled(lead), remaining);
    return () => clearTimeout(timer);
  }, [lead, settled, settleMs]);
  return settled;
}
