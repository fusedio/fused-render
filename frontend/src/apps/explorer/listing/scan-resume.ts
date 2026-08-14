// When a partially-scored corpus may be picked up where it was left, instead of
// being re-scored from zero.
//
// The incremental score cache in listing/useRankedScan holds `ranked` (hits for
// the first `scored` entries) plus the query and hidden-intent they were scored
// under. Resuming is only sound when the entries those hits came from are still
// the entries the job is about to continue over — otherwise the cache carries
// hits for rows that no longer exist, which shows a deleted file as a match.
//
// The original rule was array IDENTITY, which is exactly right for a streaming
// walk (batches append in place) and useless for anything else: a REFETCH is
// always a new array, so the whole corpus was re-scored every time the fetch
// re-ran — including for the refetches that cannot change the corpus at all (an
// in-app rename bumps the home page's fetch key; a retry after an error re-runs
// it). On a 200k-entry home corpus that is the difference between a keystroke
// and a stall.
//
// So identity is joined by a KEY: a caller-supplied name for the corpus's
// content (WalkState.key / CorpusState.key — a (root, index generation) pair,
// or a folder plus its walk generation). Same key means same rows in the same
// order. The empty string is "no identity" and never resumes, so a caller that
// cannot make that promise simply does not make it.
import type { WalkEntry } from "@platform/lib/api";

export interface ScanCache {
  q: string;
  showHidden: boolean;
  entries: WalkEntry[] | null;
  key: string;
  /** How many of `entries` have been scored into `ranked`. */
  scored: number;
}

export interface ScanTarget {
  q: string;
  showHidden: boolean;
  entries: WalkEntry[];
  key: string;
}

export function canResumeScan(cache: ScanCache, next: ScanTarget): boolean {
  if (cache.q !== next.q || cache.showHidden !== next.showHidden) return false;
  // The streaming case: one array, appended in place.
  if (cache.entries === next.entries) return true;
  if (next.key === "" || cache.key !== next.key) return false;
  // Same corpus, new array — but only past what was already scored. A retry
  // rebuilds the array from empty and grows it again, and resuming at a mark
  // beyond its current end would skip rows entirely.
  return next.entries.length >= cache.scored;
}
