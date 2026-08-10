// Polling for the file index's scan state, shared by the explorer's search
// indicator and the Preferences > Indexing panel.
//
// Polling (not a stream) because a scan is short — seconds over a whole home —
// and the question is coarse: is one running, and does an index exist. The
// loop stops as soon as nothing is running, so an idle app makes one request
// per mount, not one every two seconds forever.
import { useEffect, useState } from "react";
import { indexStatus } from "@platform/lib/api";
import type { IndexStatus } from "@platform/lib/api";

export const INDEX_POLL_MS = 1500;

// `active` gates the whole thing: the explorer only asks while its search box
// is in use, so a listing nobody is searching costs nothing.
export function useIndexStatus(active: boolean, nonce = 0): IndexStatus | null {
  const [status, setStatus] = useState<IndexStatus | null>(null);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const ctrl = new AbortController();
    const tick = () => {
      indexStatus(ctrl.signal).then(
        (s) => {
          if (!alive) return;
          setStatus(s);
          // Keep watching only while something is happening. A finished scan
          // leaves the last state on screen and stops the traffic.
          if (s.scanning) timer = setTimeout(tick, INDEX_POLL_MS);
        },
        () => {
          // Silent: the index is an accelerator, and a failed status poll is
          // never something the user can act on. Search still works.
        }
      );
    };
    tick();
    return () => {
      alive = false;
      if (timer !== null) clearTimeout(timer);
      ctrl.abort();
    };
  }, [active, nonce]);
  return status;
}
