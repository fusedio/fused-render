// Whether index-backed search orders hits by relevance score — the
// `ranked_search_enabled` preference (D720, shell/prefs.py), read by the
// explorer's two search boxes (FilesHome.tsx's home search, listing/
// useListingSearch.ts's in-folder search) rather than by the Preferences page:
// neither of those components otherwise touches `Prefs` at all, so this
// follows the same module-level cache + subscribe pattern
// apps/canvases/feature-flag.ts already established for the identical shape
// of problem — a boolean set only on the Preferences page that a component
// elsewhere in the app needs to read on every request it fires, without
// threading a new prop or a new context through the whole explorer tree.
//
// Default ON (ranked) — the OPPOSITE polarity from canvases_enabled's
// default-off: `null`/unread renders as ranked, so a request fired before
// the first GET/api/prefs response lands still asks for the scored order,
// exactly as the server itself defaults `ranked` to true.
import { useEffect, useState } from "react";
import { getPrefs } from "@platform/lib/api";

let enabled: boolean | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean) => void>();

function set(next: boolean) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of listeners) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefs()
    .then((p) => {
      if (generation === departed) set(p.indexing.ranked);
    })
    .catch(() => {
      // A failed read is not an answer: leave `enabled` as it was (defaults
      // to ranked below, via the hook's initial state) and let the next
      // mount try again.
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer — the prefs payload a PUT returned. Called
 * by the Preferences page's toggle, which already has the whole updated
 * `Prefs` in hand. */
export function publishRankedSearchEnabled(next: boolean) {
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Subscribe. Defaults to `true` (ranked) until the first read lands, since
 * that is also the server's own default — a request fired before the GET
 * resolves should ask for the same thing the server would give it anyway. */
export function useRankedSearchEnabled(): boolean {
  const [current, setCurrent] = useState(enabled ?? true);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(enabled ?? true);
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}
