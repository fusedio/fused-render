// The chunked fuzzy scan behind in-folder search.
//
// Kept out of the hook, and pure apart from the effects it is handed, because
// its whole reason to exist is a timing property that a synchronous useMemo
// cannot have: the scan must never occupy the main thread long enough to stop
// the search input echoing keystrokes.
//
// Two things made that a real freeze once the file index started answering:
//   * a covered folder's corpus arrives INSTANTLY and whole (up to MAX_CORPUS
//     = 200k entries), so every keystroke re-scanned the lot — where a
//     streamed walk used to ramp up from nothing, and
//   * useDeferredValue cannot interrupt work already running inside a memo.
//     It buys one cheap render for the echo, then the low-priority render runs
//     the scan straight through.
//
// So the scan is sliced, yields between slices, and is cancellable. A newer
// query cancels an older scan mid-flight instead of queueing behind it.
//
// Every published result carries the query it was computed FOR. The caller
// must not re-tag it at render time — see lib/search-hold for why that is the
// one thing this system has to get right.
import type { WalkEntry } from "@platform/lib/api";
import type { QueryTagged } from "@platform/lib/search-hold";
import type { SearchHit } from "@apps/explorer/listing/types";

export interface ScanJobSpec {
  q: string;
  showHidden: boolean;
  entries: WalkEntry[];
  // Where to resume. Non-zero continues an earlier scan of the SAME
  // q/showHidden/entries — a stream flush appends, it does not restart.
  from: number;
  // Hits already accumulated for `from`. Mutated in place as slices land.
  ranked: SearchHit[];
  sliceSize: number;
  // Scans of at most this many pending entries start immediately; bigger ones
  // wait for the query to go still.
  immediateMax: number;
  debounceMs: number;
  // Minimum gap between INTERMEDIATE publishes. The final one always fires.
  commitMs: number;
}

export interface ScanJobDeps {
  // Slice scorer, sorter and clock, injected so the job's scheduling is
  // testable without a DOM or a real fuzzy scan.
  score: (
    q: string,
    entries: WalkEntry[],
    from: number,
    showHidden: boolean,
    to: number,
  ) => SearchHit[];
  sort: (hits: SearchHit[]) => void;
  now: () => number;
  setTimer: (fn: () => void, ms: number) => number;
  clearTimer: (id: number) => void;
  // Called with a sorted snapshot, tagged with the query it belongs to.
  onPublish: (result: QueryTagged<SearchHit>, done: boolean) => void;
  // Progress, so a cancelled scan can be resumed rather than restarted.
  onProgress: (scored: number) => void;
}

/**
 * Start a scan. Returns a cancel function; calling it stops the job before
 * its next slice and prevents any further publish.
 */
export function startScanJob(spec: ScanJobSpec, deps: ScanJobDeps): () => void {
  const { q, showHidden, entries, ranked, sliceSize } = spec;
  let cancelled = false;
  let timer: number | null = null;
  let scored = spec.from;
  let lastPublish = 0;

  const publish = (done: boolean) => {
    deps.sort(ranked);
    lastPublish = deps.now();
    // A copy, so the array the caller renders is never the one the next slice
    // pushes into — React must see a new identity to re-render, and a live
    // array would also let a later slice mutate rows already on screen.
    deps.onPublish({ q, items: ranked.slice() }, done);
  };

  const step = () => {
    if (cancelled) return;
    timer = null;
    const end = Math.min(scored + sliceSize, entries.length);
    const slice = deps.score(q, entries, scored, showHidden, end);
    for (const h of slice) ranked.push(h); // no spread: a big slice blows the arg limit
    scored = end;
    deps.onProgress(scored);
    if (scored >= entries.length) {
      publish(true);
      return;
    }
    // Intermediate results are throttled to the same budget the rendered
    // re-rank uses: the rows are allowed to move a few times a second, not
    // once per slice.
    if (deps.now() - lastPublish >= spec.commitMs) publish(false);
    timer = deps.setTimer(step, 0);
  };

  const pending = entries.length - scored;
  if (pending <= 0) {
    // Nothing new to score — the caller still needs the result tagged for the
    // current query, so publish what is already ranked.
    publish(true);
    return () => {};
  }
  if (pending <= spec.immediateMax) step();
  else timer = deps.setTimer(step, spec.debounceMs);

  return () => {
    cancelled = true;
    if (timer !== null) deps.clearTimer(timer);
  };
}
