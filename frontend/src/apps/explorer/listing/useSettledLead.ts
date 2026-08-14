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
  // When the lead last CHANGED — not when this effect last ran. Seeded to
  // -Infinity so the first change is a move from rest and mounts at once.
  const lastChangeRef = useRef<number>(-Infinity);
  useEffect(() => {
    const now = Date.now();
    const action = settleAction(now - lastChangeRef.current, settleMs);
    lastChangeRef.current = now;
    if (action === "mount") {
      setSettled(lead);
      return;
    }
    // Mid-burst: re-arm. The cleanup is what makes each further move cancel the
    // previous row's pending mount, so a held key spends one mount at the end
    // rather than one per row.
    const timer = setTimeout(() => setSettled(lead), settleMs);
    return () => clearTimeout(timer);
  }, [lead, settleMs]);
  return settled;
}
