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

// **A GGUF republish is its own family, not a variant of the safetensors
// one** (D676). Keying on `baseModel` alone puts
// `leejet/FLUX.2-klein-4B-GGUF` and
// `Disty0/FLUX.2-klein-4B-SDNQ-4bit-dynamic` in one bucket, where the GGUF
// row cannot win the primary contest: a GGUF row's `matchScore` is blended
// from three missing-evidence constants (no `fit`, no `estimatedSize`, no
// `speedEstimate`; see `hub_models.py::_model_row`), so it cannot outrank a
// sibling carrying real numbers no matter how good it is. That parks it
// permanently behind the variants expander, which for the one format a
// llama.cpp/stable-diffusion.cpp user is specifically searching for is the
// same as not being in the results.
//
// The deeper reason is that the two are not the same download. Every other
// relation this module joins — a finetune, a merge, a 4-bit safetensors
// quant — produces a repo the SAME runner opens the same way, so showing one
// and offering the rest behind a disclosure is honest. A format switch is
// not that: it decides which runner can open the repo at all. So `format`
// (the server's own field, read off the Hub) joins `baseModel` in the key.
export interface HubFamily {
  /** `baseModel` + `format` when a base model is known, else the lone
   *  member's own id — stable across re-renders of the SAME result set,
   *  which is what a React `key` needs.
   *
   *  Opaque: a composite whose parts are joined by a separator no Hub repo
   *  id contains. Nothing reads it back apart as an id, and nothing should. */
  key: string;
  /** The member that ranks first under the ACTIVE sort (see `groupIntoFamilies`'s
   *  `sort` parameter) — the row a family's single line draws. */
  primary: HubModel;
  /** Every other member, same ordering rule, for the "N variants" affordance. */
  variants: HubModel[];
  /** The base model this family is keyed on — the repo every member declares
   *  as its origin — or null for a family that is just one untagged row
   *  standing alone under its own id. NOT necessarily a repo present in
   *  `models`: see the module header. */
  baseModel: string | null;
  /** The member that IS the base model (`member.id === baseModel`), when the
   *  base repo itself is among the results, else null. */
  base: HubModel | null;
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
 *  same stability guarantee as `byFitThenDownloads`. `matchScore` (D663) is
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

/** Which comparator ranks a family's members, for a given active sort. It has
 *  to follow the sort the page is actually under: a comparator that ranked by
 *  `fit` while the page ranked by the composite would hand the disclosure a
 *  different "best" than the Match number printed beside it, and the Match
 *  column would stop descending down the table wherever the two disagreed.
 *  `"fit"` therefore keeps the memory-only comparator, matching what that
 *  sort itself ranks by; every other sort (including the "best" default, and
 *  page-level sorts like "size" that have nothing of their own to say about
 *  which variant is the "right" one) uses the composite `matchScore`, since
 *  it is the one ranking figure every row carries no matter how the page is
 *  sorted. */
function primaryComparator(sort: ResultSort): (a: HubModel, b: HubModel) => number {
  return sort === "fit" ? byFitThenDownloads : byMatchThenDownloads;
}

/** Every result, collapsed to one family per model — headed by its base
 *  model when that repo is among the results, else by the member
 *  `primaryComparator(sort)` ranks first, with the rest ordered by that same
 *  comparator and an untagged row standing alone. `sort` defaults to `"fit"`
 *  for a caller with no sort context of its own.
 *
 *  **Families are positioned at their PRIMARY's index in `models`, not at
 *  whichever member first appeared.** A family draws its primary's row —
 *  size, downloads, age, everything a column shows comes off that one
 *  member — so that is also the member whose position in an already-sorted
 *  `models` (`bySizeAscending`, the Hub's own `downloads`/`trending` order,
 *  a `sort=fit`/`sort=best` reorder) has to decide where the family lands.
 *  Positioning by first-appearance instead would let a family sit at a
 *  NON-primary variant's index while showing the primary's value there — a
 *  sort-visible column (Size, Match) visibly not ascending/descending,
 *  because the row drawn at that position could be showing a different
 *  member's value.
 *
 *  **What positioning by the primary's own index guarantees, and why.** The
 *  guarantee is NOT "the same key decided both `models`'s order and the
 *  primary" — the two keys routinely differ. Under `"size"`,
 *  `"downloads"`, `"trending"` and `"new"`, `models` is sorted by THAT key
 *  while the primary is the family's base model or (failing that) its
 *  best-ranked member. What holds unconditionally, whether or not those keys
 *  agree: a family is placed at the exact array index its own primary
 *  already held in `models`, so the resulting family order is just
 *  `models`'s own order with every non-primary member deleted — reusing a
 *  real position `models` already decided, not deriving a new one. Deleting
 *  elements from an array can never change the relative order of the ones
 *  left behind, so whatever monotone property `models`'s own sort key had
 *  among the surviving (primary) rows is preserved automatically, with no
 *  dependency on how that primary was chosen.
 */
export function groupIntoFamilies(
  models: readonly HubModel[],
  sort: ResultSort = "fit",
): HubFamily[] {
  const buckets = new Map<string, HubModel[]>();
  const indexOf = new Map<HubModel, number>();

  models.forEach((model, i) => {
    indexOf.set(model, i);
    // Joined on a SPACE, which a Hub repo id cannot contain — so no two
    // distinct (base, format) pairs flatten to the same string, a property a
    // `-` or `/` join would not have. A row with no format contributes
    // nothing rather than an empty suffix, so the common case's key stays
    // the bare base id it always was.
    const key = model.baseModel
      ? (model.format ? `${model.baseModel} ${model.format}` : model.baseModel)
      : model.id;
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
    // Every member's own `baseModel` tag (when it has one) names the SAME
    // repo — that's what put them in this bucket together — so the base
    // model itself, sitting in the bucket untagged, doesn't get a vote here;
    // whichever tagged member is found first hands back the answer. No
    // tagged member at all means this bucket is the untagged-row fallback
    // (`key` is the row's own id, not a base tag), so there's no base
    // identity to report.
    const baseModel = group.find((m) => m.baseModel)?.baseModel ?? null;
    const base = baseModel ? (group.find((m) => m.id === baseModel) ?? null) : null;
    // **The base model heads its own family whenever it is in the results,
    // regardless of score (D685).**
    // regardless of score.** A family's first row is its IDENTITY, and a
    // score is the wrong thing to decide identity with: a 4-bit republish
    // that happens to fit this machine better than the model it was made
    // from is still a republish OF it, and drawing it as the family's own
    // name says the opposite. `compare` still orders everything else, so
    // `variants` is ranked exactly as before and the best-ranked member is
    // simply the first one behind the disclosure when it is not the base.
    const primary = base ?? sorted[0];
    return { key, primary, variants: sorted.filter((m) => m !== primary), baseModel, base };
  });
  families.sort((a, b) => indexOf.get(a.primary)! - indexOf.get(b.primary)!);
  return families;
}
