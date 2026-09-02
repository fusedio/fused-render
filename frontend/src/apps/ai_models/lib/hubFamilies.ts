// Collapsing a Hub search's quant/finetune republishes back to one row per
// model family — a pure grouping rule, tested directly here for the reason
// `hubSearchView.ts` already is: the rule is testable and the JSX that draws
// its output is not.
//
// **The signal is the Hub's `base_model:<relation>:<id>` tag** (parsed
// server-side, `hub_models.py::_base_model`), and the module accepts every
// relation it names — `quantized`, `finetune`, `merge`, `adapter` — rather
// than narrowing to `quantized` alone. In the wild sample that motivated this
// build, `quantized` dominates, but the MLX ports actually on this machine
// (`gemma-3-12b-it-4bit`, `ltx-2.3-mlx-q4`) mostly declare `finetune`, and
// keying on one relation would split exactly the families this exists to
// join.
//
// **A row keeps its identity even when its base model never appeared in the
// same page of results** — dropped upstream by D313, or simply outside the
// query's match. The variant still names what it came from, and grouping it
// under that id (rather than its own) is what lets a second variant of the
// SAME base, arriving on a later page or a different sort, land in the same
// family rather than starting a new one.
import type { HubModel } from "@platform/lib/api";
import type { ResultSort } from "./hubSearchView";

export interface HubFamily {
  /** `baseModel` when known, else the lone member's own id — stable across
   *  re-renders of the SAME result set, which is what a React `key` needs. */
  key: string;
  /** The member that ranks first under the ACTIVE sort (see `groupIntoFamilies`'s
   *  `sort` parameter) — the row a family's single line draws. */
  primary: HubModel;
  /** Every other member, same ordering rule, for the "N variants" affordance. */
  variants: HubModel[];
}

/** Descending: higher fit score first (a model with no fit — nothing to
 *  judge — sorts behind one that has any real score, including "no"'s 0),
 *  then higher downloads, then leaves ties exactly where they were.
 *
 *  `Array.prototype.sort` has been a STABLE sort in every engine this app
 *  ships on for years (the ECMA-262 requirement since ES2019), so a
 *  comparator that returns 0 for a tie is enough to keep the server's own
 *  ranking as the last word — no index bookkeeping needed here. */
function byFitThenDownloads(a: HubModel, b: HubModel): number {
  const fitA = a.fit?.score ?? -1;
  const fitB = b.fit?.score ?? -1;
  if (fitA !== fitB) return fitB - fitA;
  const dlA = a.downloads ?? -1;
  const dlB = b.downloads ?? -1;
  return dlB - dlA;
}

/** Descending: higher composite `matchScore` first, then higher downloads,
 *  same stability guarantee as `byFitThenDownloads`. `matchScore` (D639) is
 *  attached to every row regardless of which sort was requested, so this is
 *  the general-purpose comparator for every sort except "fit" itself, which
 *  asks specifically for the memory-only judgement `byFitThenDownloads`
 *  gives. */
function byMatchThenDownloads(a: HubModel, b: HubModel): number {
  const matchA = a.matchScore ?? -1;
  const matchB = b.matchScore ?? -1;
  if (matchA !== matchB) return matchB - matchA;
  const dlA = a.downloads ?? -1;
  const dlB = b.downloads ?? -1;
  return dlB - dlA;
}

/** Which comparator decides a family's primary, for a given active sort
 *  (code review finding: the primary used to be picked by `fit` alone, no
 *  matter what the page was actually sorted by — under `sort=best` this drew
 *  the family's best-FITTING member while every visible row's Match number
 *  came from the family's best-MATCHING one, so the Match column stopped
 *  descending down the table the moment the two disagreed). `"fit"` keeps
 *  the memory-only comparator, matching what that sort itself ranks by;
 *  every other sort (including the "best" default, and page-level sorts
 *  like "size" that have nothing of their own to say about which variant is
 *  the "right" one) uses the composite `matchScore`, since it is the one
 *  ranking figure every row carries no matter how the page is sorted. */
function primaryComparator(sort: ResultSort): (a: HubModel, b: HubModel) => number {
  return sort === "fit" ? byFitThenDownloads : byMatchThenDownloads;
}

/** Every result, collapsed to one family per model — primary chosen by
 *  `primaryComparator(sort)`, variants ordered the same way, an untagged row
 *  standing alone. `sort` defaults to `"fit"` for a caller with no sort
 *  context of its own (every existing test predates `sort=best` and assumes
 *  this).
 *
 *  **Families are positioned at their PRIMARY's index in `models`, not at
 *  whichever member first appeared.** A family draws its primary's row —
 *  size, downloads, age, everything a column shows comes off that one
 *  member — so that is also the member whose position in an already-sorted
 *  `models` (`bySizeAscending`, the Hub's own `downloads`/`trending` order,
 *  a `sort=fit`/`sort=best` reorder) has to decide where the family lands.
 *  Positioning by first-appearance instead let a family sit at a NON-primary
 *  variant's index while showing the primary's value there — a sort-visible
 *  column (Size, Match) visibly not ascending/descending, because the row
 *  drawn at that position could be showing a different member's value.
 *  Ordering by the primary's own index instead makes the family order
 *  exactly as sorted as `models` itself was, for the SAME key
 *  `primaryComparator(sort)` used to choose that primary — which is what
 *  keeps the two decisions from disagreeing the way they did before this
 *  fix, rather than a guarantee that holds for every key regardless of
 *  which one the primary was actually chosen by.
 */
export function groupIntoFamilies(
  models: readonly HubModel[],
  sort: ResultSort = "fit",
): HubFamily[] {
  const buckets = new Map<string, HubModel[]>();
  const indexOf = new Map<HubModel, number>();

  models.forEach((model, i) => {
    indexOf.set(model, i);
    const key = model.baseModel ?? model.id;
    let bucket = buckets.get(key);
    if (!bucket) {
      bucket = [];
      buckets.set(key, bucket);
    }
    bucket.push(model);
  });

  const compare = primaryComparator(sort);
  const families = Array.from(buckets.entries(), ([key, group]) => {
    const sorted = group.slice().sort(compare);
    return { key, primary: sorted[0], variants: sorted.slice(1) };
  });
  families.sort((a, b) => indexOf.get(a.primary)! - indexOf.get(b.primary)!);
  return families;
}
