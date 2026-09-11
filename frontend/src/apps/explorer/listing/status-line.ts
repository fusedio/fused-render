// The one line under the list: how many things are here, and — now that a
// selection drives a move-drag — how many are coming with you. Pure and
// tested for the same reason marquee.ts and drag-drop.ts are: the component
// renders whatever this returns and decides nothing itself.
//
// Every field comes from state Listing.tsx already holds (sortedEntries,
// state.truncated, the selection, the search hit counts), so there is nothing
// new to fetch — only a sum over the selected rows, done once per render.
import { formatSize } from "@platform/lib/format";

export interface StatusLineInput {
  total: number;
  selected: number;
  // Bytes summed over the selected FILES only — see below on why a folder in
  // the selection is never part of this number.
  selectedBytes: number;
  // How many of the selected rows are folders, tracked separately from the
  // byte sum rather than folded into it.
  folderCount: number;
  truncated: boolean;
  searching: boolean;
  // How many rows are actually reachable for selection — the CAPPED count
  // the body renders (Listing.tsx's `visibleHits.length`, result-cap.ts's
  // `capHits`), never the raw match total (`hits.length`). A search can rank
  // thousands of hits while only ~100 rows are on screen; the search box's
  // own pinned chip already owns up to that split ("top 100 of 4.9K" —
  // Listing.tsx's searchCount), so this line's "N of M selected" must not
  // silently substitute the bigger, off-screen number for M.
  visibleHits: number;
}

// en-US thousands separators — "1,000", not "1000" — for the one number here
// that can plausibly get big enough to need them (a folder's total, or a
// truncated cap).
function fmt(n: number): string {
  return n.toLocaleString("en-US");
}

export function statusLine({
  total,
  selected,
  selectedBytes,
  folderCount,
  truncated,
  searching,
  visibleHits,
}: StatusLineInput): string | null {
  // ITEM 11 (running-screen review, 2026-09-10): the match count used to be
  // reported HERE too — "24 matches" — while the search box's own pinned
  // chip already said the same thing (searchCount/searchCountFull,
  // Listing.tsx), plus a caveat and the elapsed time this line never
  // carried in the first place. Division of labour, decided on that
  // review: the BOX owns how many matched and how long it took; the FOOTER
  // owns only what the user has SELECTED, which the box's pin says nothing
  // about. A search with nothing selected now has nothing left for this
  // line to add, so it returns `null` — no line, not an empty one — rather
  // than restate a number already on screen a few pixels up. Selecting
  // rows during a search still has something new to say, so that case
  // survives unchanged.
  if (searching) {
    if (selected > 0) return `${fmt(selected)} of ${fmt(visibleHits)} selected`;
    return null;
  }

  if (total === 0 && !truncated) return "Empty folder";

  // A truncated listing never claims to know the true count, so the label
  // carries a "+" rather than a number the walk stopped short of confirming.
  // The existing banner row (Listing.tsx) is where the detail behind that
  // truncation lives; this strip only ever says "at least this many".
  const totalLabel = truncated ? `${fmt(total)}+` : fmt(total);

  // A truncated total keeps its "+" and stays plural — it is a floor the walk
  // stopped short of confirming, never a count that could actually read "1".
  if (selected === 0) return `${totalLabel} ${total === 1 && !truncated ? "item" : "items"}`;

  // The listing does not know a folder's recursive size and must not stall
  // this line to find out, so a selected folder is counted on its own rather
  // than silently left out of the byte sum (which would read as "0 bytes",
  // not "unknown"). `fileCount` is never carried on the input — it is exactly
  // what is left of the selection once the folders are subtracted out.
  const fileCount = selected - folderCount;
  const parts: string[] = [];
  if (fileCount > 0) parts.push(formatSize(selectedBytes));
  if (folderCount > 0) parts.push(`${fmt(folderCount)} folder${folderCount === 1 ? "" : "s"}`);
  const suffix = parts.length ? ` · ${parts.join(" + ")}` : "";

  return `${fmt(selected)} of ${totalLabel} selected${suffix}`;
}
