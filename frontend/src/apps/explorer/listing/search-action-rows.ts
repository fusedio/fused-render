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
   * DIFFERENT folder than the one on screen (`awaitingCommit` below,
   * renamed from `gated` by ITEM 9, is exactly that case) — "Search this
   * folder for …" is a lie for it, the same lie the
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
   * ITEM 9 (running-screen review, 2026-09-10): whether this query's commit
   * gate has NOT yet been satisfied — a STATE (`!gateOpen`,
   * useListingSearch.ts), not a property of the query TEXT. This parameter
   * used to be named `gated` and carry `escapesFsPath(query, fsPath, home)`
   * — "this query's base is a different folder from the one being
   * searched" — which is a fact about the text that stays true for as long
   * as the text keeps starting with `~/`, INCLUDING after the commit that
   * satisfies it. That mismatch broke this exact offer row three separate
   * times: offering it over an already-live, ungated search (defect 1,
   * fixed by adding this parameter in the first place); a one-render skew
   * against the deferred value the caller's own gate reads (FINDING 5,
   * fixed by reading `q` instead of `query` — unrelated to this rename, but
   * the same family of bug); and finally, still offering "Press Enter to
   * open ~ and search" after the user had ALREADY pressed Enter and gotten
   * 31 matches — pressing the row at that point would do nothing, because
   * the search it promises had already run. A non-path query that is not
   * awaiting a commit is either already answering live (the rows below ARE
   * the result) or was never gated to begin with — either way, offering to
   * "search this folder" reads as "nothing has happened yet" over a box
   * that has already acted, or has nothing left to do. The path-shaped-but-
   * missing case below is unaffected — it never asks the index at all,
   * awaiting a commit or not.
   */
  awaitingCommit: boolean,
  /**
   * The query is empty, or still the untouched path the box pre-filled
   * itself with (`isPristineQuery`, query-pristine.ts) — nothing has been
   * typed to search FOR yet, so neither the offer nor the not-found notice
   * has anything to say. Checked first: a pristine query is also, by
   * construction, one `escapesFsPath` would call escaping (it names fsPath
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
  if (!awaitingCommit) return NOTHING;
  // FINDING 2 (code review, 2026-09-10): a query reaching here has escaped
  // (that's what put it in `awaitingCommit`'s true branch at all) — its own
  // base genuinely differs from the folder on screen — so name THAT folder,
  // the one Enter is actually about to search, rather than claiming "this
  // folder". `folderToOpen` derives it
  // from the query text alone, the same way the retired banner did; when it
  // has nothing to name (a bare `~`/`/` with nothing after it), fall back to
  // the generic wording rather than printing an empty folder name.
  const folder = folderToOpen(trimmed);
  const label = folder
    ? `Press Enter to open ${folder} and search`
    : `Search this folder for "${trimmed}"`;
  return { notice: null, action: { label, query: trimmed, commitInPlace: true } };
}
