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
//
// **A row with no `base_model:` tag at all still folds into a family when it
// is a plain re-upload of another untagged row** — the same weights pushed
// to a second namespace, with no relation for either copy to declare. The
// signal for that is a mirror key: the trailing segment of the repo id
// (everything after the last `/`), the measured `params`, and the measured
// `quant`, all three matching exactly. Each piece alone is common — a
// filename convention, a rounded size class, a widely-shared encoding — and
// says nothing about shared identity; together, with `params` compared as
// the server's raw integer rather than a rounded display figure, they do.
// A missing `params` or `quant` drops a row back to keying on its own id,
// since a null is not a value two rows can agree on.
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
  /** `baseModel` + `format` when a base model is known, else a mirror key
   *  (trailing name segment + `params` + `quant`) when that trio is fully
   *  measured, else the lone member's own id — stable across re-renders of
   *  the SAME result set, which is what a React `key` needs.
   *
   *  Opaque: a composite whose parts are joined by a separator no Hub repo
   *  id contains. Nothing reads it back apart as an id, and nothing should. */
  key: string;
  /** The member that ranks first under the ACTIVE sort (see `groupIntoFamilies`'s
   *  `sort` parameter) — the row a family's single line draws. A mirror
   *  family (no declared base model) instead heads on `downloads`; see
   *  `groupIntoFamilies`. */
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
 *  model when that repo is among the results, else, for a bucket built from
 *  a declared `baseModel` whose repo just isn't in this page of results, by
 *  the member `primaryComparator(sort)` ranks first, else, for a mirror
 *  bucket with no declared base at all, by whichever member has the most
 *  `downloads` (null lowest). The rest are ordered by
 *  `primaryComparator(sort)` regardless, and an untagged, unmirrored row
 *  stands alone. `sort` defaults to `"fit"` for a caller with no sort
 *  context of its own.
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
    //
    // **An untagged row falls back to a MIRROR key — trailing name segment,
    // `params`, `quant` — not straight to its own id.** A plain re-upload of
    // an untagged root repo (a second account pushing the exact same weights
    // under a new namespace) carries no `base_model:` tag for either copy to
    // point at — the Hub has no `duplicated_from` field either — so the
    // `baseModel` key above cannot see it, and without this fallback two
    // uploads of the same weights would sit in the results as if they were
    // different models. Three components, all required, is what makes the
    // fold trustworthy rather than cosmetic: the trailing segment alone is
    // just a filename convention two unrelated projects can share by
    // coincidence; `params` alone is a rounded, coarse figure shared by
    // whole families of unrelated models at a given size class; `quant`
    // alone just names an encoding every repo at that precision also
    // carries. None of the three is individually rare enough to mean
    // "same weights" — together, on an exact (not rounded) `params` match,
    // they are: two repos that agree on all three have never been observed
    // to be different weights in practice, which is the same evidentiary
    // bar `baseModel` itself clears by being a Hub-published claim rather
    // than a guess.
    const mirrorKey =
      model.params != null && model.quant != null
        ? `${model.id.slice(model.id.lastIndexOf("/") + 1)} ${model.params} ${model.quant}`
        : model.id;
    const key = model.baseModel
      ? (model.format ? `${model.baseModel} ${model.format}` : model.baseModel)
      : mirrorKey;
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
    //
    // **A mirror family (no member declares a `baseModel` at all) heads on
    // `downloads` instead, highest first, a null treated as lower than any
    // real count.** `base ?? sorted[0]` doesn't apply here — there IS no
    // declared base to defer to, by construction: `baseModel` is null for
    // the whole group, which is exactly the condition under which the key
    // itself came from the mirror fallback above rather than a `base_model:`
    // tag. Where a declared base exists this rule never runs, so the two
    // compose instead of competing: `base` wins whenever the Hub names one,
    // and only a bucket with no such claim falls through to downloads.
    // Downloads, not `compare`'s fit/match score, is the right tiebreak
    // because it is the one honest, publisher-blind signal available: it
    // doesn't require the page to know or guess which upload is
    // "canonical," it just reflects which one the rest of the Hub already
    // treats as canonical by using it.
    const primary =
      base ??
      (baseModel
        ? sorted[0]
        : group.reduce((most, m) => ((m.downloads ?? -1) > (most.downloads ?? -1) ? m : most)));
    return { key, primary, variants: sorted.filter((m) => m !== primary), baseModel, base };
  });
  families.sort((a, b) => indexOf.get(a.primary)! - indexOf.get(b.primary)!);
  return families;
}
