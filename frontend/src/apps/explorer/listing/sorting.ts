// Sort resolution and entry sorting for the plain (non-search) listing.
import type { FsEntry } from "@platform/lib/api";
import { getViewState } from "@platform/lib/viewstate";
import { SORT_KEYS, type SortKey, type SortOrder } from "@apps/explorer/listing/types";

// Effective sort for a folder. An explicit `?sort` in the URL wins — a shared
// or hand-typed link is authoritative — otherwise fall back to this folder's
// own saved state (lib/viewstate), otherwise the default name/asc. So each
// folder shows its own remembered order regardless of how it was reached
// (clicked into, a breadcrumb, Back, or a fresh URL), and sibling folders keep
// independent sorts.
export function resolveSort(fsPath: string): { sort: SortKey; order: SortOrder } {
  const url = new URLSearchParams(location.search);
  const src = url.get("sort") ? url : new URLSearchParams(getViewState(fsPath));
  const key = src.get("sort");
  const sort: SortKey = key && key in SORT_KEYS ? (key as SortKey) : "name";
  const order: SortOrder = src.get("order") === "desc" ? "desc" : "asc";
  return { sort, order };
}

export function sortEntries(entries: FsEntry[], sort: SortKey, order: SortOrder): FsEntry[] {
  const flip = order === "desc" ? -1 : 1;
  // Case-insensitive primary order, then an exact (case-sensitive) tiebreak so
  // names differing only by case/accent get a stable, deterministic order.
  // Without the tiebreak such names compare equal and the sort falls back to
  // the arbitrary os.listdir() arrival order, which changes between refreshes.
  const byName = (a: FsEntry, b: FsEntry) => {
    const c = a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    return c !== 0 ? c : a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
  };
  return [...entries].sort((a, b) => {
    const aDot = a.name.startsWith(".");
    const bDot = b.name.startsWith(".");
    if (aDot !== bDot) return aDot ? 1 : -1; // dot entries always group last
    // Name sort is purely alphabetical — folders and files interleave. The
    // size/mtime sorts still group dirs first: a dir has no size and its mtime
    // means something different from a file's, so mixing them there is noise.
    if (sort !== "name" && a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let cmp: number;
    if (sort === "size") cmp = (a.size ?? -1) - (b.size ?? -1);
    else if (sort === "mtime") cmp = (a.mtime ?? 0) - (b.mtime ?? 0);
    else cmp = byName(a, b);
    if (cmp === 0) cmp = byName(a, b);
    return cmp * flip;
  });
}
