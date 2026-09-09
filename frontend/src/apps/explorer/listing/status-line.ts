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
  hits: number;
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
  hits,
}: StatusLineInput): string {
  // A running or finished search answers a different question than the
  // listing does — how many rows match, not how many are in the folder — so
  // it gets its own line shape and never mixes with `total`/`truncated`.
  if (searching) {
    if (selected > 0) return `${fmt(selected)} of ${fmt(hits)} selected`;
    return `${fmt(hits)} ${hits === 1 ? "match" : "matches"}`;
  }

  if (total === 0 && !truncated) return "Empty folder";

  // A truncated listing never claims to know the true count, so the label
  // carries a "+" rather than a number the walk stopped short of confirming.
  // The existing banner row (Listing.tsx) is where the detail behind that
  // truncation lives; this strip only ever says "at least this many".
  const totalLabel = truncated ? `${fmt(total)}+` : fmt(total);

  if (selected === 0) return `${totalLabel} items`;

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
