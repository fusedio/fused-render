// The file index's scan state, shared by the explorer's search indicator and
// the Preferences > Indexing panel — a subscription to the `index.status`
// topic (platform/lib/events), whose snapshot is exactly what
// `GET /api/index/status` answered.
//
// The question is coarse — is a scan running, and does an index exist — and
// the server pushes a fresh snapshot whenever its answer moves: fast while a
// scan runs (the counts change every few hundred ms), and only on a change
// while nothing is. It never stops following while `active`: a scan can start
// after the search box was opened (the startup one racing a fast typist, a
// rebuild triggered from Preferences), and a reader that let go on the first
// idle answer would never show the caveat for exactly those cases. N readers
// of this hook hold ONE server subscription (the client refcounts by topic).
import { useEffect, useRef, useState } from "react";
import type { IndexStatus } from "@platform/lib/api";
import { resyncTopic, subscribeTopic } from "@platform/lib/events";
import { noteIndexLifecycle } from "@platform/lib/index-freshness";

/** The topic this hook follows; `useListingSearch` re-asks its rank on it. */
export const INDEX_STATUS_TOPIC = "index.status";

// A completed scan means every corpus fetched before it is a generation
// behind, and nothing else says so — the filesystem didn't change, so no
// dir-watch refresh arrives. This hook is the one place completion is
// observed. Module-level so concurrent readers dedupe: the first to see the
// new completion stamp signals, the rest see an unchanged value.
let lastCompleted: number | null | undefined;
function noteScanProgress(s: IndexStatus): void {
  const done = s.last_completed_at ?? null;
  if (lastCompleted !== undefined && done !== null && done !== lastCompleted) {
    noteIndexLifecycle();
  }
  lastCompleted = done;
}

// `active` gates the whole thing: the explorer only follows while its search
// box is in use, so a listing nobody is searching costs nothing. `nonce` is
// the caller's "a scan was just asked for" — an EVENT that resyncs the
// subscription so the first scanning snapshot is not a server tick away.
export function useIndexStatus(active: boolean, nonce = 0): IndexStatus | null {
  const [status, setStatus] = useState<IndexStatus | null>(null);
  useEffect(() => {
    if (!active) return;
    return subscribeTopic<IndexStatus>(INDEX_STATUS_TOPIC, null, (snap) => {
      // Silent on an `err` frame: the index is an accelerator, and a failed
      // status read is never something the user can act on. Search still
      // works, and the next push is the retry.
      if (!snap) return;
      noteScanProgress(snap);
      setStatus(snap);
    });
  }, [active]);
  // The subscribe above already answers with a snapshot, so only a nonce that
  // MOVED after mount is news worth a resync.
  const seenNonce = useRef(nonce);
  useEffect(() => {
    if (seenNonce.current === nonce) return;
    seenNonce.current = nonce;
    if (active) resyncTopic(INDEX_STATUS_TOPIC);
  }, [active, nonce]);
  return status;
}
