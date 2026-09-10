// SPEC-omnibox-search-affordance.md, scope item 4: the completion dropdown
// used to offer only folder names to complete. This is the pure computation
// behind the one NEW row kind it gains — an explicit offer to search,
// surfaced in words instead of left implicit in a banner above the file
// list (the banner this replaces: Listing.tsx's old `enterPrompt`/
// `pathNotFoundMessage` row, see DECISIONS-omnibox-search-affordance.md for
// why the two migrate into one row shape instead of two).
//
// Kept a pure function of the same values the rest of the field already
// reads (`isPathQuery`, `typedAddress`, `searching`) rather than a second,
// parallel notion of "is this a path" — the hard constraint the spec calls
// out by name. SearchField.tsx is the only caller; it turns `action` into
// an indexed row alongside the folder completions and `notice` into a
// plain, non-interactive line above them.
import type { TypedAddress } from "@apps/explorer/listing/useTypedPathAddress";
import { folderToOpen, pathNotFoundMessage } from "@apps/explorer/listing/enter-prompt";
import { basename } from "@platform/lib/format";

export interface SearchActionRow {
  /**
   * The row's full, ready-to-render label. FINDING 2 (code review,
   * 2026-09-10): a `commitInPlace` row's query base can genuinely be a
   * DIFFERENT folder than the one on screen (`gated` below is exactly that
   * case) — "Search this folder for …" is a lie for it, the same lie the
   * banner this row replaced never told (`Press Enter to open <folder> and
   * search`, `enter-prompt.ts`'s own wording, reused rather than invented a
   * second time). Computed here, once, rather than left for the render
   * layer to reconstruct from `query` and `commitInPlace` separately —
   * there is exactly one place that would have to keep matching this
   * predicate's own reasoning about which folder is actually being named.
   */
  label: string;
  /** The text the row's own label quotes, e.g. `Search this folder for
   *  "<query>"`. */
  query: string;
  /**
   * True: pressing the row commits the CURRENT query text as-is
   * (`commitSearch()` — the same call Enter already falls through to for an
   * escaping-but-non-path query, decision 4's gate).
   *
   * False: the current query is path-shaped but does not resolve
   * (`typedAddress.status === "missing"`) — a rank request for it is
   * suppressed by design (`isPathQuery`, useListingSearch.ts) and stays
   * suppressed no matter how many times commitSearch() is called, so
   * "search for it instead" cannot mean committing this same text. Pressing
   * the row instead REWRITES the box to a plain word (this query's own
   * basename, via the same `basename()` PathCrumbs/SearchField already use)
   * and leaves it there — a plain word is never path-shaped, so the box's
   * existing live-search machinery picks it up on its own, with no second
   * commit path grown for this one case.
   */
  commitInPlace: boolean;
}

export interface SearchAffordance {
  /** A non-interactive line shown above `action`, or null. */
  notice: string | null;
  /** The one pressable search offer this render has to show, or null. */
  action: SearchActionRow | null;
}

const NOTHING: SearchAffordance = { notice: null, action: null };

export function searchAffordance(
  query: string,
  isPathQuery: boolean,
  typedAddress: TypedAddress,
  searching: boolean,
  /**
   * SPEC-omnibox-search-affordance.md correction (2026-09-10, defect 1):
   * whether THIS query's base genuinely differs from the folder being
   * searched (`escapesFsPath`, query-base.ts — the SAME predicate
   * useListingSearch.ts's own commit gate reads, not a second one). A
   * non-path query that is NOT gated is already answering live — the rows
   * below are the real search result — so offering to "search this folder"
   * for it would read as "nothing has happened yet" over a box that has
   * already acted. Only a genuinely gated query (needs an explicit commit
   * before anything runs) gets the offer; the path-shaped-but-missing case
   * below is unaffected — it never asks the index at all, gated or not.
   */
  gated: boolean,
  /**
   * The query is empty, or still the untouched path the box pre-filled
   * itself with (`isPristineQuery`, query-pristine.ts) — nothing has been
   * typed to search FOR yet, so neither the offer nor the not-found notice
   * has anything to say. Checked first: a pristine query is also, by
   * construction, one `escapesFsPath` would call gated (it names fsPath
   * itself) and one `isPathQuery` calls path-shaped, so without this check
   * first the missing-path branch below could fire for a folder that very
   * much exists — the one the box is standing in.
   */
  pristine: boolean,
  /**
   * FINDING 3 (code review, 2026-09-10): whether the completion dropdown
   * already has at least one folder match for this same text. The
   * not-found report is a dead end's message (`enter-prompt.ts`'s own
   * comment on why `pathNotFoundMessage` reads as a report, not an
   * instruction) — but with a live completion sitting right there, the
   * query is not a dead end, it's mid-typed, and the report is simply
   * wrong. Deliberately NOT gated behind a commit (Enter): the user has
   * objected twice in this project to being made to press a key for
   * results already implied on screen, and a live completion is exactly
   * that. Suppressing the WHOLE missing-path row (notice and its search
   * offer both) rather than only the notice text: with a real completion
   * already offering to finish the same typing, a bare "search for it
   * instead" floating above it has nothing left to add.
   */
  hasCompletions: boolean,
): SearchAffordance {
  if (pristine) return NOTHING;
  const trimmed = query.trim();
  if (!searching || trimmed === "") return NOTHING;
  if (isPathQuery) {
    if (typedAddress.status !== "missing") return NOTHING;
    if (hasCompletions) return NOTHING;
    const leaf = basename(trimmed);
    return {
      notice: pathNotFoundMessage(query),
      action: { label: `Search this folder for "${leaf}"`, query: leaf, commitInPlace: false },
    };
  }
  if (!gated) return NOTHING;
  // FINDING 2 (code review, 2026-09-10): `gated` here means the query's own
  // base genuinely differs from the folder on screen (`escapesFsPath`,
  // query-base.ts) — name THAT folder, the one Enter is actually about to
  // search, rather than claiming "this folder". `folderToOpen` derives it
  // from the query text alone, the same way the retired banner did; when it
  // has nothing to name (a bare `~`/`/` with nothing after it), fall back to
  // the generic wording rather than printing an empty folder name.
  const folder = folderToOpen(trimmed);
  const label = folder
    ? `Press Enter to open ${folder} and search`
    : `Search this folder for "${trimmed}"`;
  return { notice: null, action: { label, query: trimmed, commitInPlace: true } };
}
