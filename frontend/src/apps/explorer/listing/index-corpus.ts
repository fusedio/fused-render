// Whether the in-folder search can use the index's corpus instead of walking
// the tree live.
//
// Kept pure and separate from the hook because it is the one decision that can
// silently degrade search: say yes on a stale or partial index and the box
// answers confidently with files that moved an hour ago. Every uncertainty
// therefore resolves to "walk" — a slower correct answer, which is exactly
// what the explorer did before the index existed.
import type { IndexSearchResult, WalkEntry } from "@platform/lib/api";

export interface IndexCorpus {
  entries: WalkEntry[];
  truncated: boolean;
}

// Coverage — "the scan visited THIS folder" — is the whole gate. Age is not:
// the index is rescanned at every startup, a rescan keeps serving its last
// completed generation while it runs, and the search box says "indexing…"
// while that is happening (useIndexStatus). An instant, mostly-right answer
// with a visible caveat beats re-walking the tree; a folder the index never
// reached has no answer at all, and that is what falls back to the walk.
//
// A truncated corpus is still used: it is the same cap the walk hits, and the
// walk flags it the same way.
export function indexCorpusFrom(res: IndexSearchResult | null | undefined): IndexCorpus | null {
  if (!res || !res.covered) return null;
  if (!Array.isArray(res.entries)) return null;
  return { entries: res.entries, truncated: !!res.truncated };
}
