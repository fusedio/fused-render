// Polling for the file index's scan state, shared by the explorer's search
// indicator and the Preferences > Indexing panel.
//
// Polling (not a stream) because a scan is short — seconds over a whole home —
// and the question is coarse: is one running, and does an index exist.
//
// Two rates. While a scan runs the answer changes every few hundred ms, so the
// loop keeps up with it; while nothing is running it drops to a slow
// heartbeat. It does NOT stop: a scan can start after the search box was
// opened (the startup one racing a fast typist, a rebuild triggered from
// Preferences), and a poller that quit on the first idle answer would never
// show the caveat for exactly those cases. Ten seconds of idle polling is one
// request per search session, and only while one is open.
import { useEffect, useState } from "react";
import { indexStatus } from "@platform/lib/api";
import type { IndexStatus } from "@platform/lib/api";

export const INDEX_POLL_MS = 1500;
export const INDEX_IDLE_POLL_MS = 10000;

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
          timer = setTimeout(tick, s.scanning ? INDEX_POLL_MS : INDEX_IDLE_POLL_MS);
        },
        () => {
          // Silent: the index is an accelerator, and a failed status poll is
          // never something the user can act on. Search still works. No retry
          // either — the next heartbeat is the retry.
          if (alive) timer = setTimeout(tick, INDEX_IDLE_POLL_MS);
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
