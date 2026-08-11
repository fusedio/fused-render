// The one place a corpus gets fuzzy-ranked against a query.
//
// Both search boxes in this app do the same thing to a flat entry list: score
// it, sort it by the shared comparator, and publish the result TAGGED with the
// query it was computed for. That was written out twice — the in-folder search
// (useWalkSearch) and the home page (FilesHome) — and the subtleties that make
// it correct are exactly the kind that rot in a copy. The home page's copy had
// already dropped `onProgress`, so a scan cancelled mid-flight started over
// instead of resuming.
//
// The load-bearing parts, once:
//
//  * an EFFECT, not a memo. A full scan of an index-backed corpus (200k
//    entries) is far too much work for a keystroke, and a memo cannot be
//    interrupted once it starts. scan-job slices it, yields between slices, and
//    is cancelled the moment the query, hidden-intent or corpus moves.
//  * the query TAG is committed WITH the data. On the first render after `q`
//    changes, this state still holds the previous query's rows, so anything
//    that tags it at render time labels the old query's rows with the new
//    query. See lib/search-hold.
//  * `done` distinguishes "this is the whole answer for q" from "this is what
//    the scan has found so far". Without it an intermediate slice read as a
//    settled result and a zero-hit moment mid-scan rendered a confident "no
//    matches".
//  * incremental resume. As long as query, hidden-intent and the entries ARRAY
//    are unchanged, only entries appended since the last progress mark get
//    scored, so a stream flush near the tail of a big walk costs one small scan
//    rather than a re-scan of everything.
import { useEffect, useMemo, useRef, useState } from "react";
import type { WalkEntry } from "@platform/lib/api";
import type { QueryTagged } from "@platform/lib/search-hold";
import { startScanJob } from "@apps/explorer/listing/scan-job";
import { queryWantsHidden, rankCompare, scoreEntries } from "@apps/explorer/listing/search";
import {
  RERANK_COMMIT_MS,
  SCAN_IMMEDIATE_MAX,
  SCAN_SLICE,
  type SearchHit,
} from "@apps/explorer/listing/types";

/**
 * Rank `entries` against `q`, incrementally and interruptibly.
 *
 * `entries` is null when there is nothing to scan (no corpus yet, or no query),
 * which publishes a settled empty answer for `q` rather than leaving the
 * previous query's rows tagged as current.
 *
 * `debounceMs` is how still the query must be before a scan starts — the two
 * callers debounce differently (a streamed walk arrives in flushes, the home
 * page's corpus arrives once), so it is a parameter rather than a constant.
 *
 * Stream flushes reuse the same entries array (it is appended in place), so the
 * array identity cannot tell the job that more arrived — its length can, which
 * is why the length is a dep of its own.
 */
export function useRankedScan(entries: WalkEntry[] | null, q: string, debounceMs: number) {
  const showHidden = queryWantsHidden(q);
  const [scanned, setScanned] = useState<QueryTagged<SearchHit> & { done: boolean }>(() => ({
    q: "",
    items: [],
    done: true,
  }));

  // Incremental-scoring cache, which also carries a chunked scan's PROGRESS so
  // a job cancelled mid-flight (by the next stream flush) resumes.
  const scoreCache = useRef<{
    q: string;
    showHidden: boolean;
    entries: WalkEntry[] | null;
    scored: number; // how many of `entries` have been scored already
    ranked: SearchHit[];
  }>({ q: "", showHidden: false, entries: null, scored: 0, ranked: [] });

  const scannable = q === "" ? null : entries;
  const entryCount = scannable ? scannable.length : 0;

  useEffect(() => {
    if (scannable === null) {
      // Nothing to scan, and no scan is in flight, so this IS the final answer
      // for q — what is outstanding (a walk, a corpus fetch) is the caller's to
      // report on. Kept identity-stable so it is not a render loop.
      setScanned((prev) =>
        prev.q === q && prev.items.length === 0 && prev.done
          ? prev
          : { q, items: [], done: true },
      );
      return;
    }
    const cache = scoreCache.current;
    const resumable =
      cache.entries === scannable && cache.q === q && cache.showHidden === showHidden;
    if (!resumable) {
      scoreCache.current = { q, showHidden, entries: scannable, scored: 0, ranked: [] };
    }
    const live = scoreCache.current;
    return startScanJob(
      {
        q,
        showHidden,
        entries: scannable,
        from: live.scored,
        ranked: live.ranked,
        sliceSize: SCAN_SLICE,
        immediateMax: SCAN_IMMEDIATE_MAX,
        debounceMs,
        commitMs: RERANK_COMMIT_MS,
      },
      {
        score: scoreEntries,
        sort: (hitsToSort) => hitsToSort.sort(rankCompare),
        now: Date.now,
        setTimer: (fn, ms) => window.setTimeout(fn, ms),
        clearTimer: (id) => window.clearTimeout(id),
        onPublish: (result, done) => setScanned({ ...result, done }),
        onProgress: (n) => {
          live.scored = n;
        },
      },
    );
  }, [q, showHidden, scannable, entryCount, debounceMs]);

  // Rows are dropped unless they were computed for the CURRENT query, so a scan
  // still catching up never shows the previous query's matches — the same rule
  // search-hold enforces downstream, applied at the source.
  const ranked = useMemo(() => (scanned.q === q ? scanned.items : []), [scanned, q]);
  // True until a FINAL publish for the current query lands. Cleared by the tag
  // alone, an intermediate slice would end the "Searching…" state early.
  const pending = scanned.q !== q || !scanned.done;

  return { scanned, ranked, pending };
}
