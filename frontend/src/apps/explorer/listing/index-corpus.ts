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

// The index answers only when it has visited THIS folder and is recent enough
// (both flags come from the server, which owns the freshness threshold — one
// definition, not one per caller). A truncated corpus is still used: it is the
// same cap the walk hits, and the walk flags it the same way.
export function indexCorpusFrom(res: IndexSearchResult | null | undefined): IndexCorpus | null {
  if (!res || !res.covered || !res.fresh) return null;
  if (!Array.isArray(res.entries)) return null;
  return { entries: res.entries, truncated: !!res.truncated };
}
