// A query that resolves to exactly the folder already open is the resting
// state written out as text, not a pending search and not an escape from the
// box — even though `escapesBase` (query-base.ts) sees the leading "~" or "/"
// and gates it exactly like any other address that might name somewhere
// else. That gate is right to ask for Enter before a query that COULD move
// the search base; it is wrong to keep treating the query as pending once it
// names the base that is already open.
//
// Resolved with the same pure address resolution `useTypedPathAddress` uses
// to decide what to `stat` (`listingAddress`), not a fresh comparison: fsPath
// is already open, so there is nothing to confirm on the filesystem here,
// only whether the typed text names it. That also means this needs no round
// trip and cannot flash true-then-false while a stat is in flight — it is
// synchronous, unlike `typedAddress`.
//
// `listingAddress` returns null for a glob or a plain filter word, so
// `~/Fused/local/random/*.svg` and `~/Fused/local/random/ico` both resolve to
// something other than `fsPath` (or to null) and stay real queries — this
// only recognizes the exact folder, with or without a trailing slash, in
// either the "~"-contracted or the absolute spelling.
import { listingAddress } from "@apps/explorer/listing/listing-address";

export function queryNamesOpenFolder(
  query: string,
  fsPath: string,
  home: string | undefined,
): boolean {
  const resolved = listingAddress(query, fsPath, home);
  return resolved !== null && resolved === fsPath.replace(/\/+$/, "");
}
